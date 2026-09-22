"""Request body validation.

Two outcomes, per the spec's rejection rule:

- Structural problems (not a JSON object, missing required field, wrong JSON
  type, out-of-range unsigned integer, malformed AES key) raise `BadRequest`,
  which the server answers with 400 and emits no event.
- A well-formed but unacceptable meter configuration (command outside the
  enum, or a negative limit or unsigned field) is returned with
  `ConfigureRequest.rejected` set; the server answers 202 and emits
  `invalid_electrical_meter_configuration`.
"""

import json
import math
import re
from dataclasses import dataclass

UINT32_MAX = 2**32 - 1
UINT64_MAX = 2**64 - 1
INT32_MIN = -(2**31)
INT32_MAX = 2**31 - 1

AES_KEY_HEX = re.compile(r"^[A-Fa-f0-9]{32}$")

COMMAND_NAMES = frozenset(
    {
        "ElectricalMeterCommandEnable",
        "ElectricalMeterCommandDisable",
        "ElectricalMeterCommandReboot",
        "ElectricalMeterCommandCalibrateStart",
        "ElectricalMeterCommandCalibrateFinish",
        "ElectricalMeterCommandEnableSts",
    }
)

CONFIGURATION_FLOAT_FIELDS = ("power_limit", "current_limit")
CONFIGURATION_UINT32_FIELDS = (
    "startup_delay",
    "throttle_on_time",
    "throttle_off_time",
    "throttle_count_limit",
)


class BadRequest(Exception):
    """A structurally invalid request; answered with 400 `{error: "invalid_request", message}`."""

    error = "invalid_request"


def _reject_constant(name):
    # json.loads would otherwise accept NaN, Infinity and -Infinity, which
    # are not JSON.
    raise ValueError(f"{name} is not valid JSON")


def parse_json_object(body):
    """Decode a request body that must be a JSON object."""
    try:
        # ValueError also covers JSONDecodeError and the integer-string
        # conversion limit exceeded by an enormous integer literal.
        value = json.loads(body, parse_constant=_reject_constant) if body else None
    except (UnicodeDecodeError, ValueError) as exc:
        raise BadRequest(f"body is not valid JSON: {exc}") from None
    if not isinstance(value, dict):
        raise BadRequest("body must be a JSON object")
    return value


def _present(obj, field, required):
    if field not in obj or obj[field] is None:
        if required:
            raise BadRequest(f"missing required field '{field}'")
        return False
    return True


def _integer(value, field):
    # bool is a subclass of int in Python but a distinct JSON type.
    if isinstance(value, bool) or not isinstance(value, int):
        raise BadRequest(f"'{field}' must be an integer")
    return value


def _ranged(value, field, low, high):
    if value < low or value > high:
        raise BadRequest(f"'{field}' must be between {low} and {high}")
    return value


def uint32(obj, field, required=True, default=None):
    """Read an optional or required uint32 field."""
    if not _present(obj, field, required):
        return default
    return _ranged(_integer(obj[field], field), field, 0, UINT32_MAX)


def uint64(obj, field, required=True, default=None):
    """Read an optional or required uint64 field."""
    if not _present(obj, field, required):
        return default
    return _ranged(_integer(obj[field], field), field, 0, UINT64_MAX)


def int32(obj, field, required=True, default=None):
    """Read an optional or required int32 field."""
    if not _present(obj, field, required):
        return default
    return _ranged(_integer(obj[field], field), field, INT32_MIN, INT32_MAX)


def number(obj, field, required=True, default=None):
    """Read an optional or required JSON number field (integer or float, not boolean)."""
    if not _present(obj, field, required):
        return default
    value = obj[field]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BadRequest(f"'{field}' must be a number")
    try:
        result = float(value)
    except OverflowError:
        raise BadRequest(f"'{field}' is too large") from None
    # A literal such as 1e999 parses to infinity.
    if not math.isfinite(result):
        raise BadRequest(f"'{field}' must be a finite number")
    return result


def boolean(obj, field, required=True, default=None):
    """Read an optional or required boolean field."""
    if not _present(obj, field, required):
        return default
    value = obj[field]
    if not isinstance(value, bool):
        raise BadRequest(f"'{field}' must be a boolean")
    return value


def string(obj, field, required=True, default=None):
    """Read an optional or required string field."""
    if not _present(obj, field, required):
        return default
    value = obj[field]
    if not isinstance(value, str):
        raise BadRequest(f"'{field}' must be a string")
    return value


def nested_object(obj, field, required=True):
    """Read an optional or required object-valued field."""
    if not _present(obj, field, required):
        return None
    value = obj[field]
    if not isinstance(value, dict):
        raise BadRequest(f"'{field}' must be an object")
    return value


def path_node_id(text):
    """Parse the `{node_id}` path segment as a uint64."""
    if not text.isascii() or not text.isdigit():
        raise BadRequest("node_id in path must be an unsigned integer")
    return _ranged(int(text), "node_id", 0, UINT64_MAX)


def version(obj, field, required=True):
    """Read a `Version {major, minor, patch}` field."""
    inner = nested_object(obj, field, required)
    if inner is None:
        return None
    return {"major": uint32(inner, "major"), "minor": uint32(inner, "minor"), "patch": uint32(inner, "patch")}


def decimal(obj, field, required=True):
    """Read a `Decimal {sign, coef, exp}` field."""
    inner = nested_object(obj, field, required)
    if inner is None:
        return None
    return {"sign": int32(inner, "sign"), "coef": uint64(inner, "coef"), "exp": int32(inner, "exp")}


def aes_key(obj, field="aes_key"):
    """Read an AES key given as a 32-character hex string or a 16-element byte array; returns hex."""
    _present(obj, field, True)
    value = obj[field]
    if isinstance(value, str):
        if not AES_KEY_HEX.match(value):
            raise BadRequest(f"'{field}' must be 32 hexadecimal characters")
        return value
    if isinstance(value, list):
        if len(value) != 16:
            raise BadRequest(f"'{field}' as an array must have exactly 16 bytes")
        for byte in value:
            if isinstance(byte, bool) or not isinstance(byte, int) or byte < 0 or byte > 255:
                raise BadRequest(f"'{field}' bytes must be integers between 0 and 255")
        return bytes(value).hex()
    raise BadRequest(f"'{field}' must be a hex string or an array of 16 bytes")


def mask_aes_key(key_hex):
    """Mask a key as its first two and last two hex digits, uppercase, joined by `..`."""
    upper = key_hex.upper()
    return f"{upper[:2]}..{upper[-2:]}"


@dataclass(frozen=True)
class InitRequest:
    """Validated `POST /v1/init` body; `aes_key` is the key in hex."""

    heartbeat_period_duration: int
    channel: int
    aes_key: str


def validate_init(body):
    """Validate an `InitRequest`; `channel` defaults to 0 when absent."""
    return InitRequest(
        heartbeat_period_duration=uint32(body, "heartbeat_period_duration"),
        channel=uint32(body, "channel", required=False, default=0),
        aes_key=aes_key(body),
    )


@dataclass(frozen=True)
class RegisterRequest:
    """Validated `POST /v1/nodes/register` body; optional fields are None when absent."""

    node_id: int
    node_type: str
    mac: int | None
    firmware_version: dict | None
    balance: dict | None
    low_balance_flag: bool | None
    request_phased_readings: bool


def validate_register(body):
    """Validate a `RegisterNodeRequest`."""
    return RegisterRequest(
        node_id=uint64(body, "node_id"),
        node_type=string(body, "node_type"),
        mac=uint32(body, "mac", required=False),
        firmware_version=version(body, "firmware_version", required=False),
        balance=decimal(body, "balance", required=False),
        low_balance_flag=boolean(body, "low_balance_flag", required=False),
        request_phased_readings=boolean(body, "request_phased_readings", required=False, default=False),
    )


@dataclass(frozen=True)
class ConfigureRequest:
    """Validated configure-meter body, from either spec form.

    `configuration` holds the six spec fields as parsed: limits as floats,
    unsigned fields as integers, unknown keys dropped. `rejected` is set when
    the body is well-formed but the driver will not apply it.
    """

    node_id: int
    command: str
    configuration: dict
    rejected: bool

    def as_compat_request(self):
        """Return the parsed node_id, command and configuration in the compat request shape.

        This is what `invalid_electrical_meter_configuration` carries.
        """
        return {"node_id": self.node_id, "command": self.command, "configuration": dict(self.configuration)}


def validate_configure(body, node_id=None):
    """Validate a configure-meter body.

    `node_id` is the path value for the path form and None for the body form.
    In the path form a body `node_id` is allowed only when it equals the path.
    """
    if node_id is None:
        node_id = uint64(body, "node_id")
    elif "node_id" in body and body["node_id"] is not None:
        body_node_id = uint64(body, "node_id")
        if body_node_id != node_id:
            raise BadRequest(f"node_id in body ({body_node_id}) does not match path ({node_id})")
    command = string(body, "command")
    raw = nested_object(body, "configuration")
    configuration = {}
    rejected = command not in COMMAND_NAMES
    for field in CONFIGURATION_FLOAT_FIELDS:
        configuration[field] = number(raw, field)
        rejected = rejected or configuration[field] < 0
    for field in CONFIGURATION_UINT32_FIELDS:
        # A negative value is a rejection, not a structural error; a value
        # above the uint32 range is structural.
        _present(raw, field, True)
        value = _integer(raw[field], field)
        if value > UINT32_MAX:
            raise BadRequest(f"'{field}' must be at most {UINT32_MAX}")
        configuration[field] = value
        rejected = rejected or value < 0
    return ConfigureRequest(node_id=node_id, command=command, configuration=configuration, rejected=rejected)


@dataclass(frozen=True)
class BalanceRequest:
    """Validated `POST /v1/nodes/{node_id}/balance-and-flags` body."""

    balance: dict
    low_balance_flag: bool


def validate_balance(body):
    """Validate a `SetBalanceAndFlagsRequest`."""
    return BalanceRequest(
        balance=decimal(body, "balance"), low_balance_flag=boolean(body, "low_balance_flag")
    )
