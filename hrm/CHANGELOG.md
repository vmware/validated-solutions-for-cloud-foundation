# Release History

## [v2.1.3]

> Release Date: TBD

Enhancement:

- Ship `hrm/patches/env-json-add-sos-options.patch`, idempotent `add_sos_options_to_env_json.py`, and `README-SOS-OPTIONS.md` with Linux/macOS/Windows usage; README links for upgrading older `env.json` files.
- send-data-to-vrops: log OS details, Python interpreter/version, standard-library import list, and versions for nagini, urllib3, cryptography, and in-tree `utils` modules at startup (support diagnostics).
- README: document that `nagini` is the Aria Operations Suite API Python bindings (from the appliance), not a PyPI package, with self-contained install steps (no external blog).
- Support for VMware Cloud Foundation 5.2.
- Script improvements to handle missing metrics data in VMware Aria Operations.
- Script performance improvements.

Bugfix:

- LogUtility: add `warning()` as an alias of `warn()` for compatibility with code using stdlib-style names.
- send-data-to-vrops: `get_resource_id` fallback for NSX VIP-style FQDN vs node name in inventory (e.g. `vip-nsx-mgmt` vs `nsx-mgmt-1`).
- send-data-to-vrops: SDDC Manager free pool — treat non-array JSON safely; log empty array as expected state; push summary metrics to SDDC Manager when pool has no rows; skip malformed/null rows.
- send-data-to-vrops: skip metrics when `get_resource_id` returns missing inventory (`build_payload` and `push_data_to_vrops`) to avoid nagini 400 `Cannot convert "None" to uuid`.
- send-data-to-vrops: `log_runtime_debug_context` and one SoS error line use a single string for `LogUtility.info`/`error` (wrapper does not accept stdlib logging extra args); `push_handler` uses `logger.error(..., trace=True)` instead of missing `logger.exception`.
- send-data-to-vrops: resolve `utils` from `main\utils` when the script sits in the pip `--target` root; set cwd to that `main` folder so `env.json` and `encrypted_files` work. README documents layout A (flat) and layout B (target root + `main`).
- SosRest: numeric VCF version comparison for 5.2+ payload (fixes double-digit majors and lexicographic string compare); JSON parse guard with logged snippets; SoS poll timeout aligned with `sos_options.lock_max_hours`; HTTP 200 check before streaming bundle; safe `logger` use; rename operation id variable; larger download chunks; single `health-results.json` copy target when `path` is None.
- send-data-to-vrops: store full VCF version string from PowerShell; validate PS output and SoS operation id; handle poll timeouts and JSON errors; `decrypt_pwds` binary file read and clear errors; `push_handler` uses `logger.exception`.
- PSUtility: return `None` explicitly when PowerShell execution fails.
- FolderUtility: collect log paths before delete during `os.walk` (safer iteration).
- encrypt-passwords: PEP 8 variable names for Fernet encrypt path.
- SosRest: add HTTP timeouts for all `requests` calls; stream bundle downloads; validate tar members before `extractall` (path traversal); document TLS `verify=False` with Bandit `nosec` where appliance PKI applies.
- LogUtility / FolderUtility: fix Ruff docstring and unused `os.walk` variable issues.
- send-data-to-vrops: prepend the script directory to `sys.path` before local imports so `utils` resolves when the process cwd differs from the folder that contains the script (typical on Windows if the script is invoked from another directory).
- HRM SoS client: use scope per domain instead of includeAllDomains; set force and includeFreeHosts to false by default to avoid unexpected results and improve reliability after cluster offline/online operations. Optional `sos_options` in env.json: `force`, `include_free_hosts`, `lock_max_hours`, `use_run_lock`.
- Collect SoS health per domain and merge results before pushing to VMware Aria Operations so dashboards show metrics for all ESXi hosts across all domains (previously only the last domain's data was pushed).
- PSUtility: fixed inverted condition in `finally` (kill process only when subprocess exists); use portable `terminate()`/`wait()`/`kill()` instead of Unix-only `killpg`; remove unused `signal` import.
- FolderUtility: fixed `remove_folder` and `remove_file` to use try/except and log errors (no longer rely on return value of `rmtree`/`remove`); renamed `make_director_with_timestamp` to `make_directory_with_timestamp`; LogUtility updated to use new method name.
- SosRest: use `urllib3` directly instead of deprecated `requests.packages.urllib3`.
- LogUtility: use `logger.warning` instead of deprecated `logger.warn`; only log traceback in `warn()` when an exception is active.
- encrypt-passwords: create `encrypted_files` directory if missing before writing key and encrypted passwords.
- send-data-to-vrops: load env.json in `__init__` with explicit error handling (no longer use `read_data` before logger exists); `read_data` now handles None path, missing file, and JSON/IO errors and returns None or [] as appropriate; skip each push step when its JSON file is not found and log instead of crashing; fixed `push_localuserexpiry_status` to read file content and pass data array to `push_flat_struct` (was passing path); all push_* methods that use `read_data` now guard on None and skip with a log message.
- notifications: write notification JSON to a temp file and rename on success to avoid truncated output on failure; alphabetize imports.

Chore:

- README: document required on-disk layout (`utils` next to `send-data-to-vrops.py`); add `utils/__init__.py`; always prepend script directory to `sys.path` and exit with a clear message if `utils/LogUtility.py` is missing.
- send-data-to-vrops: clarify why Ruff E402 is suppressed on imports after `sys.path` setup; rename `get_worload_domains_cmd` to `get_workload_domains_cmd`.
- Optional run lock to prevent overlapping scheduled runs; adjust schedule/time and frequency to avoid conflicts. Configure `sos_options.use_run_lock` and `sos_options.lock_max_hours` in env.json if needed.
- Low-risk code quality fixes: use `logger.warning` instead of deprecated `logger.warn`; use `isinstance()` instead of `type()` checks; fix mutable default in `get_most_nested_dict`; use `urllib3` directly instead of deprecated `requests.packages.urllib3`; use `raise` to preserve traceback; replace `print` with `logger.debug` in `build_payload`.

## [v2.1.2]

> Release Date: 2024-04-25

Bugfix:

- Updated artifacts to add vCenter Single Sign-on Ring Topology Health in VMware Aria Operations (Compute dashboard, New Supermetrics, View, Alert/ Notification). [GH-89](https://github.com/vmware-samples/validated-solutions-for-cloud-foundation/issues/89)

## [v2.1.1]

> Release Date: 2024-04-04

Bugfix:

- Fixed parsing of SOS Version health data for VxRail Manager VM.
- Updated Artifacts to address incorrect status reported for NSX Backup Health, Supermetrics and Rollup dashboard.

Chore:

- Removed unused files.

## [v2.1.0]

> Release Date: 2024-01-30

Enhancement:

- Added new health checks and updated dashboards to have parity with `VMware.CloudFoundation.Reporting`.
  - ESXi Connection Health table is added to VCF Compute dashboard.
  - Free Pool Health table is added to VCF Compute dashboard.
  - VMs with Connected CD-ROMs table is added to VCF Compute dashboard.
  - VCF Version Health (New Dashboard).
- Updated artifacts - Alerts, Notifications, Supermetrics, Views and Dashboards for new health checks.

Documentation:

- Removed support for VCF version 4.4.
- `Install the Python Module in a Disconnected Environment` section has been added to the `README.md`.
- `Updating the Python Module to the Latest Version` section has been added to the `README.md`.
- Updated Screenshots for new dashboards in the `README.md`.

Chore:

- Updated headers in `.py` files.

## [v2.0.0]

> Release Date: 2023-08-29

Enhancement:

- Added `notifications.py` to create `notifications.json` file from the `notifications-template.json` template.
- Updated artifacts to address changes for use of the VMware Cloud Foundation cloud account.

Chore:

- Code cleanup.
- Remove unused files

## [v1.2.0]

> Release Date: 2023-07-25

Bugfix:

- Update code to handle special characters in passwords for Powershell cmdlets. [GH-62](https://github.com/vmware-samples/validated-solutions-for-cloud-foundation/issues/62)

Enhancement:

- Updated format for `Notifications.json` file to make it readable. [GH-61](https://github.com/vmware-samples/validated-solutions-for-cloud-foundation/issues/61)

Documentation:

- Updated `README.md` and update version and dist files for release. [GH-61](https://github.com/vmware-samples/validated-solutions-for-cloud-foundation/issues/61)

Chore:

- Added `CHANGELOG.md` and removed changelog from `send-data-to-vrops.py` file. [GH-61](https://github.com/vmware-samples/validated-solutions-for-cloud-foundation/issues/61)

## [v1.1.0]

> Release Date: 2023-05-30

Bugfix:

- Fixed the date on `Backups and Snapshot` dashboard which was shown incorrectly in previous version [GH-50](https://github.com/vmware-samples/validated-solutions-for-cloud-foundation/issues/50)


Enhancement:

- Removed SDDC Manager root password from Python module [GH-38](https://github.com/vmware-samples/validated-solutions-for-cloud-foundation/issues/38)
- Updated project structure to host module on PyPI.


Chore:

- Updated views for `Backup and Snapshots` to address issue [GH-50](https://github.com/vmware-samples/validated-solutions-for-cloud-foundation/issues/50)
- code cleanup

Documentation:

- Updated `README.md` and update version and dist files for release

## [1.0.0]

> Release Date: 2023-03-28

Bugfix:

- Fixed code to handle exception while sending `Backup Status` data to vROps [GH-48](https://github.com/vmware-samples/validated-solutions-for-cloud-foundation/issues/48)
