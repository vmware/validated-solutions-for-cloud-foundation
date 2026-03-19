#!/usr/bin/env python3
# Copyright 2023-2026 Broadcom. All Rights Reserved.
# SPDX-License-Identifier: BSD-2
#
# Description:
# Idempotently inserts the sos_options stanza into env.json if it is missing.

import argparse
import json
import sys
from pathlib import Path

DEFAULT_SOS_OPTIONS = {
    "force": False,
    "include_free_hosts": False,
    "lock_max_hours": 4,
    "use_run_lock": True,
}


def main():
    parser = argparse.ArgumentParser(
        description="Add sos_options to HRM env.json if not already present."
    )
    parser.add_argument(
        "env_json",
        type=Path,
        help="Path to env.json",
    )
    parser.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="Print whether a change would be made and exit without writing.",
    )
    args = parser.parse_args()
    path = args.env_json
    if not path.is_file():
        print(f"Error: file not found: {path}", file=sys.stderr)
        return 1
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"Error: cannot read or parse JSON: {e}", file=sys.stderr)
        return 1
    if "sos_options" in data:
        print(f"sos_options already present in {path}; no change.")
        return 0
    if args.dry_run:
        print(f"Would add sos_options to {path}.")
        return 0
    # Preserve key order: insert sos_options first so it matches the sample env.json layout.
    merged = {"sos_options": dict(DEFAULT_SOS_OPTIONS)}
    merged.update(data)
    path.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")
    print(f"Added sos_options to {path}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
