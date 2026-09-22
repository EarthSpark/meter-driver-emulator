"""Meter driver emulator: the Meter Driver Specification's HTTP+SSE contract with no hardware behind it."""

import importlib.resources
import json

# Package version; reported as the driver firmware version and as the served
# OpenAPI document's info.version.
__version__ = "0.1.0"


def _read_spec_version():
    """Read info.version from the committed OpenAPI document."""
    text = importlib.resources.files(__package__).joinpath("openapi.json").read_text(encoding="utf-8")
    return json.loads(text)["info"]["version"]


# Version of the Meter Driver Specification this emulator implements. It is
# read from the committed openapi.json rather than written here, so it cannot
# drift from the document the emulator actually serves. That file is generated
# by scripts/sync_openapi.py from the meter-driver-spec submodule, so the
# version ultimately follows whatever commit the submodule points at.
SPEC_VERSION = _read_spec_version()
