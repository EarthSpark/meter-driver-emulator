"""Build hook: convert the pinned spec's OpenAPI YAML into the JSON the emulator serves.

The Meter Driver Specification is authored as YAML and lives only in the
meter-driver-spec submodule. The emulator serves that document at
/openapi.json but declares no runtime dependencies, and the standard library
has no YAML parser, so the conversion happens here, at build time, and the
result ships as package data. That is also what keeps the spec checkout out of
the container image: it is an input to the build, never part of the product.

The generated file is not committed. Rebuild (`uv sync`, `uv build`, or a
Docker build) after moving the submodule to a new spec commit.
"""

import json
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

SPEC_YAML = Path("meter-driver-spec") / "openapi" / "meter-driver.yaml"
OUTPUT = Path("src") / "meter_driver_emulator" / "openapi.json"


def convert(document):
    """Render an OpenAPI document as the JSON text shipped as package data."""
    if (
        not isinstance(document, dict)
        or "openapi" not in document
        or not isinstance(document.get("info"), dict)
    ):
        raise ValueError("not an OpenAPI document (needs top-level 'openapi' and 'info' mapping)")
    return json.dumps(document, indent=2) + "\n"


def generate(root):
    """Write the OpenAPI JSON under root from the spec submodule, and return its version.

    An sdist carries the generated JSON but not the submodule, so when the YAML
    is absent and the JSON is already there, that copy is kept.
    """
    root = Path(root)
    spec_yaml = root / SPEC_YAML
    output = root / OUTPUT

    if not spec_yaml.is_file():
        if output.is_file():
            return json.loads(output.read_text(encoding="utf-8"))["info"].get("version")
        raise FileNotFoundError(
            f"{spec_yaml} is missing: the meter-driver-spec submodule is not checked out, "
            "and there is no previously generated openapi.json to fall back on.\n"
            "Run: git submodule update --init"
        )

    import yaml

    document = yaml.safe_load(spec_yaml.read_text(encoding="utf-8"))
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(convert(document), encoding="utf-8")
    return document["info"].get("version")


class SpecBuildHook(BuildHookInterface):
    """Generates src/meter_driver_emulator/openapi.json before the package is assembled."""

    PLUGIN_NAME = "custom"

    def initialize(self, version, build_data):
        """Generate the document for every build, including editable installs."""
        spec_version = generate(self.root)
        self.app.display_info(f"openapi.json generated from the spec submodule (version {spec_version})")

    def clean(self, versions):
        """Remove the generated document."""
        (Path(self.root) / OUTPUT).unlink(missing_ok=True)
