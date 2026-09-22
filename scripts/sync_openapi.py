#!/usr/bin/env python3
"""Convert the spec repository's OpenAPI YAML into the JSON document the emulator serves.

Usage: sync_openapi.py <path to meter-driver-spec/openapi/meter-driver.yaml>

Writes src/meter_driver_emulator/openapi.json. The output is generated, never
hand-edited: rerun this after updating the spec checkout, then set
SPEC_VERSION in src/meter_driver_emulator/__init__.py to the spec's info.version.
"""

import argparse
import json
import sys
from pathlib import Path

import yaml

OUTPUT = Path(__file__).resolve().parent.parent / "src" / "meter_driver_emulator" / "openapi.json"


def main(argv=None):
    """Read the YAML at the given path and write it as JSON package data."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("spec_yaml", type=Path, help="path to the spec's openapi/meter-driver.yaml")
    args = parser.parse_args(argv)

    with args.spec_yaml.open(encoding="utf-8") as handle:
        document = yaml.safe_load(handle)
    if not isinstance(document, dict) or "openapi" not in document or "info" not in document:
        print(f"{args.spec_yaml}: not an OpenAPI document", file=sys.stderr)
        return 1

    OUTPUT.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {OUTPUT} (spec version {document['info'].get('version')})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
