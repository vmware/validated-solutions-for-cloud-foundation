# Add `sos_options` to `env.json`

Health Reporting and Monitoring reads optional **SoS (SOS Utility)** settings from `env.json` under **`sos_options`**:

| Key | Type | Purpose |
|-----|------|---------|
| `force` | boolean | Passed to SoS health-summary `options.config.force`. |
| `include_free_hosts` | boolean | Passed to SoS `scope.includeFreeHosts`. |
| `lock_max_hours` | integer | Maximum hours to poll SoS bundle creation (also used as poll budget). |
| `use_run_lock` | boolean | Whether to use a run lock file to avoid overlapping runs (see implementation). |

If `sos_options` is omitted, the script uses built-in defaults (same values as below).

**Default stanza:**

```json
"sos_options": {
  "force": false,
  "include_free_hosts": false,
  "lock_max_hours": 4,
  "use_run_lock": true
}
```

---

## Before you start

1. **Back up** `env.json` (copy to `env.json.bak` or use your backup tool).
2. If **`sos_options` already exists**, do not apply the patch or run the script again unless you intend to replace it manually.
3. The **unified diff** below assumes the file begins with `{` on line 1 and **`"vrops":{`** on line 2 (two-space indent, no space before `{` after `vrops`). If your file differs (different key order, spacing, or `"vrops": {` with a space), use the **Python script** instead.

---

## Method 1: Python script (Linux, macOS, Windows — recommended)

Idempotent: does nothing if `sos_options` is already present. Preserves other keys and inserts `sos_options` at the top of the object (written order may differ from your original key order).

### Linux / macOS

From the directory that contains `env.json` (often `main/`):

```bash
cp env.json env.json.bak
python3 /path/to/validated-solutions-for-cloud-foundation/hrm/patches/add_sos_options_to_env_json.py env.json
```

Dry run:

```bash
python3 /path/to/.../add_sos_options_to_env_json.py env.json --dry-run
```

### Windows (Command Prompt or PowerShell)

```powershell
copy env.json env.json.bak
python C:\path\to\validated-solutions-for-cloud-foundation\hrm\patches\add_sos_options_to_env_json.py env.json
```

Use the same path style if `python` is on `PATH` (e.g. `py -3` instead of `python` if that is how you launch Python).

---

## Method 2: `patch` (Linux, macOS, Git Bash on Windows)

Run from the **directory that contains `env.json`** so the paths `env.json` in the patch resolve.

```bash
cp env.json env.json.bak
patch -b -p0 < /path/to/validated-solutions-for-cloud-foundation/hrm/patches/env-json-add-sos-options.patch
```

- **`-b`** creates `env.json.orig` on GNU patch (behavior varies slightly on BSD).
- **`-p0`** keeps path `env.json` as-is.

If the patch rejects (hunk failed), your first lines do not match; use Method 1 or edit the patch context to match your file.

**Windows:** Install [Git for Windows](https://git-scm.com/download/win) and use **Git Bash**, or install a `patch` port (e.g. GnuWin32). Then run the same commands in Bash with forward slashes or quoted Windows paths.

---

## Method 3: `git apply` (optional)

If your `env.json` is inside a Git repository and you want to use `git apply`, copy the patch into the repo and adjust the diff headers to `a/env.json` and `b/env.json`, then run from the repo root:

```bash
git apply --check path/to/env-json-add-sos-options.patch
git apply path/to/env-json-add-sos-options.patch
```

You may need to edit the patch paths to match your tree. Method 1 is usually simpler for a single file outside a clone layout.

---

## Verify

```bash
python3 -c "import json; print(json.load(open('env.json'))['sos_options'])"
```

Confirm JSON is still valid and `send-data-to-vrops.py` starts without errors.
