"""Meter driver emulator: the Meter Driver Specification's HTTP+SSE contract with no hardware behind it."""

# Package version; reported as the driver firmware version and as the served
# OpenAPI document's info.version.
__version__ = "0.1.0"

# Tag of EarthSpark/meter-driver-spec whose openapi/meter-driver.yaml was
# converted into the committed openapi.json. scripts/check_openapi.py fails
# CI when the two disagree.
SPEC_VERSION = "1.4.0"
