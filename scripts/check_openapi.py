#!/usr/bin/env python3
"""Fail when the committed openapi.json was converted from a different spec version than SPEC_VERSION."""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
OPENAPI = SRC / "meter_driver_emulator" / "openapi.json"


def main():
    """Compare openapi.json info.version with the package's SPEC_VERSION."""
    sys.path.insert(0, str(SRC))
    from meter_driver_emulator import SPEC_VERSION

    if not OPENAPI.exists():
        print(f"{OPENAPI} is missing; run scripts/sync_openapi.py", file=sys.stderr)
        return 1
    with OPENAPI.open(encoding="utf-8") as handle:
        served_version = json.load(handle)["info"]["version"]
    if served_version != SPEC_VERSION:
        print(
            f"openapi.json info.version {served_version} does not match SPEC_VERSION {SPEC_VERSION}",
            file=sys.stderr,
        )
        return 1
    print(f"openapi.json info.version {served_version} matches SPEC_VERSION {SPEC_VERSION}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
