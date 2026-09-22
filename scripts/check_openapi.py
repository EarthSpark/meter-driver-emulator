"""Fail when the committed openapi.json was converted from a different spec version than SPEC_VERSION.

Usage: uv run python scripts/check_openapi.py
"""

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
    try:
        with OPENAPI.open(encoding="utf-8") as handle:
            document = json.load(handle)
    except ValueError as exc:
        print(f"{OPENAPI}: not valid JSON: {exc}", file=sys.stderr)
        return 1
    info = document.get("info") if isinstance(document, dict) else None
    served_version = info.get("version") if isinstance(info, dict) else None
    if not isinstance(served_version, str):
        print(f"{OPENAPI}: info.version is missing; regenerate with scripts/sync_openapi.py", file=sys.stderr)
        return 1
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
