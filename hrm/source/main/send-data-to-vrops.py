# Copyright 2023-2026 Broadcom. All Rights Reserved.
# SPDX-License-Identifier: BSD-2
#
# Description:
# Receives the operational health data as JSON from SOS utility and supporting Powershell modules and then sends the
# data to objects in VMware Aria Operations as custom metrics for use in dashboards to monitor the platform's health.
#
# Example:
# python send-data-to-vrops.py [-options]
#
# [Options]:
# -np : Retrieves the health data from SOS Utility and Powershell cmdlets and saves it JSON files.
# It will not send the data to VMware Aria Operations.

import argparse
import datetime
import json
import os
import platform
import re
import sys
import time
from pathlib import Path

# Resolve local imports: same folder as this script, or pip --target layout (utils under main/).
# hrmWorkspaceRoot is the directory that contains utils/ and is used as cwd for env.json and encrypted_files.
_script_dir = Path(__file__).resolve().parent
_utils_next_to_script = _script_dir / "utils" / "LogUtility.py"
_utils_under_main = _script_dir / "main" / "utils" / "LogUtility.py"
if _utils_next_to_script.is_file():
    hrmWorkspaceRoot = str(_script_dir)
elif _utils_under_main.is_file():
    hrmWorkspaceRoot = str(_script_dir / "main")
else:
    sys.stderr.write(
        "Health Reporting and Monitoring could not find utils/LogUtility.py.\n"
        f"  Tried: {_utils_next_to_script}\n"
        f"  Tried: {_utils_under_main}\n"
        "Use the flat layout (utils next to this script) or the pip --target layout (main\\utils\\ under this script's "
        "directory). See README.md (Python script layout on the SDDC Manager VM).\n"
    )
    raise SystemExit(1)
sys.path.insert(0, hrmWorkspaceRoot)

# Imports below run after sys.path setup. Ruff rule E402 requires all imports at the top of the file;
# noqa: E402 tells Ruff to allow these lines, since local imports must follow the path adjustment.
# nagini is the Suite API Python client from the Aria Operations appliance, not PyPI. See README.md.
import nagini  # type: ignore[import-untyped]  # noqa: E402
import urllib3  # noqa: E402
from utils.LogUtility import LogUtility  # noqa: E402
from utils.FolderUtility import FolderUtility  # noqa: E402
from utils.SosRest import SosRest  # noqa: E402
from utils.PSUtility import PSUtility  # noqa: E402
from cryptography.fernet import Fernet, InvalidToken  # noqa: E402


def _package_version_from_metadata(package_name):
    """Return the installed distribution version for package_name, or None if unavailable."""
    try:
        from importlib.metadata import PackageNotFoundError, version
    except ImportError:
        return None
    try:
        return version(package_name)
    except PackageNotFoundError:
        return None


def _resolve_module_version(module, package_name=None):
    """Return a version string for module, using __version__ or package metadata when possible."""
    module_version = getattr(module, "__version__", None)
    if module_version is not None:
        return str(module_version)
    if package_name:
        meta_version = _package_version_from_metadata(package_name)
        if meta_version is not None:
            return meta_version
    return "unknown"


def log_runtime_debug_context(logger):
    """Log OS, Python, and imported dependency versions for support diagnostics."""
    # LogUtility.info accepts a single message string (not logging's %-format extra args).
    logger.info(
        f"Debug: OS platform={platform.platform()}, system={platform.system()}, release={platform.release()}"
    )
    logger.info(f"Debug: OS version (verbose)={platform.version()}")
    logger.info(f"Debug: Python executable={sys.executable}")
    python_version_one_line = sys.version.replace("\n", " ")
    logger.info(f"Debug: Python version={python_version_one_line}")
    stdlib_imports = ("argparse", "datetime", "json", "os", "platform", "re", "sys", "time", "pathlib")
    logger.info(
        f"Debug: Standard library modules (versions match interpreter above): {', '.join(stdlib_imports)}."
    )

    third_party = (
        ("nagini", nagini, "nagini"),
        ("urllib3", urllib3, "urllib3"),
        ("cryptography", sys.modules["cryptography"], "cryptography"),
    )
    for label, module, dist_name in third_party:
        logger.info(f"Debug: module {label} version={_resolve_module_version(module, dist_name)}")

    for utils_module_name in (
        "utils.LogUtility",
        "utils.FolderUtility",
        "utils.SosRest",
        "utils.PSUtility",
    ):
        utils_module = sys.modules.get(utils_module_name)
        if utils_module is None:
            logger.info(f"Debug: module {utils_module_name} version=not loaded")
            continue
        resolved = _resolve_module_version(utils_module, None)
        if resolved == "unknown":
            resolved = "in-tree (no package version)"
        logger.info(f"Debug: module {utils_module_name} version={resolved}")


def push_handler(func):
    def inner_function(*args, **kwargs):
        try:
            func(*args, **kwargs)
            args[0].logger.info('############################################################################')
        except Exception:
            # LogUtility has no .exception(); error(..., trace=True) appends traceback.format_exc().
            args[0].logger.error(f"Exception in {func.__name__}.", trace=True)

    return inner_function


class PushDataVrops:

    def __init__(self, args):
        # Use workspace that contains utils/ (flat main/ or pip --target parent + main/).
        os.chdir(hrmWorkspaceRoot)
        env_file = args.env_json
        try:
            with open(env_file) as f:
                env_info = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError) as e:
            raise SystemExit(f"env.json not found or invalid: {e}") from e

        # set logger
        log_level = env_info["log_level"]
        self.logger = LogUtility.get_logger(log_level)
        log_runtime_debug_context(self.logger)

        # set env
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        self.logger.info('Gathering environment info..')
        self.logger.info(f'script started in path - {os.getcwd()}')
        self.logger.info(f'change current working directory to - {os.path.dirname(__file__)}')
        self.logger.info(os.getcwd())
        self.logger.info(f'Log files located at - {self.logger.test_log_folder}')

        # set script options
        self.push_to_vrops = args.push_data

        # read env.json info
        self.adapter_kind = env_info["adapterKind"]
        self.vm_resource_kind = env_info["vm_resourceKind"]
        self.pod_resource_kind = env_info["pod_resourceKind"]
        self.cluster_resource_kind = env_info["cluster_resourceKind"]
        self.esx_adapter_kind = env_info["esx_adapterKind"]
        self.esx_resource_kind = env_info["esx_resourceKind"]
        self.nsx_adapter_kind = env_info["nsx_adapterKind"]
        self.nsx_resource_kind = env_info["nsx_resourceKind"]
        self.forward_lookup = env_info["constants"]["forward_dns_lookup"]
        self.reverse_lookup = env_info["constants"]["reverse_dns_lookup"]
        self.data_file = env_info["constants"]["data_file"]
        self.component_connectivity_json = env_info["constants"]["component_connectivity_json"]
        self.backup_status_json = env_info["constants"]["backup_status_json"]
        self.storagecapacityhealth_status_json = env_info["constants"]["storagecapacityhealth_status_json"]
        self.nsxtcombinedhealthnonsos_status_json = env_info["constants"]["nsxtcombinedhealthnonsos_status_json"]
        self.snapshot_status_json = env_info["constants"]["snapshot_status_json"]
        self.nsxttier0bgp_status_json = env_info["constants"]["nsxttier0bgp_status_json"]
        self.nsxttransportnode_status_json = env_info["constants"]["nsxttransportnode_status_json"]
        self.nsxttntunnel_status_json = env_info["constants"]["nsxttntunnel_status_json"]
        self.cdrom_status_json = env_info["constants"]["cdrom_status_json"]
        self.esxi_connection_status_json = env_info["constants"]["esxi_connection_status_json"]
        self.sddc_manager_free_pool_status_json = env_info["constants"]["sddc_manager_free_pool_status_json"]

        self.vrops_fqdn = env_info["vrops"]["fqdn"]
        self.vrops_passwd = None
        self.vrops_user = env_info["vrops"]["user"]
        self.sddc_manager_fqdn = env_info["sddc_manager"]["fqdn"]
        self.sddc_manager_pwd = None
        self.sddc_manager_user = env_info["sddc_manager"]["user"]
        self.sddc_manager_local_user = env_info["sddc_manager"]["local_user"]
        self.sddc_manager_local_pwd = None
        self.vcf_version = None
        self.sos_force = env_info.get("sos_options", {}).get("force", False)
        self.sos_include_free_hosts = env_info.get("sos_options", {}).get("include_free_hosts", False)
        self.sos_lock_max_hours = int(env_info.get("sos_options", {}).get("lock_max_hours", 4))
        self.sos_use_run_lock = env_info.get("sos_options", {}).get("use_run_lock", True)

        # set status codes
        self.codes = {'green': 0, 'yellow': 1, 'red': 2, 'NA': 1, 'skipped': 0}

        # resource inventory object
        self.resource_inventory = {}
        self.object_data = {}

        try:
            self.log_retention = int(env_info["log_retention_in_days"])
            self.logger.info(f'Cleaning up older logs from location: '
                             f'{os.path.dirname(self.logger.test_log_folder)}')
            self.logger.info(f'Deleting logs older than {self.log_retention} days')
            FolderUtility.delete_logs_older_than_days(self.log_retention, self.logger)
        except Exception as e:
            self.logger.error('Exception occurred while deleting older logs')
            self.logger.error(e)

        self.decrypt_pwds()

        # get vrops nagini client
        self.vrops = nagini.Nagini(host=self.vrops_fqdn, user_pass=(self.vrops_user, self.vrops_passwd))

        self.logger.info('Fetching data from VMware.CloudFoundation.Reporting cmdlets...')
        self.get_data_from_reporting_module()
        self.data = None

    def decrypt_pwds(self):
        """Load Fernet key and decrypt credentials from encrypted_files."""
        encrypted_pwds_path = os.path.join("encrypted_files", "encrypted_pwds")
        key_path = os.path.join("encrypted_files", "key")
        try:
            with open(encrypted_pwds_path, "rb") as f:
                pwds = [ln for ln in f.read().splitlines() if ln]
            with open(key_path, "rb") as f:
                key_bytes = f.read().strip()
        except FileNotFoundError as e:
            raise SystemExit(
                f"Missing encrypted credential files. Expected {encrypted_pwds_path} and {key_path}. {e}"
            ) from e
        except OSError as e:
            raise SystemExit(f"Unable to read encrypted credential files: {e}") from e

        if len(pwds) < 3:
            raise SystemExit(
                f"encrypted_pwds must contain at least 3 lines (vrops, SDDC user, SDDC local); found {len(pwds)}."
            )

        try:
            fernet = Fernet(key_bytes)
            self.vrops_passwd = fernet.decrypt(pwds[0]).decode()
            self.sddc_manager_pwd = fernet.decrypt(pwds[1]).decode()
            self.sddc_manager_local_pwd = fernet.decrypt(pwds[2]).decode()
        except (ValueError, InvalidToken) as e:
            raise SystemExit(f"Invalid encryption key or corrupted encrypted password file: {e}") from e

    def get_resource_mapping_info(self):
        esx_res = self.match_resources(self.esx_resource_kind, self.esx_adapter_kind)
        self.logger.info(f'ESX Resource mapping info - {esx_res}, size {len(esx_res)}')

        nsx_res = self.match_resources(self.nsx_resource_kind, self.nsx_adapter_kind)
        self.logger.info(f'NSX Resource mapping info - {nsx_res},  size {len(nsx_res)}')

        vm_res = self.match_resources(self.vm_resource_kind, self.adapter_kind)
        self.logger.info(f'VM Resource mapping info - {vm_res},  size {len(vm_res)}')

        pod_res = self.match_resources(self.pod_resource_kind, self.adapter_kind)
        self.logger.info(f'Pod Resource mapping info - {pod_res},  size {len(pod_res)}')

        cluster_res = self.match_resources(self.cluster_resource_kind, self.adapter_kind)
        self.logger.info(f'Cluster Resource mapping info - {cluster_res},  size {len(cluster_res)}')

        self.resource_inventory = {**esx_res, **nsx_res, **vm_res, **cluster_res, **pod_res}
        self.logger.info(f'Total number of resources in inventory: {len(self.resource_inventory)}')
        self.logger.info(f'Number of esx resources: {len(esx_res)} ')
        self.logger.info(f'Number of nsx resources: {len(nsx_res)} ')
        self.logger.info(f'Number of vm resources: {len(vm_res)} ')
        self.logger.info(f'Number of cluster resources: {len(cluster_res)} ')
        self.logger.info(f'Number of pod resources: {len(pod_res)} ')

        total_len = len(esx_res) + len(nsx_res) + len(vm_res) + len(cluster_res) + len(pod_res)

        self.logger.info(f'Total resources (adding all categories above): {total_len}')
        if total_len != len(self.resource_inventory):
            self.logger.warning("There are duplicate resources in the inventory, please check.")
        return

    def backup_existing_file(self, data_file):
        backup_name = datetime.datetime.now().strftime('health-results_%H_%M_%d_%m_%Y.json')
        # remove existing file health_data_file
        if not os.path.exists('backup'):
            self.logger.info('creating backup directory')
            os.makedirs('backup')
        if os.path.exists(data_file):
            os.replace(data_file, os.path.join('backup', backup_name))

    def get_data_from_reporting_module(self):
        psu = PSUtility(logger=self.logger)
        modules_without_root = ['Publish-BackupStatus',
                                'Publish-NsxtTransportNodeStatus',
                                'Publish-NsxtTransportNodeTunnelStatus',
                                'Publish-NsxtTier0BgpStatus',
                                'Publish-SnapshotStatus',
                                'Publish-ComponentConnectivityHealthNonSOS',
                                'Publish-VmConnectedCdrom',
                                'Publish-EsxiConnectionHealth']

        without_local_user_cmd = f"-server {self.sddc_manager_fqdn} -user {self.sddc_manager_user} " \
                                 f"-pass '{self.sddc_manager_pwd}' -allDomains " \
                                 f"-outputJson {self.logger.test_log_folder}"
        request_token_cmd = f"Request-VCFToken -fqdn {self.sddc_manager_fqdn} -username {self.sddc_manager_user} " \
                            f"-password '{self.sddc_manager_pwd}'"
        publish_nsx_cmd = 'Publish-NsxtHealthNonSOS ' + without_local_user_cmd

        combined_cmd = request_token_cmd + ' ; ' + publish_nsx_cmd
        psu.execute_ps_cmd(combined_cmd)

        get_vcf_version_cmd = '(Get-VCFManager -version)'
        combined_cmd = request_token_cmd + ' ; ' + get_vcf_version_cmd
        version_output = psu.execute_ps_cmd(combined_cmd)
        if not version_output:
            raise RuntimeError("Unable to get VCF version: PowerShell returned no output.")
        pattern = r'\b\d+\.\d+\.\d+\.\d+\b'
        match = re.search(pattern, version_output)
        if match:
            self.vcf_version = match.group(0)
            self.logger.info(f"Extracted VCF version: {self.vcf_version}")
        else:
            raise RuntimeError("Unable to find VCF version in PowerShell output.")
        # module w/out -allDomains (ex. Publish-SddcManagerFreePool)
        without_all_domain_cmd = f"-server {self.sddc_manager_fqdn} -user {self.sddc_manager_user} " \
                                 f"-pass '{self.sddc_manager_pwd}' " \
                                 f"-outputJson {self.logger.test_log_folder}"
        publish_freepool_cmd = 'Publish-SddcManagerFreePool ' + without_all_domain_cmd
        psu.execute_ps_cmd(publish_freepool_cmd)

        # module requiring sddc local user
        psu.execute_ps_cmd(f"Publish-StorageCapacityHealth {without_local_user_cmd} "
                           f" -localUser {self.sddc_manager_local_user}"
                           f" -localPass '{self.sddc_manager_local_pwd}'")

        for module in modules_without_root:
            psu.execute_ps_cmd(f'{module} {without_local_user_cmd}')

        self.logger.info(f'Generated JSON files for Publish-* cmdlets in location {self.logger.test_log_folder}')

    def get_list_of_workload_domain(self):
        psu = PSUtility(logger=self.logger)
        request_token_cmd = f"Request-VCFToken -fqdn {self.sddc_manager_fqdn} -username {self.sddc_manager_user} " \
                            f"-password '{self.sddc_manager_pwd}'"
        get_workload_domains_cmd = '((Get-VCFWorkloadDomain | Sort-Object type | Select-Object name).name)'
        combined_cmd = request_token_cmd + ' ; ' + get_workload_domains_cmd
        workload_domains_string = psu.execute_ps_cmd(combined_cmd)
        workload_domains_list = workload_domains_string.split("\n")
        self.domain_list = [d.strip() for d in workload_domains_list[1:-1] if d and d.strip()]
        if not self.domain_list:
            self.logger.warning("No workload domains returned; will run SoS for management domain only (scope with empty domain).")
            self.domain_list = [None]

    def _merge_health_results(self, existing, new_data):
        """Deep-merge new health-results into existing so all domains' data is combined."""
        if not new_data:
            return existing
        if not existing:
            return new_data
        merged = {}
        all_keys = set(existing.keys()) | set(new_data.keys())
        for key in all_keys:
            old_val = existing.get(key)
            new_val = new_data.get(key)
            if old_val is None:
                merged[key] = new_val
            elif new_val is None:
                merged[key] = old_val
            elif isinstance(old_val, list) and isinstance(new_val, list):
                merged[key] = old_val + new_val
            elif isinstance(old_val, dict) and isinstance(new_val, dict):
                merged[key] = self._merge_health_results(old_val, new_val)
            else:
                merged[key] = new_val
        return merged

    def get_sos_data_from_sddc_manager(self, domain=None):
        dest = os.path.join(self.logger.test_log_folder, self.data_file)

        if domain is None:
            self.logger.info('Fetching data from SOS Utility on SDDC Manager...')
        else:
            self.logger.info(f'Fetching {domain} workload domain data from SOS Utility on SDDC Manager...')
        self.logger.info('This can take 15~90 min (or even more) depending on the size of your environment. '
                         'Please wait....')

        poll_max_seconds = max(1, int(self.sos_lock_max_hours)) * 3600
        sosrest = SosRest(
            host=self.sddc_manager_fqdn,
            user=self.sddc_manager_user,
            password=self.sddc_manager_pwd,
            domain=domain,
            logger=self.logger,
            force=self.sos_force,
            include_free_hosts=self.sos_include_free_hosts,
            poll_max_elapsed_seconds=poll_max_seconds,
        )
        request_id = sosrest.start_health_checks_op(vcf_version=self.vcf_version)
        if not request_id:
            self.logger.error("SDDC Manager did not return a health-summary operation id (HTTP 202 id missing).")
            if domain is None:
                self.logger.error("Unable to get data from SOS Utility on SDDC Manager")
            else:
                self.logger.error(
                    f"Unable to get {domain} workload domain data from SOS Utility on SDDC Manager"
                )
            return None
        try:
            sosrest.get_health_checks_status(request_id)
        except TimeoutError as e:
            self.logger.error(e)
            if domain is None:
                self.logger.error("Unable to get data from SOS Utility on SDDC Manager")
            else:
                self.logger.error(
                    f"Unable to get {domain} workload domain data from SOS Utility on SDDC Manager"
                )
            return None
        except ValueError as e:
            self.logger.error(f"Invalid JSON while polling SDDC health-summary: {e}")
            if domain is None:
                self.logger.error("Unable to get data from SOS Utility on SDDC Manager")
            else:
                self.logger.error(
                    f"Unable to get {domain} workload domain data from SOS Utility on SDDC Manager"
                )
            return None
        sosrest.get_health_check_bundle(request_id, path=self.logger.test_log_folder)

        if os.path.exists(dest):
            return self.read_data(dest)
        self.logger.error(f'Unable to find {self.data_file} in {dest}')
        if domain is None:
            self.logger.error("Unable to get data from SOS Utility on SDDC Manager")
        else:
            self.logger.error(f'Unable to get {domain} workload domain data from SOS Utility on SDDC Manager')
        return None

    def match_resources(self, resource_kind, adapter_kind):
        try:
            resource_data = {}
            page = 0
            page_size = 1000
            data = self.vrops.get_resources(resourceKind=resource_kind, adapterKindKey=adapter_kind,
                                            pageSize=page_size, page=page)

            page_info = data['pageInfo']
            total_count = page_info['totalCount']
            total_pages = int(total_count / page_size)
            for resource in data['resourceList']:
                resource_data[resource['resourceKey']['name']] = resource['identifier']

            while total_pages > 0:
                total_pages = total_pages - 1
                page = page + 1
                data = self.vrops.get_resources(resourceKind=resource_kind, adapterKindKey=adapter_kind,
                                                pageSize=page_size, page=page)
                for resource in data['resourceList']:
                    resource_data[resource['resourceKey']['name']] = resource['identifier']

            self.logger.info(f'Resource inventory data from VMware Aria Operations for ResourceKind: {resource_kind} '
                             f'and AdapterKind: {adapter_kind}')
            self.logger.info(resource_data)

            return resource_data
        except Exception as e:
            self.logger.error(e)
            self.logger.error('Unable to connect to VMware Aria Operations using given credentials')
            raise

    def read_data(self, data_file):
        if data_file is None:
            self.logger.info("read_data: data_file is None")
            return None
        if not os.path.exists(data_file):
            self.logger.info(f"read_data: file does not exist - {data_file}")
            return None
        try:
            if os.path.getsize(data_file) == 0:
                self.logger.info(f"{data_file} is empty")
                return []
            with open(data_file) as df:
                return json.load(df)
        except (json.JSONDecodeError, OSError) as e:
            self.logger.error(f"read_data: failed to read {data_file} - {e}")
            return None

    def get_complete_json_file_name(self, file_name):
        path = None
        for file in os.listdir(self.logger.test_log_folder):
            if file.endswith(file_name):
                path = os.path.join(self.logger.test_log_folder, file)
                break
        if not path:
            self.logger.info(f'Unable to find file ending with {file_name} in {self.logger.test_log_folder}')
        return path

    def push_data(self):
        self.get_resource_mapping_info()
        self.logger.info('############################################################################')

        file_name = self.get_complete_json_file_name(self.backup_status_json)
        if file_name:
            self.push_backup_status(file_name)
        else:
            self.logger.info(f"Skipping backup status: file not found ending with {self.backup_status_json}")

        file_name = self.get_complete_json_file_name(self.storagecapacityhealth_status_json)
        if file_name:
            self.push_storagecapacityhealth_status(file_name)
        else:
            self.logger.info(f"Skipping storage capacity health: file not found ending with {self.storagecapacityhealth_status_json}")

        file_name = self.get_complete_json_file_name(self.nsxtcombinedhealthnonsos_status_json)
        if file_name:
            self.push_nsxtcombinedhealthnonsos_status(file_name)
        else:
            self.logger.info(f"Skipping NSXT combined health: file not found ending with {self.nsxtcombinedhealthnonsos_status_json}")

        file_name = self.get_complete_json_file_name(self.component_connectivity_json)
        if file_name:
            self.push_componentconnectivityhealth_status(file_name)
        else:
            self.logger.info(f"Skipping component connectivity: file not found ending with {self.component_connectivity_json}")

        file_name = self.get_complete_json_file_name(self.snapshot_status_json)
        if file_name:
            self.push_snapshot_status(file_name)
        else:
            self.logger.info(f"Skipping snapshot status: file not found ending with {self.snapshot_status_json}")

        file_name = self.get_complete_json_file_name(self.nsxttier0bgp_status_json)
        if file_name:
            self.push_nsxttier0_status(file_name)
        else:
            self.logger.info(f"Skipping NSXT Tier0 BGP: file not found ending with {self.nsxttier0bgp_status_json}")

        file_name = self.get_complete_json_file_name(self.nsxttransportnode_status_json)
        if file_name:
            self.push_nsxt_transportnode_status(file_name)
        else:
            self.logger.info(f"Skipping NSXT transport node: file not found ending with {self.nsxttransportnode_status_json}")

        file_name = self.get_complete_json_file_name(self.nsxttntunnel_status_json)
        if file_name:
            self.push_nsxt_tunnel_status(file_name)
        else:
            self.logger.info(f"Skipping NSXT tunnel: file not found ending with {self.nsxttntunnel_status_json}")

        file_name = self.get_complete_json_file_name(self.cdrom_status_json)
        if file_name:
            self.push_vm_connected_cdrom_status(file_name)
        else:
            self.logger.info(f"Skipping VM CD-ROM status: file not found ending with {self.cdrom_status_json}")

        file_name = self.get_complete_json_file_name(self.esxi_connection_status_json)
        if file_name:
            self.push_esxi_connection_health(file_name)
        else:
            self.logger.info(f"Skipping ESXi connection health: file not found ending with {self.esxi_connection_status_json}")

        file_name = self.get_complete_json_file_name(self.sddc_manager_free_pool_status_json)
        if file_name:
            self.push_sddc_manager_free_pool_status(file_name)
        else:
            self.logger.info(f"Skipping SDDC Manager free pool: file not found ending with {self.sddc_manager_free_pool_status_json}")

        # pushing data from sos utility health-results.json
        if self.data:
            self.push_data_password()
            self.push_data_certificates()
            self.push_dns_lookup(self.forward_lookup)
            self.push_dns_lookup(self.reverse_lookup)
            self.push_ntp()
            self.push_services()
            self.push_compute()
            self.push_vsan()
            self.push_connectivity()
            self.push_hw_compatibility()
            self.push_general()
            self.push_sddc_versions()
        else:
            self.logger.info('Skipping pushing SOS data to VMware Aria Operations. No data received.')

        self.push_data_to_vrops()

        self.logger.info("############################### Script Complete ############################################")
        self.logger.info(f'Log files located at - {self.logger.test_log_folder}')

    def get_most_nested_dict(self, dictionary, parent_key=None, prev_keys=None):
        if prev_keys is None:
            prev_keys = []
        for key, value in dictionary.items():
            if isinstance(value, dict):
                parent_key = key
                prev_keys.append(key)
                yield from self.get_most_nested_dict(value, parent_key, prev_keys)
            else:
                yield dictionary, parent_key, prev_keys
                break

    def push_data_to_vrops(self):
        for resource_id, data in self.object_data.items():
            if data:
                if not resource_id:
                    self.logger.warning(
                        f"Skipping push of {len(data)} custom metrics: no VMware Aria Operations object id "
                        f"(hostname may be missing from resource inventory)."
                    )
                    continue
                metrics_payload_json = {"stat-content": data}
                if self.push_to_vrops:
                    try:
                        self.vrops.add_stats(metrics_payload_json, id=resource_id)
                        self.logger.info(
                            f'***** Pushed {len(data)} custom metrics to VMware Aria Operations Object ID: {resource_id}')
                        # time.sleep(0.5)
                    except Exception as e:
                        self.logger.error(
                            f'***** Unable to push custom metrics to VMware Aria Operations Object ID: {resource_id}')
                        self.logger.error(e)
                else:
                    self.logger.info(
                        "***** Will not push data to VMware Aria Operations : args.push_data = False *****")
            else:
                self.logger.info(f'No data available for object id {resource_id}')

    def build_payload(self, category, resource_id, hostname, metrics_payload):
        if not resource_id:
            self.logger.warning(
                f"Skipping payload for {category}: no VMware Aria Operations resource id for host {hostname!r}. "
                f"Confirm the object exists in inventory and adapter kinds in env.json match your environment."
            )
            return
        self.logger.info(f'********************** Building Payload for {category}: {hostname} **********************')
        existing_data = self.object_data.get(resource_id)
        if existing_data:
            new_data = metrics_payload.get('stat-content')
            existing_data.extend(new_data)
            self.object_data[resource_id] = existing_data
        else:
            self.object_data[resource_id] = metrics_payload.get('stat-content')

        self.logger.debug(str(self.object_data[resource_id]))

    @push_handler
    def push_general(self):
        category = "General"
        update_count = 0
        for d, parent_key, prev_keys in self.get_most_nested_dict(self.data[category]):
            if parent_key == "":
                continue
            if parent_key == "Vcenter Ring Topology Status":
                hostname = self.sddc_manager_fqdn
                component = 'SDDCMANAGER'
            else:
                if ':' in d['area']:
                    component, hostname = d['area'].split(':')
                else:
                    hostname = d['area']
                    component = 'NA'
                hostname = hostname.lstrip('*').lstrip().rstrip()
                component = component.lstrip('*').rstrip().lstrip()
            resource_name = hostname.split(".")[0]
            self.logger.info(f'Hostname: {hostname}, Component: {component}')

            resource_id = self.get_resource_id(hostname, resource_name)

            metrics_payload = {"stat-content": []}
            timestamp_raw = d['timestamp']
            timestamp = time.mktime(datetime.datetime.strptime(timestamp_raw, "%c").timetuple())

            if 'NSX Edge Cluster Health Status' in d['title']:
                metric_key = 'NSX Edge Cluster Health Status'
            elif 'NSX Edge Health Status' in d['title']:
                metric_key = 'NSX Edge Health Status'
            elif 'No NSX Edge Cluster available' in d['title']:
                metric_key = 'NSX Edge Cluster Health Status'
            elif parent_key not in d['area']:
                metric_key = d['title'].lstrip('*').rstrip().lstrip().split("\n")[0]
            else:
                prev_keys.pop()
                metric_key = prev_keys[-1]

            for k, v in d.items():
                details = {
                    "statKey": f"SOS General|{metric_key}|{k}",
                    "timestamps": [int(timestamp * 1000)],
                    "data" if isinstance(v, int) else "values": [v]
                }

                metrics_payload["stat-content"].append(details)
                if k == 'alert':
                    details = {
                        "statKey": f"SOS General|{metric_key}|alert_code",
                        "timestamps": [int(timestamp * 1000)],
                        "data": [self.codes[v.lower()]] if v.lower() in self.codes else [self.codes['NA']]
                    }
                    metrics_payload["stat-content"].append(details)

            self.logger.info(resource_id)
            self.logger.info(metrics_payload)
            self.build_payload(category, resource_id, hostname, metrics_payload)
            update_count = update_count + 1

        self.logger.info(f'Total statKeys = {update_count}')

    @push_handler
    def push_hw_compatibility(self):
        category = "Hardware Compatibility"
        update_count = 0
        for d, parent_key, _ in self.get_most_nested_dict(self.data[category]):
            if parent_key == "":
                continue
            component, hostname = d['area'].split(':')
            hostname = hostname.lstrip().rstrip()
            component = component.rstrip().lstrip()
            self.logger.info(f'Hostname: {hostname}, Component: {component}')

            resource_id = self.get_resource_id(hostname, None)

            metrics_payload = {"stat-content": []}
            timestamp_raw = d['timestamp']
            timestamp = time.mktime(datetime.datetime.strptime(timestamp_raw, "%c").timetuple())

            for k, v in d.items():
                if k.lower() == 'title':
                    flat_list = []
                    for sublist in v:
                        if isinstance(sublist, list):
                            for item in sublist:
                                flat_list.append(item)
                        else:
                            flat_list.append(sublist)
                    v = ','.join(flat_list)
                details = {
                    "statKey": f"SOS Hardware Compatibility|{parent_key}|{k}",
                    "timestamps": [int(timestamp * 1000)],
                    "data" if isinstance(v, int) else "values": [v]
                }
                metrics_payload["stat-content"].append(details)
                if k == 'alert':
                    details = {
                        "statKey": f"SOS Hardware Compatibility|{parent_key}|alert_code",
                        "timestamps": [int(timestamp * 1000)],
                        "data": [self.codes[v.lower()]] if v.lower() in self.codes else [self.codes['NA']]
                    }
                    metrics_payload["stat-content"].append(details)

            self.logger.info(resource_id)
            self.logger.info(metrics_payload)
            self.build_payload(category, resource_id, hostname, metrics_payload)
            update_count = update_count + 1

        self.logger.info(f'Total statKeys = {update_count}')

    @push_handler
    def push_connectivity(self):
        category = "Connectivity"
        update_count = 0
        for d, parent_key, _ in self.get_most_nested_dict(self.data[category]):
            if parent_key == "NSX Ping Status":
                continue
            if parent_key == "Vcenter Ring Topology Status":
                hostname = self.sddc_manager_fqdn
                component = 'SDDCMANAGER'
            else:
                component, hostname = d['area'].split(':')
                hostname = hostname.lstrip().rstrip()
                component = component.rstrip().lstrip()
            resource_name = hostname.split(".")[0]
            self.logger.info(f'Hostname: {hostname}, Component: {component}')

            resource_id = self.get_resource_id(hostname, resource_name)

            metrics_payload = {"stat-content": []}
            timestamp_raw = d['timestamp']
            timestamp = time.mktime(datetime.datetime.strptime(timestamp_raw, "%c").timetuple())

            for k, v in d.items():
                if 'SSH' in d['title']:
                    details = {
                        "statKey": f"SOS SSH Connectivity Health|{k}",
                        "timestamps": [int(timestamp * 1000)],
                        "data" if isinstance(v, int) else "values": [v]
                    }
                    metrics_payload["stat-content"].append(details)
                    if k == 'alert':
                        details = {
                            "statKey": "SOS SSH Connectivity Health|alert_code",
                            "timestamps": [int(timestamp * 1000)],
                            "data": [self.codes[v.lower()]] if v.lower() in self.codes else [self.codes['skipped']]
                        }
                        metrics_payload["stat-content"].append(details)
                else:
                    details = {
                        "statKey": f"SOS {d['title']}|{k}",
                        "timestamps": [int(timestamp * 1000)],
                        "data" if isinstance(v, int) else "values": [v]
                    }
                    metrics_payload["stat-content"].append(details)
                    if k == 'alert':
                        details = {
                            "statKey": f"SOS {d['title']}|alert_code",
                            "timestamps": [int(timestamp * 1000)],
                            "data": [self.codes[v.lower()]] if v.lower() in self.codes else [self.codes['NA']]
                        }
                        metrics_payload["stat-content"].append(details)

                if parent_key != hostname:
                    details = {
                        "statKey": f"SOS {d['title']}|IP",
                        "timestamps": [int(timestamp * 1000)],
                        "data" if isinstance(v, int) else "values": [parent_key]
                    }
                    metrics_payload["stat-content"].append(details)
                    if k == 'alert':
                        details = {
                            "statKey": f"SOS {d['title']}|alert_code",
                            "timestamps": [int(timestamp * 1000)],
                            "data": [self.codes[v.lower()]] if v.lower() in self.codes else [self.codes['NA']]
                        }
                        metrics_payload["stat-content"].append(details)

            self.logger.info(resource_id)
            self.logger.info(metrics_payload)
            self.build_payload(category, resource_id, hostname, metrics_payload)
            update_count = update_count + 1

        self.logger.info(f'Total statKeys = {update_count}')

    @push_handler
    def push_vsan(self):
        category = 'vSAN'
        update_count = 0
        for key, value in self.data[category].items():
            alert_code_val = 0
            if value.get('area'):
                hostname = key
                resource_id = self.get_resource_id(hostname, None)

                metrics_payload = {"stat-content": []}
                timestamp_raw = value['timestamp']
                timestamp = time.mktime(datetime.datetime.strptime(timestamp_raw, "%c").timetuple())

                for k, v in value.items():
                    details = {
                        "statKey": f"SOS vSAN Summary|{k}",
                        "timestamps": [int(timestamp * 1000)],
                        "data" if isinstance(v, int) else "values": [v]
                    }
                    metrics_payload["stat-content"].append(details)

                    if k == 'alert':
                        if v.lower() == "":
                            if value['status'].lower() == "passed":
                                alert_code_val = 0
                            else:
                                alert_code_val = 1
                        details = {
                            "statKey": "SOS vSAN Summary|alert_code",
                            "timestamps": [int(timestamp * 1000)],
                            "data": [self.codes[v.lower()]] if v.lower() in self.codes else [alert_code_val]
                        }
                        metrics_payload["stat-content"].append(details)

                self.logger.info(resource_id)
                self.logger.info(metrics_payload)
                self.build_payload(category, resource_id, hostname, metrics_payload)
                update_count = update_count + 1
            else:
                for host, val in value.items():
                    hostname = host.split(":")[1].split(".")[0].lstrip().rstrip()
                    cluster_name = host.split(":")[2].lstrip().rstrip()
                    resource_id = self.get_resource_id(cluster_name, None)
                    metrics_payload = {"stat-content": []}
                    timestamp_raw = val['timestamp']
                    timestamp = time.mktime(datetime.datetime.strptime(timestamp_raw, "%c").timetuple())

                    for k, v in val.items():
                        details = {
                            "statKey": f"SOS vSAN Summary:{key}|{k}",
                            "timestamps": [int(timestamp * 1000)],
                            "data" if isinstance(v, int) else "values": [v]
                        }
                        metrics_payload["stat-content"].append(details)
                        if k == 'alert':
                            if v.lower() == "":
                                if val['status'].lower() == "passed":
                                    alert_code_val = 0
                                else:
                                    alert_code_val = 1
                            details = {
                                "statKey": f"SOS vSAN Summary:{key}|alert_code",
                                "timestamps": [int(timestamp * 1000)],
                                "data": [self.codes[v.lower()]] if v.lower() in self.codes else [alert_code_val]
                            }
                            metrics_payload["stat-content"].append(details)

                    self.logger.info(resource_id)
                    self.logger.info(metrics_payload)
                    self.build_payload(category, resource_id, hostname, metrics_payload)
                    update_count = update_count + 1

        self.logger.info(f'Total statKeys = {update_count}')

    @push_handler
    def push_compute(self):
        category = "Compute"
        update_count = 0
        for key, value in self.data[category].items():
            for _host, val in value.items():
                component, hostname = val['area'].split(':')
                hostname = hostname.lstrip().rstrip()
                component = component.rstrip().lstrip()
                resource_name = hostname.split(".")[0]
                self.logger.info(f'Hostname: {hostname}, Component: {component}')

                metrics_payload = {"stat-content": []}

                timestamp_raw = val['timestamp']
                timestamp = time.mktime(datetime.datetime.strptime(timestamp_raw, "%c").timetuple())

                resource_id = self.get_resource_id(hostname, resource_name)

                for k, v in val.items():
                    details = {
                        "statKey": f"SOS Compute Summary:{key}|{k}",
                        "timestamps": [int(timestamp * 1000)],
                        "data" if isinstance(v, int) else "values": [v]
                    }
                    metrics_payload["stat-content"].append(details)
                    if k == 'alert':
                        details = {
                            "statKey": f"SOS Compute Summary:{key}|alert_code",
                            "timestamps": [int(timestamp * 1000)],
                            "data": [self.codes[v.lower()]] if v.lower() in self.codes else [self.codes['NA']]
                        }
                        metrics_payload["stat-content"].append(details)

                self.logger.info(resource_id)
                self.logger.info(metrics_payload)
                self.build_payload(category, resource_id, hostname, metrics_payload)
                update_count = update_count + 1

        self.logger.info(f'Total statKeys = {update_count}')

    @push_handler
    def push_services(self):
        category = "Services"
        update_count = 0
        for element in self.data[category]:
            for _key, value in element.items():
                component, hostname = value['area'].split(':')
                hostname = hostname.lstrip().rstrip()
                component = component.rstrip().lstrip()
                resource_name = hostname.split(".")[0]
                self.logger.info(f'Hostname: {hostname}, Component: {component}')

                metrics_payload = {"stat-content": []}

                timestamp_raw = value['timestamp']
                timestamp = time.mktime(datetime.datetime.strptime(timestamp_raw, "%c").timetuple())

                resource_id = self.get_resource_id(hostname, resource_name)

                for k, val in value.items():
                    details = {
                        "statKey": f"SOS Services Summary:{value['title']}|{k}",
                        "timestamps": [int(timestamp * 1000)],
                        "data" if isinstance(val, int) else "values": [val]
                    }
                    metrics_payload["stat-content"].append(details)
                    if k == 'alert':
                        details = {
                            "statKey": f"SOS Services Summary:{value['title']}|alert_code",
                            "timestamps": [int(timestamp * 1000)],
                            "data": [self.codes[val.lower()]]
                        }
                        metrics_payload["stat-content"].append(details)

                self.logger.info(resource_id)
                self.logger.info(metrics_payload)
                self.build_payload(category, resource_id, hostname, metrics_payload)
                update_count = update_count + 1

        self.logger.info(f'Total statKeys = {update_count}')

    @push_handler
    def push_ntp_iterator(self, category, data):
        category_orig = category
        update_count = 0
        for key, value in data.items():
            if key != 'ESXi Time' and key != 'ESXi HW Time':
                hostname = key.rstrip()
                category = category_orig
            else:
                if value != {}:
                    try:
                        hostname = value['area'].split(':')[1].lstrip().rstrip()
                        category = key
                    except Exception:
                        self.push_ntp_iterator(key, value)
                        category_orig = category
                        continue
                else:
                    continue
            resource_name = hostname.split(".")[0]
            self.logger.info(f'Hostname: {hostname}')
            metrics_payload = {"stat-content": []}

            timestamp_raw = value['timestamp']
            timestamp = time.mktime(datetime.datetime.strptime(timestamp_raw, "%c").timetuple())
            resource_id = self.get_resource_id(hostname, resource_name)

            for k, val in value.items():
                details = {
                    "statKey": f"SOS {category} Status Summary|{k}",
                    "timestamps": [int(timestamp * 1000)],
                    "data" if isinstance(val, int) else "values": [val]
                }
                metrics_payload["stat-content"].append(details)
                if k == 'alert':
                    details = {
                        "statKey": f"SOS {category} Status Summary|alert_code",
                        "timestamps": [int(timestamp * 1000)],
                        "data": [self.codes[val.lower()]]
                    }
                    metrics_payload["stat-content"].append(details)

            self.logger.info(resource_id)
            self.logger.info(metrics_payload)
            self.build_payload(category, resource_id, hostname, metrics_payload)
            update_count = update_count + 1

        self.logger.info(f'Total statKeys = {update_count}')

    @push_handler
    def push_ntp(self):
        ntp = "NTP"
        self.push_ntp_iterator(ntp, self.data[ntp])

    @push_handler
    def push_dns_lookup(self, dns_type):
        category = "DNS lookup Status"
        update_count = 0
        for key, value in self.data[category][dns_type].items():
            hostname = key.rstrip()
            resource_name = hostname.split(".")[0]
            self.logger.info(f'Hostname = {hostname}')
            metrics_payload = {"stat-content": []}

            timestamp_raw = value['timestamp']
            timestamp = time.mktime(datetime.datetime.strptime(timestamp_raw, "%c").timetuple())
            resource_id = self.get_resource_id(hostname, resource_name)

            for k, val in value.items():
                details = {
                    "statKey": f"SOS DNS {dns_type} Summary|{k}",
                    "timestamps": [int(timestamp * 1000)],
                    "data" if isinstance(val, int) else "values": [val]
                }

                metrics_payload["stat-content"].append(details)
                if k == 'alert':
                    details = {
                        "statKey": f"SOS DNS {dns_type} Summary|alert_code",
                        "timestamps": [int(timestamp * 1000)],
                        "data": [self.codes[val.lower()]]
                    }
                    metrics_payload["stat-content"].append(details)

            self.logger.info(resource_id)
            self.logger.info(metrics_payload)
            self.build_payload(category, resource_id, hostname, metrics_payload)
            update_count = update_count + 1

        self.logger.info(f'Total statKeys = {update_count}')

    def get_resource_id(self, hostname, resource_name):
        resource_id = self.resource_inventory.get(hostname)
        if not resource_id and resource_name:
            resource_id = self.resource_inventory.get(resource_name)
        if not resource_id and resource_name:
            for name, res_id in self.resource_inventory.items():
                if resource_name in name or hostname in name:
                    resource_id = res_id
                    break
        # NSX VIP FQDN (e.g. vip-nsx-mgmt) vs inventory node name (e.g. nsx-mgmt-1): match shared name stem.
        if not resource_id and hostname:
            host_first = hostname.split(".")[0].lower()
            for name, res_id in self.resource_inventory.items():
                inv_first = name.split(".")[0].lower()
                inv_core = re.sub(r"-\d+$", "", inv_first)
                if len(inv_core) >= 6 and inv_core in host_first:
                    resource_id = res_id
                    break
        return resource_id

    def push_flat_struct(self, data_type, data_arr, key_var):
        update_count = 0
        category = f"HRM {data_type} Status"
        self.logger.info(f'Pushing {data_type} status data to VMware Aria Operations')
        for arr_val in data_arr:
            data = arr_val
            user = None
            component = None
            if arr_val.get("User"):
                user = arr_val["User"]
            if arr_val.get("Component"):
                component = arr_val["Component"]
            if key_var:
                hostname = data[key_var]
            else:
                hostname = data["Resource"]
            resource_name = hostname.split(".")[0]
            self.logger.info(f'Hostname: {hostname}, Component: {component}')
            metrics_payload = {"stat-content": []}

            timestamp = time.mktime(datetime.datetime.now().timetuple())
            resource_id = self.get_resource_id(hostname, resource_name)

            for k, v in arr_val.items():
                if user:
                    statkey = f"HRM {data_type} Status:{user}"
                else:
                    statkey = f"HRM {data_type} Status"

                details = {
                    "statKey": f"{statkey}|{k.lower()}",
                    "timestamps": [int(timestamp * 1000)],
                    "values": [v]
                }
                metrics_payload["stat-content"].append(details)
                if k.lower() == 'alert':
                    details = {
                        "statKey": f"{statkey}|alert_code",
                        "timestamps": [int(timestamp * 1000)],
                        "data": [self.codes[v.lower()]] if v.lower() in self.codes else [self.codes['NA']]
                    }
                    metrics_payload["stat-content"].append(details)

            self.logger.info(resource_id)
            self.logger.info(metrics_payload)
            self.build_payload(category, resource_id, hostname, metrics_payload)
            update_count = update_count + 1
        return update_count

    @push_handler
    def push_data_password(self):
        category = "Password Expiry Status"
        self.logger.info('Pushing SOS password data to VMware Aria Operations')
        update_count = 0
        for key, value in self.data[category].items():
            hostname, user = key.split(':')
            hostname = hostname.rstrip()
            user = user.rstrip().lstrip()
            resource_name = hostname.split(".")[0]
            component = value['area'].split(":")[0].rstrip()
            self.logger.info(f"hostname = {hostname}, component = {component}")
            metrics_payload = {"stat-content": []}

            timestamp_raw = value['timestamp']
            timestamp = time.mktime(datetime.datetime.strptime(timestamp_raw, "%c").timetuple())
            resource_id = self.get_resource_id(hostname, resource_name)

            for k, v in value.items():
                if k == 'title':
                    for k1, val in value['title'].items():
                        if key != 'host':
                            if not isinstance(val, int) and val.lower() == 'never':
                                val = 999999999
                            details = {
                                "statKey": f"SOS Password Summary:{user}|{k1}",
                                "timestamps": [int(timestamp * 1000)],
                                "data" if isinstance(val, int) else "values": [val]
                            }
                            metrics_payload["stat-content"].append(details)
                else:
                    details = {
                        "statKey": f"SOS Password Summary:{user}|{k}",
                        "timestamps": [int(timestamp * 1000)],
                        "values": [v]
                    }
                    metrics_payload["stat-content"].append(details)
                    if k == 'alert':
                        details = {
                            "statKey": f"SOS Password Summary:{user}|alert_code",
                            "timestamps": [int(timestamp * 1000)],
                            "data": [self.codes[v.lower()]] if v.lower() in self.codes else [self.codes['NA']]
                        }
                        metrics_payload["stat-content"].append(details)

            self.logger.info(resource_id)
            self.logger.info(metrics_payload)
            self.build_payload(category, resource_id, hostname, metrics_payload)
            update_count = update_count + 1

        self.logger.info(f'Total statKeys = {update_count} ')

    @push_handler
    def push_backup_status(self, file_name):
        data_arr = self.read_data(file_name)
        if data_arr is None:
            self.logger.info("Skipping backup status: could not read file")
            return
        update_count = 0
        category = "HRM Backup Status"
        data_type = "Backup"
        self.logger.info(f'Pushing {data_type} status data to VMware Aria Operations')

        for data in data_arr:
            component = data["Component"]
            index_val = data["Resource"]
            hostname = data["Element"]
            if component == "NSX Manager":
                statkey = f"HRM {data_type} Status:{index_val}"
            else:
                statkey = f"HRM {data_type} Status"
            resource_name = hostname.split(".")[0]
            self.logger.info(f'Hostname: {hostname}, Component: {component}')
            metrics_payload = {"stat-content": []}
            timestamp = time.mktime(datetime.datetime.now().timetuple())
            resource_id = self.get_resource_id(hostname, resource_name)

            for k, v in data.items():
                if k.lower() == 'date':
                    k = "date_taken"
                    if v:
                        match = re.search(r"\d{10}", v)
                        if match:
                            timestamp_raw = int(match.group())
                            parsed_date = datetime.datetime.fromtimestamp(timestamp_raw)
                            datetime_str = parsed_date.strftime("%b %d %H:%M:%S %Y GMT")
                            v = datetime_str
                        else:
                            match = re.search("\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", v)
                            if match:
                                date_format = "%Y-%m-%dT%H:%M:%S"
                                parsed_date = datetime.datetime.strptime(match.group(), date_format)
                                datetime_str = parsed_date.strftime("%b %d %H:%M:%S %Y GMT")
                                v = datetime_str
                    else:
                        v = ""
                details = {
                    "statKey": f"{statkey}|{k.lower()}",
                    "timestamps": [int(timestamp * 1000)],
                    "values": [v]
                }
                metrics_payload["stat-content"].append(details)
                if k.lower() == 'alert':
                    details = {
                        "statKey": f"{statkey}|alert_code",
                        "timestamps": [int(timestamp * 1000)],
                        "data": [self.codes[v.lower()]] if v.lower() in self.codes else [self.codes['NA']]
                    }
                    metrics_payload["stat-content"].append(details)

            self.logger.info(resource_id)
            self.logger.info(metrics_payload)
            self.build_payload(category, resource_id, hostname, metrics_payload)
            update_count = update_count + 1

        self.logger.info(f'Total statKeys = {update_count} ')

    @push_handler
    def push_snapshot_status(self, file_name):
        data_arr = self.read_data(file_name)
        if data_arr is None:
            self.logger.info("Skipping snapshot status: could not read file")
            return
        category = "HRM Snapshot Status"
        data_type = "Snapshot"
        self.logger.info(f'Pushing {data_type} status data to VMware Aria Operations')
        update_count = 0
        for data in data_arr:
            component = data["Component"]
            hostname = data["Element"]
            resource_name = hostname.split(".")[0]
            self.logger.info(f'Hostname: {hostname}, Component: {component}')
            metrics_payload = {"stat-content": []}

            timestamp = time.mktime(datetime.datetime.now().timetuple())
            resource_id = self.get_resource_id(hostname, resource_name)

            for k, v in data.items():
                if k.lower() == 'latest':
                    k = "date_taken"
                    if v:
                        match = re.search(r"\d{10}", v)
                        if match:
                            timestamp_raw = int(match.group())
                            parsed_date = datetime.datetime.fromtimestamp(timestamp_raw)
                            datetime_str = parsed_date.strftime("%b %d %H:%M:%S %Y GMT")
                            v = datetime_str
                        else:
                            match = re.search("\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", v)
                            if match:
                                date_format = "%Y-%m-%dT%H:%M:%S"
                                parsed_date = datetime.datetime.strptime(match.group(), date_format)
                                datetime_str = parsed_date.strftime("%b %d %H:%M:%S %Y GMT")
                                v = datetime_str
                    else:
                        v = ""
                details = {
                    "statKey": f"HRM {data_type} Status|{k.lower()}",
                    "timestamps": [int(timestamp * 1000)],
                    "values": [v]
                }
                metrics_payload["stat-content"].append(details)
                if k.lower() == 'alert':
                    details = {
                        "statKey": f"HRM {data_type} Status|alert_code",
                        "timestamps": [int(timestamp * 1000)],
                        "data": [self.codes[v.lower()]] if v.lower() in self.codes else [self.codes['NA']]
                    }
                    metrics_payload["stat-content"].append(details)

            self.logger.info(resource_id)
            self.logger.info(metrics_payload)
            self.build_payload(category, resource_id, hostname, metrics_payload)
            update_count = update_count + 1

        self.logger.info(f'Total statKeys = {update_count} ')

    @push_handler
    def push_vm_connected_cdrom_status(self, file_name):
        data_arr = self.read_data(file_name)
        if data_arr is None:
            self.logger.info("Skipping VM connected CD-ROM status: could not read file")
            return
        category = "HRM VM with Connected CD-ROMs Status"
        data_type = "VM Connected CDROM"
        self.logger.info(f'Pushing {data_type} status data to VMware Aria Operations')
        update_count = 0
        for data in data_arr:
            hostname = data["vCenter Server"]
            resource_name = data["VM Name"]
            self.logger.info(f'Resource: {resource_name} that recides on hostname {hostname}')
            metrics_payload = {"stat-content": []}

            # timestamp_raw = value['timestamp']
            timestamp = time.mktime(datetime.datetime.now().timetuple())

            resource_id = self.get_resource_id(hostname, resource_name)

            for k, v in data.items():
                if k.lower() == 'latest':
                    k = "date_taken"
                    if v:
                        match = re.search(r"\d{10}", v)
                        if match:
                            timestamp_raw = int(match.group())
                            parsed_date = datetime.datetime.fromtimestamp(timestamp_raw)
                            datetime_str = parsed_date.strftime("%b %d %H:%M:%S %Y GMT")
                            v = datetime_str
                        else:
                            match = re.search("\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", v)
                            if match:
                                date_format = "%Y-%m-%dT%H:%M:%S"
                                parsed_date = datetime.datetime.strptime(match.group(), date_format)
                                datetime_str = parsed_date.strftime("%b %d %H:%M:%S %Y GMT")
                                v = datetime_str
                    else:
                        v = ""
                details = {
                    "statKey": f"HRM {data_type} Status|{k.lower()}",
                    "timestamps": [int(timestamp * 1000)],
                    "values": [v]
                }
                metrics_payload["stat-content"].append(details)
                if k.lower() == 'alert':
                    details = {
                        "statKey": f"HRM {data_type} Status|alert_code",
                        "timestamps": [int(timestamp * 1000)],
                        "data": [self.codes[v.lower()]] if v.lower() in self.codes else [self.codes['NA']]
                    }
                    metrics_payload["stat-content"].append(details)

            self.logger.info(resource_id)
            self.logger.info(metrics_payload)
            self.build_payload(category, resource_id, hostname, metrics_payload)
            update_count = update_count + 1

        self.logger.info(f'Total statKeys = {update_count} ')

    @push_handler
    def push_localuserexpiry_status(self, file_name):
        data_type = "Localuserexpiry"
        path = self.get_complete_json_file_name(file_name)
        if not path:
            self.logger.info(f"Skipping local user expiry: file not found ending with {file_name}")
            return
        data_arr = self.read_data(path)
        if data_arr is None:
            self.logger.info(f"Skipping local user expiry: could not read {path}")
            return
        update_count = self.push_flat_struct(data_type, data_arr, None)
        self.logger.info(f'Total statKeys = {update_count} ')

    @push_handler
    def push_componentconnectivityhealth_status(self, file_name):
        data_type = "ComponentConnectivityHealth"
        datastruct = self.read_data(file_name)
        if datastruct is None:
            self.logger.info("Skipping component connectivity: could not read file")
            return
        update_count = self.push_flat_struct(data_type, datastruct, None)
        self.logger.info(f'Total statKeys = {update_count} ')

    @push_handler
    def push_storagecapacityhealth_status(self, file_name):
        update_count = 0
        datastruct = self.read_data(file_name)
        if datastruct is None:
            self.logger.info("Skipping storage capacity health: could not read file")
            return

        for key_val in datastruct.keys():
            data_arr = datastruct[key_val]
            if key_val == "esxi":
                key_var = "ESXi FQDN"
                category = "HRM StorageCapacityHealth Status"
                data_type = "StorageCapacityHealth"
                for arr_val in data_arr:
                    data = arr_val
                    hostname = data[key_var]
                    resource_name = hostname.split(".")[0]
                    self.logger.info(f"hostname = {hostname}")
                    metrics_payload = {"stat-content": []}

                    # timestamp_raw = value['timestamp']
                    timestamp = time.mktime(datetime.datetime.now().timetuple())
                    resource_id = self.get_resource_id(hostname, resource_name)

                    for k, v in arr_val.items():
                        index_val = arr_val["Volume Name"]
                        statkey = f"HRM {data_type} Status:{index_val}"
                        details = {
                            "statKey": f"{statkey}|{k.lower()}",
                            "timestamps": [int(timestamp * 1000)],
                            "values": [v]
                        }
                        metrics_payload["stat-content"].append(details)
                        if k.lower() == 'alert':
                            details = {
                                "statKey": f"{statkey}|alert_code",
                                "timestamps": [int(timestamp * 1000)],
                                "data": [self.codes[v.lower()]] if v.lower() in self.codes else [self.codes['NA']]
                            }
                            metrics_payload["stat-content"].append(details)

                    self.logger.info(resource_id)
                    self.logger.info(metrics_payload)
                    self.build_payload(category, resource_id, hostname, metrics_payload)
                    update_count = update_count + 1

            elif key_val == "datastore":
                key_var = "vCenter Server"
                category = "HRM StorageCapacityDatastoreHealth Status"
                data_type = "StorageCapacityDatastoreHealth"
                for arr_val in data_arr:
                    data = arr_val
                    hostname = data[key_var]
                    resource_name = hostname.split(".")[0]
                    self.logger.info(f"hostname = {hostname}")
                    metrics_payload = {"stat-content": []}

                    timestamp = time.mktime(datetime.datetime.now().timetuple())
                    resource_id = self.get_resource_id(hostname, resource_name)

                    for k, v in arr_val.items():
                        index_val = arr_val["Datastore Name"]
                        statkey = f"HRM {data_type} Status:{index_val}"
                        details = {
                            "statKey": f"{statkey}|{k.lower()}",
                            "timestamps": [int(timestamp * 1000)],
                            "values": [v]
                        }
                        metrics_payload["stat-content"].append(details)
                        if k.lower() == 'alert':
                            details = {
                                "statKey": f"{statkey}|alert_code",
                                "timestamps": [int(timestamp * 1000)],
                                "data": [self.codes[v.lower()]] if v.lower() in self.codes else [self.codes['NA']]
                            }
                            metrics_payload["stat-content"].append(details)

                    self.logger.info(resource_id)
                    self.logger.info(metrics_payload)
                    self.build_payload(category, resource_id, hostname, metrics_payload)
                    update_count = update_count + 1
            else:
                key_var = "FQDN"
                category = "HRM StorageCapacityFilesystemHealth Status"
                data_type = "StorageCapacityFilesystemHealth"
                for arr_val in data_arr:
                    data = arr_val
                    index_val = data["Filesystem"]
                    hostname = data[key_var]
                    resource_name = hostname.split(".")[0]
                    self.logger.info(f"hostname = {hostname}")
                    metrics_payload = {"stat-content": []}

                    timestamp = time.mktime(datetime.datetime.now().timetuple())
                    resource_id = self.get_resource_id(hostname, resource_name)
                    for k, v in arr_val.items():
                        statkey = f"HRM {data_type} Status:{index_val}"
                        details = {
                            "statKey": f"{statkey}|{k.lower()}",
                            "timestamps": [int(timestamp * 1000)],
                            "values": [v]
                        }
                        metrics_payload["stat-content"].append(details)
                        if k.lower() == 'alert':
                            details = {
                                "statKey": f"{statkey}|alert_code",
                                "timestamps": [int(timestamp * 1000)],
                                "data": [self.codes[v.lower()]] if v.lower() in self.codes else [self.codes['NA']]
                            }
                            metrics_payload["stat-content"].append(details)

                    self.logger.info(resource_id)
                    self.logger.info(metrics_payload)
                    self.build_payload(category, resource_id, hostname, metrics_payload)

                    update_count = update_count + 1
        self.logger.info(f'Total statKeys = {update_count} ')

    @push_handler
    def push_esxi_connection_health(self, file_name):
        data_arr = self.read_data(file_name)
        if data_arr is None:
            self.logger.info("Skipping ESXi connection health: could not read file")
            return
        category = "HRM ESXi Connection Health"
        data_type = "ESXi Connection Health"
        self.logger.info(f'Pushing {data_type} status data to VMware Aria Operations')
        update_count = 0
        for data in data_arr:
            component = data["Component"]
            hostname = data["Resource"]
            resource_name = hostname.split(".")[0]
            self.logger.info(f'Hostname: {hostname}, Component: {component}')
            metrics_payload = {"stat-content": []}

            timestamp = time.mktime(datetime.datetime.now().timetuple())
            resource_id = self.get_resource_id(hostname, resource_name)

            for k, v in data.items():
                if k.lower() == 'latest':
                    k = "date_taken"
                    if v:
                        match = re.search(r"\d{10}", v)
                        if match:
                            timestamp_raw = int(match.group())
                            parsed_date = datetime.datetime.fromtimestamp(timestamp_raw)
                            datetime_str = parsed_date.strftime("%b %d %H:%M:%S %Y GMT")
                            v = datetime_str
                        else:
                            match = re.search("\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", v)
                            if match:
                                date_format = "%Y-%m-%dT%H:%M:%S"
                                parsed_date = datetime.datetime.strptime(match.group(), date_format)
                                datetime_str = parsed_date.strftime("%b %d %H:%M:%S %Y GMT")
                                v = datetime_str
                    else:
                        v = ""
                details = {
                    "statKey": f"HRM {data_type} Status|{k.lower()}",
                    "timestamps": [int(timestamp * 1000)],
                    "values": [v]
                }
                metrics_payload["stat-content"].append(details)
                if k.lower() == 'alert':
                    details = {
                        "statKey": f"HRM {data_type} Status|alert_code",
                        "timestamps": [int(timestamp * 1000)],
                        "data": [self.codes[v.lower()]] if v.lower() in self.codes else [self.codes['NA']]
                    }
                    metrics_payload["stat-content"].append(details)

            self.logger.info(resource_id)
            self.logger.info(metrics_payload)
            self.build_payload(category, resource_id, hostname, metrics_payload)
            update_count = update_count + 1

        self.logger.info(f'Total statKeys = {update_count} ')

    def _push_free_pool_empty_summary(self, category, data_type):
        """Publish a single summary when the free pool has no ESXi rows (valid empty state, not an error)."""
        resource_id = (
            self.resource_inventory.get(self.sddc_manager_fqdn)
            or self.resource_inventory.get(self.sddc_manager_fqdn.split(".")[0])
        )
        if not resource_id:
            self.logger.warning(
                "SDDC Manager free pool is empty but no VMware Aria Operations resource id was found for SDDC Manager; "
                "cannot push empty-pool summary."
            )
            return
        timestamp = time.mktime(datetime.datetime.now().timetuple())
        empty_message = "No ESXi hosts present in the free pool."
        metrics_payload = {
            "stat-content": [
                {
                    "statKey": f"HRM {data_type} Status|summary",
                    "timestamps": [int(timestamp * 1000)],
                    "values": [empty_message],
                },
                {
                    "statKey": f"HRM {data_type} Status|alert",
                    "timestamps": [int(timestamp * 1000)],
                    "values": ["GREEN"],
                },
                {
                    "statKey": f"HRM {data_type} Status|alert_code",
                    "timestamps": [int(timestamp * 1000)],
                    "data": [0],
                },
            ]
        }
        self.build_payload(category, resource_id, self.sddc_manager_fqdn, metrics_payload)
        self.logger.info(
            f"Pushed free pool empty-state summary to SDDC Manager object ({len(metrics_payload['stat-content'])} stats)."
        )

    @push_handler
    def push_sddc_manager_free_pool_status(self, file_name):
        data_arr = self.read_data(file_name)
        if data_arr is None:
            self.logger.info("Skipping SDDC Manager free pool status: could not read file")
            return
        if not isinstance(data_arr, list):
            self.logger.info(
                f"SDDC Manager free pool: JSON is not an array (type={type(data_arr).__name__}); file: {file_name}. "
                f"Treating as no per-host rows."
            )
            data_arr = []
        category = "HRM Free Pool Health"
        data_type = "SDDC Free Pool"
        if len(data_arr) == 0:
            self.logger.info(
                "SDDC Manager free pool: JSON array is empty (no ESXi rows). "
                "This is expected when the free pool has no hosts (e.g. report text 'No ESXi hosts present in the free pool.')."
            )
            self._push_free_pool_empty_summary(category, data_type)
            return
        self.logger.info(f'Pushing {data_type} status data to VMware Aria Operations')
        update_count = 0
        for data in data_arr:
            if not isinstance(data, dict):
                self.logger.info(f"Skipping free pool row: expected object, got {type(data).__name__}.")
                continue
            component = data.get("Component")
            hostname = data.get("ESXi FQDN")
            if not hostname:
                self.logger.info(f"Skipping free pool row with missing ESXi FQDN: {data!r}.")
                continue
            resource_name = hostname.split(".")[0]
            self.logger.info(f'Hostname: {hostname}, Component: {component}')
            metrics_payload = {"stat-content": []}

            timestamp = time.mktime(datetime.datetime.now().timetuple())
            resource_id = self.get_resource_id(hostname, resource_name)

            for k, v in data.items():
                if k.lower() == 'latest':
                    k = "date_taken"
                    if v:
                        match = re.search(r"\d{10}", v)
                        if match:
                            timestamp_raw = int(match.group())
                            parsed_date = datetime.datetime.fromtimestamp(timestamp_raw)
                            datetime_str = parsed_date.strftime("%b %d %H:%M:%S %Y GMT")
                            v = datetime_str
                        else:
                            match = re.search("\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", v)
                            if match:
                                date_format = "%Y-%m-%dT%H:%M:%S"
                                parsed_date = datetime.datetime.strptime(match.group(), date_format)
                                datetime_str = parsed_date.strftime("%b %d %H:%M:%S %Y GMT")
                                v = datetime_str
                    else:
                        v = ""
                details = {
                    "statKey": f"HRM {data_type} Status|{k.lower()}",
                    "timestamps": [int(timestamp * 1000)],
                    "values": [v]
                }
                metrics_payload["stat-content"].append(details)
                if k.lower() == 'alert':
                    details = {
                        "statKey": f"HRM {data_type} Status|alert_code",
                        "timestamps": [int(timestamp * 1000)],
                        "data": [self.codes[v.lower()]] if v.lower() in self.codes else [self.codes['NA']]
                    }
                    metrics_payload["stat-content"].append(details)

            self.logger.info(resource_id)
            self.logger.info(metrics_payload)
            self.build_payload(category, resource_id, hostname, metrics_payload)
            update_count = update_count + 1

        self.logger.info(f'Total statKeys = {update_count} ')

    @push_handler
    def push_nsxttier0_status(self, file_name):
        data_arr = self.read_data(file_name)
        if data_arr is None:
            self.logger.info("Skipping NSXT Tier0 BGP status: could not read file")
            return
        category = "HRM NSXT TIER0 BGP Backup Status"
        self.logger.info('Pushing nsxt tier0 bgp status data to VMware Aria Operations')
        update_count = 0
        instance_hash = {}

        for arr_val in data_arr:
            instance_name = arr_val["NSX Manager"]
            instance_hash[instance_name] = 0

        for arr_val in data_arr:
            data = arr_val
            hostname = data["NSX Manager"]
            resource_name = hostname.split(".")[0]
            self.logger.info(f"hostname = {hostname}")
            metrics_payload = {"stat-content": []}
            timestamp = time.mktime(datetime.datetime.now().timetuple())
            resource_id = self.get_resource_id(hostname, resource_name)

            for k, v in arr_val.items():
                details = {
                    "statKey": f"{category}:{instance_hash[hostname]}|{k.lower()}",
                    "timestamps": [int(timestamp * 1000)],
                    "values": [v]
                }
                metrics_payload["stat-content"].append(details)
                if k.lower() == 'alert':
                    details = {
                        "statKey": f"{category}:{instance_hash[hostname]}|alert_code",
                        "timestamps": [int(timestamp * 1000)],
                        "data": [self.codes[v.lower()]] if v.lower() in self.codes else [self.codes['NA']]
                    }
                    metrics_payload["stat-content"].append(details)

            self.logger.info(resource_id)
            self.logger.info(metrics_payload)
            self.build_payload(category, resource_id, hostname, metrics_payload)
            update_count = update_count + 1
            instance_hash[hostname] += 1

        self.logger.info(f'Total statKeys = {update_count} ')

    @push_handler
    def push_nsxt_transportnode_status(self, file_name):
        data_arr = self.read_data(file_name)
        if data_arr is None:
            self.logger.info("Skipping NSXT transport node status: could not read file")
            return
        category = "HRM NSXT Transport Node Status"
        self.logger.info('Pushing nsxt transport node status data to VMware Aria Operations')
        update_count = 0
        instance_hash = {}

        for arr_val in data_arr:
            instance_name = arr_val["Resource"]
            instance_hash[instance_name] = 0

        for arr_val in data_arr:
            data = arr_val
            hostname = data["Resource"]
            resource_name = hostname.split(".")[0]
            self.logger.info(f"hostname = {hostname}")
            metrics_payload = {"stat-content": []}

            # timestamp_raw = value['timestamp']
            timestamp = time.mktime(datetime.datetime.now().timetuple())
            resource_id = self.get_resource_id(hostname, resource_name)

            for k, v in arr_val.items():
                details = {
                    "statKey": f"HRM NSXT Transport Node Status:{instance_hash[hostname]}|{k.lower()}",
                    "timestamps": [int(timestamp * 1000)],
                    "values": [v]
                }
                metrics_payload["stat-content"].append(details)
                if k.lower() == 'alert':
                    details = {
                        "statKey": f"HRM NSXT Transport Node Status:{instance_hash[hostname]}|alert_code",
                        "timestamps": [int(timestamp * 1000)],
                        "data": [self.codes[v.lower()]] if v.lower() in self.codes else [self.codes['NA']]
                    }
                    metrics_payload["stat-content"].append(details)

            self.logger.info(resource_id)
            self.logger.info(metrics_payload)
            self.build_payload(category, resource_id, hostname, metrics_payload)
            update_count = update_count + 1
            instance_hash[hostname] += 1

        self.logger.info(f'Total statKeys = {update_count} ')

    @push_handler
    def push_nsxt_tunnel_status(self, file_name):
        data_arr = self.read_data(file_name)
        if data_arr is None:
            self.logger.info("Skipping NSXT tunnel status: could not read file")
            return
        category = "HRM NSXT Tunnel Status"
        self.logger.info('Pushing nsxt tunnel status data to VMware Aria Operations')
        update_count = 0
        instance_hash = {}

        for arr_val in data_arr:
            instance_name = arr_val["Resource"]
            instance_hash[instance_name] = 0

        for arr_val in data_arr:
            data = arr_val
            hostname = data["Resource"]
            resource_name = hostname.split(".")[0]
            self.logger.info(f"hostname = {hostname}")
            metrics_payload = {"stat-content": []}

            # timestamp_raw = value['timestamp']
            timestamp = time.mktime(datetime.datetime.now().timetuple())
            resource_id = self.get_resource_id(hostname, resource_name)

            for k, v in arr_val.items():
                details = {
                    "statKey": f"HRM NSXT Tunnel Status:{instance_hash[hostname]}|{k.lower()}",
                    "timestamps": [int(timestamp * 1000)],
                    "values": [v]
                }
                metrics_payload["stat-content"].append(details)
                if k.lower() == 'alert':
                    details = {
                        "statKey": f"HRM NSXT Tunnel Status:{instance_hash[hostname]}|alert_code",
                        "timestamps": [int(timestamp * 1000)],
                        "data": [self.codes[v.lower()]] if v.lower() in self.codes else [self.codes['NA']]
                    }
                    metrics_payload["stat-content"].append(details)

            self.logger.info(resource_id)
            self.logger.info(metrics_payload)
            self.build_payload(category, resource_id, hostname, metrics_payload)
            update_count = update_count + 1
            instance_hash[hostname] += 1

        self.logger.info(f'Total statKeys = {update_count} ')

    @push_handler
    def push_nsxtcombinedhealthnonsos_status(self, file_name):
        data_arr = self.read_data(file_name)
        if data_arr is None:
            self.logger.info("Skipping NSXT combined health: could not read file")
            return
        category = "HRM NSXTCombinedHealth Status"
        self.logger.info('Pushing NSXT Combined Health Status data to VMware Aria Operations')
        update_count = 0
        instance_hash = {}

        for arr_val in data_arr:
            instance_name = arr_val["Resource"]
            instance_hash[instance_name] = 0

        for arr_val in data_arr:
            data = arr_val
            component = data["Component"]
            hostname = data["Resource"]
            resource_name = hostname.split(".")[0]
            self.logger.info(f"hostname = {hostname} and component = {component}")
            metrics_payload = {"stat-content": []}

            timestamp = time.mktime(datetime.datetime.now().timetuple())
            resource_id = self.get_resource_id(hostname, resource_name)

            for k, v in arr_val.items():
                details = {
                    "statKey": f"HRM NSXTCombinedHealth Status:{instance_hash[hostname]}|{k.lower()}",
                    "timestamps": [int(timestamp * 1000)],
                    "values": [v]
                }
                metrics_payload["stat-content"].append(details)
                if k.lower() == 'alert':
                    details = {
                        "statKey": f"HRM NSXTCombinedHealth Status:{instance_hash[hostname]}|alert_code",
                        "timestamps": [int(timestamp * 1000)],
                        "data": [self.codes[v.lower()]] if v.lower() in self.codes else [self.codes['NA']]
                    }
                    metrics_payload["stat-content"].append(details)

            self.logger.info(resource_id)
            self.logger.info(metrics_payload)
            self.build_payload(category, resource_id, hostname, metrics_payload)
            update_count = update_count + 1
            instance_hash[hostname] += 1

        self.logger.info(f'Total statKeys = {update_count} ')

    @push_handler
    def push_data_certificates(self):
        category = "Certificates"
        self.logger.info('Pushing SOS Certificate Health data to VMware Aria Operations')
        update_count = 0
        for _key, value in self.data[category]['Certificate Status'].items():
            for hostname, cert_data in value.items():
                resource_name = hostname.split(".")[0]
                self.logger.info(f"hostname = {hostname}")

                resource_id = self.get_resource_id(hostname, resource_name)

                metrics_payload = {"stat-content": []}

                timestamp_raw = cert_data['timestamp']
                timestamp = time.mktime(datetime.datetime.strptime(timestamp_raw, "%c").timetuple())
                expires_in = 0
                if cert_data['title'] and "-" not in cert_data['title']:
                    details = {
                        "statKey": "SOS Certificate Health Summary|fqdn",
                        "timestamps": [int(timestamp * 1000)],
                        "values": [cert_data['title'][0]]
                    }
                    metrics_payload["stat-content"].append(details)
                    details = {
                        "statKey": "SOS Certificate Health Summary|issue_date",
                        "timestamps": [int(timestamp * 1000)],
                        "values": [cert_data['title'][1]]
                    }
                    metrics_payload["stat-content"].append(details)

                    details = {
                        "statKey": "SOS Certificate Health Summary|expiry_date",
                        "timestamps": [int(timestamp * 1000)],
                        "values": [cert_data['title'][2]]
                    }
                    metrics_payload["stat-content"].append(details)
                    current = datetime.datetime.now()
                    expiry = datetime.datetime.strptime(cert_data['title'][2], "%b %d %H:%M:%S %Y %Z")
                    expires_in = (expiry - current).days

                    details = {
                        "statKey": "SOS Certificate Health Summary|expires_in",
                        "timestamps": [int(timestamp * 1000)],
                        "data": [int(expires_in)]
                    }
                    metrics_payload["stat-content"].append(details)

                details = {
                    "statKey": "SOS Certificate Health Summary|state",
                    "timestamps": [int(timestamp * 1000)],
                    "values": [cert_data['state']]
                }
                metrics_payload["stat-content"].append(details)
                details = {
                    "statKey": "SOS Certificate Health Summary|message",
                    "timestamps": [int(timestamp * 1000)],
                    "values": [cert_data['message']]
                }
                metrics_payload["stat-content"].append(details)

                alert = "GREEN"
                if int(expires_in) <= 15:
                    alert = "RED"
                elif 15 < int(expires_in) <= 30:
                    alert = "YELLOW"

                details = {
                    "statKey": "SOS Certificate Health Summary|alert",
                    "timestamps": [int(timestamp * 1000)],
                    "values": [alert]
                }
                metrics_payload["stat-content"].append(details)
                details = {
                    "statKey": "SOS Certificate Health Summary|alert_code",
                    "timestamps": [int(timestamp * 1000)],
                    "data": [self.codes[alert.lower()]]
                }
                metrics_payload["stat-content"].append(details)

                self.logger.info(resource_id)
                self.logger.info(metrics_payload)
                self.build_payload(category, resource_id, hostname, metrics_payload)
                update_count = update_count + 1

        self.logger.info(f'Total statKeys = {update_count}')

    @push_handler
    def push_sddc_versions(self):
        category = "Version Check Status"
        self.logger.info(
            'Pushing SOS version health check of BOM components (vCenter Server, NSX, ESXi, and SDDC Manager) data to VMware Aria Operations')
        update_count = 0
        for key, value in self.data[category].items():
            hostname = key.rstrip()
            resource_name = hostname.split(".")[0]
            self.logger.info(f'Hostname = {hostname}')
            metrics_payload = {"stat-content": []}

            timestamp_raw = value['timestamp']
            timestamp = time.mktime(datetime.datetime.strptime(timestamp_raw, "%c").timetuple())
            resource_id = self.get_resource_id(hostname, resource_name)

            for k, val in value.items():
                if k == 'title' and val:
                    try:
                        title_value = val[0] if isinstance(val[0], str) else val[0][0]
                    except Exception as e:
                        title_value = f"Unable to get the version. Please check the logs. {e}"

                    details = {
                        "statKey": f"SOS Version Health Summary|{k}",
                        "timestamps": [int(timestamp * 1000)],
                        "values": [title_value]
                    }
                else:
                    details = {
                        "statKey": f"SOS Version Health Summary|{k}",
                        "timestamps": [int(timestamp * 1000)],
                        "values": [val]
                    }

                metrics_payload["stat-content"].append(details)

                if k == 'alert':
                    details = {
                        "statKey": "SOS Version Health Summary|alert_code",
                        "timestamps": [int(timestamp * 1000)],
                        "data": [self.codes[val.lower()]]
                    }
                    metrics_payload["stat-content"].append(details)

            self.logger.info(resource_id)
            self.logger.info(metrics_payload)
            self.build_payload(category, resource_id, hostname, metrics_payload)
            update_count = update_count + 1

        self.logger.info(f'Total statKeys = {update_count}')


if __name__ == "__main__":
    parser = argparse.ArgumentParser(formatter_class=argparse.RawTextHelpFormatter)

    parser.add_argument("-e", "--env-json", default='env.json',
                        help="path of env.json file. Default is in same directory as this file")
    parser.add_argument('-p', '--push-data', dest='push_data', action='store_true',
                        help="This is default option to send the data to VMware Aria Operations")
    parser.add_argument('-np', '--no-push-data', dest='push_data', action='store_false',
                        help="Use this option if you do not want to send the data to VMware Aria Operations")
    parser.set_defaults(push_data=True)

    args = parser.parse_args()
    pd = PushDataVrops(args)
    pd.get_list_of_workload_domain()

    lockFile = os.path.join(pd.logger.test_log_folder, ".hrm-sos-run.lock") if pd.sos_use_run_lock else None
    if lockFile and pd.sos_use_run_lock:
        if os.path.exists(lockFile):
            ageHours = (time.time() - os.path.getmtime(lockFile)) / 3600
            if ageHours < pd.sos_lock_max_hours:
                pd.logger.warning(
                    f"Another HRM run may be in progress (lock file exists, age {ageHours:.1f}h). "
                    f"Exiting to avoid conflicts. Adjust schedule or lock_max_hours in env.json if needed."
                )
                raise SystemExit(1)
            pd.logger.info(f"Removing stale lock file (age {ageHours:.1f}h).")
            try:
                os.remove(lockFile)
            except OSError:
                pass
        try:
            Path(lockFile).touch()
        except OSError:
            pd.logger.warning("Could not create run lock file; continuing anyway.")

    pd.data = None
    try:
        for domainItem in pd.domain_list:
            domainData = pd.get_sos_data_from_sddc_manager(domain=domainItem)
            pd.data = pd._merge_health_results(pd.data, domainData)
        if pd.data:
            pd.push_data()
        else:
            pd.logger.warning("No SOS health data collected; skipping push to VMware Aria Operations.")
    finally:
        if lockFile and os.path.exists(lockFile):
            try:
                os.remove(lockFile)
            except OSError:
                pass
