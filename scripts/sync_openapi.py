"""Convert the spec repository's OpenAPI YAML into the JSON document the emulator serves.

Usage: uv run scripts/sync_openapi.py <path to meter-driver-spec/openapi/meter-driver.yaml>

Writes src/meter_driver_emulator/openapi.json. The output is generated, never
hand-edited: rerun this after updating the spec checkout, then set
SPEC_VERSION in src/meter_driver_emulator/__init__.py to the spec's info.version.
"""

import argparse
import json
import sys
from pathlib import Path

OUTPUT = Path(__file__).resolve().parent.parent / "src" / "meter_driver_emulator" / "openapi.json"


def main(argv=None):
    """Read the YAML at the given path and write it as JSON package data."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("spec_yaml", type=Path, help="path to the spec's openapi/meter-driver.yaml")
    args = parser.parse_args(argv)

    try:
        import yaml
    except ImportError:
        print("PyYAML is not installed; run `uv sync --group dev` (it is a dev dependency)", file=sys.stderr)
        return 1

    try:
        with args.spec_yaml.open(encoding="utf-8") as handle:
            document = yaml.safe_load(handle)
    except OSError as exc:
        print(f"cannot read {args.spec_yaml}: {exc.strerror or exc}", file=sys.stderr)
        return 1
    except yaml.YAMLError as exc:
        print(f"{args.spec_yaml}: not valid YAML: {exc}", file=sys.stderr)
        return 1
    if (
        not isinstance(document, dict)
        or "openapi" not in document
        or not isinstance(document.get("info"), dict)
    ):
        print(
            f"{args.spec_yaml}: not an OpenAPI document (needs top-level 'openapi' and 'info' mapping)",
            file=sys.stderr,
        )
        return 1

    OUTPUT.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {OUTPUT} (spec version {document['info'].get('version')})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
