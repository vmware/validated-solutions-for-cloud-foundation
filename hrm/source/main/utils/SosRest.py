# Copyright 2023-2026 Broadcom. All Rights Reserved.
# SPDX-License-Identifier: BSD-2
#
# Description:
# Runs health checks on SDDC Manager using SOS Utility and get the data.
# Saves the results as health-results.json in test log directory.

import glob
import os
import re
import shutil
import tarfile
import time
from pathlib import Path

import requests
import urllib3

# HTTP client timeouts (connect seconds, read seconds) for SDDC Manager API calls.
CONNECT_TIMEOUT_SECONDS = 10
READ_TIMEOUT_TOKEN_SECONDS = 90
READ_TIMEOUT_API_SECONDS = 120
READ_TIMEOUT_STREAM_SECONDS = 3600

_TOKEN_TIMEOUT = (CONNECT_TIMEOUT_SECONDS, READ_TIMEOUT_TOKEN_SECONDS)
_API_TIMEOUT = (CONNECT_TIMEOUT_SECONDS, READ_TIMEOUT_API_SECONDS)
_STREAM_TIMEOUT = (CONNECT_TIMEOUT_SECONDS, READ_TIMEOUT_STREAM_SECONDS)

# SoS bundle status polling (seconds between polls, max total wait).
POLL_INTERVAL_SECONDS = 30
POLL_MAX_ELAPSED_SECONDS_DEFAULT = 4 * 3600

# Download chunk size for streaming bundle body (bytes).
BUNDLE_DOWNLOAD_CHUNK_BYTES = 65536


def vcf_version_is_5_2_or_later(vcf_version):
    """Return True if vcf_version string is VMware Cloud Foundation 5.2 or later (numeric major.minor)."""
    if vcf_version is None:
        return False
    text = str(vcf_version).strip()
    match = re.match(r"^(\d+)\.(\d+)", text)
    if not match:
        return False
    major = int(match.group(1))
    minor = int(match.group(2))
    return (major, minor) >= (5, 2)


class SosRest(object):

    def __init__(
        self,
        host,
        user,
        password,
        domain=None,
        logger=None,
        force=False,
        include_free_hosts=False,
        poll_max_elapsed_seconds=None,
    ):
        self.host = host
        self.user = user
        self.password = password
        self.domain = domain
        self.logger = logger
        self.force = force
        self.include_free_hosts = include_free_hosts
        if poll_max_elapsed_seconds is None:
            self.poll_max_elapsed_seconds = POLL_MAX_ELAPSED_SECONDS_DEFAULT
        else:
            self.poll_max_elapsed_seconds = max(1, int(poll_max_elapsed_seconds))
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        self.token = self.get_auth_token()

        if not self.token:
            raise RuntimeError("Unable to get Authorization token from SDDC manager")

    def _safe_response_json(self, response, context):
        """Parse JSON response body or log a snippet and raise ValueError."""
        try:
            return response.json()
        except ValueError:
            snippet = (getattr(response, "text", None) or "")[:2000]
            self.log_msg(f"{context}: response is not JSON (HTTP {response.status_code}): {snippet}")
            raise ValueError(f"{context}: response body is not valid JSON.") from None

    def get_auth_token(self):
        headers = {
            'Content-Type': 'application/json',
        }

        json_data = {
            'username': self.user,
            'password': self.password,
        }

        # verify=False: SDDC Manager often uses an enterprise or appliance CA; import the CA bundle to harden TLS.
        response = requests.post(
            f'https://{self.host}/v1/tokens',
            headers=headers,
            json=json_data,
            verify=False,  # nosec B501
            timeout=_TOKEN_TIMEOUT,
        )
        token = None
        try:
            res = self._safe_response_json(response, "get_auth_token")
        except ValueError:
            return None
        if response.status_code == 200:
            token = res.get("accessToken")
        return token

    def start_health_checks_op(self, vcf_version=None):

        headers = {
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {self.token}'
        }

        # Use scope per domain; do not use includeAllDomains to avoid unexpected results.
        if self.domain is None:
            domain_name_string = "\"\""
        else:
            domain_name_string = self.domain

        if vcf_version_is_5_2_or_later(vcf_version):
            json_data = {
                "healthChecks": {
                    "certificateHealth": True,
                    "computeHealth": True,
                    "connectivityHealth": True,
                    "dnsHealth": True,
                    "generalHealth": True,
                    "hardwareCompatibilityHealth": True,
                    "ntpHealth": True,
                    "passwordHealth": True,
                    "servicesHealth": True,
                    "storageHealth": True,
                    "versionHealth": True
                },
                "options": {
                    "config": {
                        "force": self.force,
                        "skipKnownHostCheck": True
                    },
                    "include": {
                        "precheckReport": False,
                        "summaryReport": True
                    }
                },
                "scope": {
                    "domains": [{
                        "clusterNames": [""],
                        "domainName": domain_name_string
                    }],
                    "includeAllDomains": False,
                    "includeFreeHosts": self.include_free_hosts
                }
            }
        else:
            json_data = {
                "healthChecks": {
                    "certificateHealth": True,
                    "composabilityHealth": True,
                    "computeHealth": True,
                    "connectivityHealth": True,
                    "dnsHealth": True,
                    "generalHealth": True,
                    "hardwareCompatibilityHealth": True,
                    "ntpHealth": True,
                    "passwordHealth": True,
                    "servicesHealth": True,
                    "storageHealth": True,
                    "versionHealth": True
                },
                "options": {
                    "config": {
                        "force": self.force,
                        "skipKnownHostCheck": True
                    },
                    "include": {
                        "summaryReport": True
                    }
                },
                "scope": {
                    "domains": [{
                        "clusterNames": [""],
                        "domainName": domain_name_string
                    }],
                    "includeAllDomains": False,
                    "includeFreeHosts": self.include_free_hosts
                }
            }

        response = requests.post(
            f'https://{self.host}/v1/system/health-summary',
            headers=headers,
            json=json_data,
            verify=False,  # nosec B501
            timeout=_API_TIMEOUT,
        )
        operation_id = None
        try:
            res = self._safe_response_json(response, "start_health_checks_op")
        except ValueError:
            return None
        self.log_msg(res)
        self.log_msg(f'status code {response.status_code}')
        if response.status_code == 202:
            operation_id = res.get("id")
        if response.status_code == 409:
            raise RuntimeError(f"SOS Utility operation in progress - {res}")
        return operation_id

    def log_msg(self, msg):
        if self.logger:
            self.logger.info(msg)
        else:
            print(msg)

    @staticmethod
    def _validate_tar_members_before_extract(tar, extract_path):
        """Reject archive members that would extract outside extract_path."""
        extract_root = Path(extract_path).resolve()
        for member in tar.getmembers():
            if os.path.isabs(member.name):
                raise ValueError(f"Refusing absolute path in health bundle: {member.name!r}")
            destination = (extract_root / member.name).resolve()
            if destination != extract_root and extract_root not in destination.parents:
                raise ValueError(f"Refusing path outside bundle root in health bundle: {member.name!r}")

    def get_health_checks_status(self, operation_id):
        headers = {
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {self.token}'
        }

        deadline = time.monotonic() + self.poll_max_elapsed_seconds
        res = {}
        while True:
            if time.monotonic() > deadline:
                raise TimeoutError(
                    f"SDDC health-summary operation {operation_id!r} did not finish within "
                    f"{self.poll_max_elapsed_seconds} seconds."
                )
            time.sleep(POLL_INTERVAL_SECONDS)
            response = requests.get(
                f'https://{self.host}/v1/system/health-summary/{operation_id}',
                headers=headers,
                verify=False,  # nosec B501
                timeout=_API_TIMEOUT,
            )
            res = self._safe_response_json(response, "get_health_checks_status")

            self.log_msg(response)
            status = res.get("status")
            if status == 'IN_PROGRESS':
                self.log_msg('Health check bundle creation in progress...')
                self.log_msg('Please wait for it to complete')
                self.log_msg(f'Polling again in {POLL_INTERVAL_SECONDS} seconds')
                continue
            if status == 'COMPLETED_WITH_SUCCESS':
                self.log_msg(f'Bundle created successfully - {res}')
                break
            self.log_msg(f'Unexpected status code - response is - {res}')
            break
        return res

    def get_health_check_bundle(self, operation_id, path=None):
        headers = {
            'Content-Type': 'application/octet-stream',
            'Authorization': f'Bearer {self.token}'
        }

        response = requests.get(
            f'https://{self.host}/v1/system/health-summary/{operation_id}/data',
            headers=headers,
            verify=False,  # nosec B501
            stream=True,
            timeout=_STREAM_TIMEOUT,
        )

        if self.logger:
            self.logger.info(f'bundle response code - {response.status_code}')
        else:
            self.log_msg(f'bundle response code - {response.status_code}')

        if response.status_code != 200:
            err_body = (getattr(response, "text", None) or "")[:2000]
            self.log_msg(f"Bundle download failed (HTTP {response.status_code}): {err_body}")
            return

        if path:
            if os.path.exists(path):
                file_path = os.path.join(path, f'health-data-{operation_id}.tar')
            else:
                raise ValueError(f'Invalid location - {path} or folder doesnt exist')
        else:
            file_path = f'health-data-{operation_id}.tar'

        with open(file_path, 'wb') as f:
            for chunk in response.iter_content(chunk_size=BUNDLE_DOWNLOAD_CHUNK_BYTES):
                if chunk:
                    f.write(chunk)

        if os.path.exists(file_path):
            self.log_msg('Extracting contents of bundle')
            self.get_health_check_json_from_tar(file_path, path)

    def get_health_check_json_from_tar(self, absfname, path=None):
        fname = Path(absfname).name
        foldername = fname.rstrip(".tar")
        dst = path
        if path is None:
            path = os.getcwd()
        path = os.path.join(path, foldername)
        filepath = os.path.join(path, '**')
        filepath = os.path.join(filepath, 'health-results.json')
        tar = tarfile.open(absfname, "r:*")
        try:
            SosRest._validate_tar_members_before_extract(tar, path)
            tar.extractall(path)  # nosec B202  # Validated by _validate_tar_members_before_extract; trusted SDDC Manager bundle.
        finally:
            tar.close()

        matches = glob.glob(filepath, recursive=True)
        if not matches:
            self.log_msg(f"health-results.json file doesn't exist in {fname}")
            return
        health_results_path = matches[0]
        if len(matches) > 1:
            self.log_msg(f"Multiple health-results.json files found; using {health_results_path}")
        self.log_msg(f"health-results.json file exists in the path {health_results_path}")
        copy_target = dst if dst is not None else os.path.dirname(os.path.abspath(absfname))
        shutil.copy(health_results_path, copy_target)


if __name__ == "__main__":
    host = 'sfo-vcf01.sfo.rainpole.io'
    user = 'svc-hrm-vcf@sfo.rainpole.io'
    password = '********'
    sosrest = SosRest(host=host, user=user, password=password)
    time.sleep(5)
    operation_id = sosrest.start_health_checks_op()
    if operation_id:
        sosrest.get_health_checks_status(operation_id)
        sosrest.get_health_check_bundle(operation_id)
