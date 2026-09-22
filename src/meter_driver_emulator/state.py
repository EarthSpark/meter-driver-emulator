"""Driver runtime state: configuration, meters, counters, and the synthetic readings they produce.

Mutating methods return the events to publish as `(type, data)` tuples in
spec order; the caller publishes them. Nothing here touches the network.
"""

import re
import threading
import time
from dataclasses import dataclass, field

from meter_driver_emulator import __version__

GATEWAY_TYPE = "emulator"
POWER_FACTOR = 0.99
READ_REPLY_MILLISECONDS = 120.0
REGISTER_SOURCE_MANUAL = 1
COMPUTED_FIELDS_VERSION = 1

# ElectricalMeterState values the emulator uses.
STATE_ON = 1
STATE_METER_DISABLED = 11
STATE_CALIBRATE = 12

# Command -> resulting ElectricalMeterState.
COMMAND_STATES = {
    "ElectricalMeterCommandEnable": STATE_ON,
    "ElectricalMeterCommandDisable": STATE_METER_DISABLED,
    "ElectricalMeterCommandReboot": STATE_ON,
    "ElectricalMeterCommandCalibrateStart": STATE_CALIBRATE,
    "ElectricalMeterCommandCalibrateFinish": STATE_ON,
    "ElectricalMeterCommandEnableSts": STATE_ON,
}

# Reading fields that get _a/_b/_c copies in the phased form.
PHASED_FIELDS = (
    "frequency",
    "current_avg",
    "current_min",
    "current_max",
    "voltage_avg",
    "voltage_min",
    "voltage_max",
    "true_power_avg",
    "true_power_inst",
    "apparent_power_avg",
    "power_factor_avg",
)


def _parse_version(text):
    """Read the leading major.minor.patch of a version string; anything after (rc1, +local) is ignored."""
    match = re.match(r"^(\d+)\.(\d+)\.(\d+)", text)
    if match is None:
        raise RuntimeError(f"__version__ {text!r} does not start with major.minor.patch")
    return {"major": int(match[1]), "minor": int(match[2]), "patch": int(match[3])}


# Resolved once at import so a malformed __version__ fails the process start
# rather than the first meter registration.
PACKAGE_VERSION = _parse_version(__version__)


def package_version():
    """Return the package version as a spec `Version` object (a fresh copy)."""
    return dict(PACKAGE_VERSION)


def _zero_statistics():
    return {"count": 0, "last_value": 0.0, "max": 0.0, "min": 0.0, "avg": 0.0}


@dataclass(frozen=True)
class Nominal:
    """Steady conditions every energized meter reports."""

    voltage: float = 230.0
    frequency: float = 50.0
    load_watts: float = 100.0


@dataclass
class Meter:
    """One node the driver has been told about.

    A record is created the first time any route names the node_id and kept
    after unregistration; `registered` says whether it takes part in
    heartbeat cycles.
    """

    node_id: int
    node_type: str = ""
    mac: int | None = None
    firmware_version: dict = field(default_factory=package_version)
    balance: dict | None = None
    low_balance_flag: bool = False
    request_phased_readings: bool = False
    registered: bool = False
    registered_at: float = 0.0
    state: int = STATE_ON
    configuration: dict | None = None
    energy: float = 0.0

    @property
    def energized(self):
        """Whether the meter draws its load; MeterDisabled and Calibrate do not."""
        return self.state == STATE_ON

    @property
    def user_power_limit(self):
        """The applied power_limit, or 0.0 before any configuration."""
        return float(self.configuration["power_limit"]) if self.configuration else 0.0


class DriverState:
    """Everything the emulator remembers between requests, guarded by one lock."""

    def __init__(self, nominal=None):
        self.nominal = nominal or Nominal()
        self._lock = threading.RLock()
        self.heartbeat_period_duration = 0
        self.channel = 0
        self.aes_key = None
        self.meters = {}
        self.messages_received = 0
        self.messages_sent = 0
        self.total_packets_sent = 0
        self.total_packets_received = 0

    @property
    def lock(self):
        """The reentrant lock guarding all state; callers hold it across a mutation and its publication."""
        return self._lock

    # -- counters ---------------------------------------------------------

    def count_request(self):
        """Record one HTTP request handled (`messages_received`)."""
        with self._lock:
            self.messages_received += 1

    # -- init -------------------------------------------------------------

    def init(self, heartbeat_period_duration, channel, aes_key, masked_aes_key):
        """Reset runtime state and store the configuration; returns the init events.

        Clears every meter record. The request, message and packet counters
        describe the process and persist across inits.
        """
        with self._lock:
            self.meters.clear()
            self.heartbeat_period_duration = heartbeat_period_duration
            self.channel = channel
            self.aes_key = aes_key
            applied = {
                "masked_aes_key": masked_aes_key,
                "channel": channel,
                "heartbeat_period_duration": heartbeat_period_duration,
            }
            return [("driver_configuration_applied", applied), ("gateway_status", self.gateway_status())]

    # -- meters -----------------------------------------------------------

    def _meter(self, node_id):
        meter = self.meters.get(node_id)
        if meter is None:
            meter = self.meters[node_id] = Meter(node_id)
        return meter

    def register(self, request):
        """Register a node from a validated RegisterRequest; returns the registration events."""
        with self._lock:
            meter = self._meter(request.node_id)
            if meter.registered:
                return [("node_already_registered", {"node_id": meter.node_id})]
            meter.node_type = request.node_type
            meter.mac = request.mac
            if request.firmware_version is not None:
                meter.firmware_version = dict(request.firmware_version)
            if request.balance is not None:
                meter.balance = dict(request.balance)
            if request.low_balance_flag is not None:
                meter.low_balance_flag = request.low_balance_flag
            meter.request_phased_readings = request.request_phased_readings
            meter.registered = True
            meter.registered_at = time.time()
            return [
                ("node_registered", {"node_id": meter.node_id, "source_type": REGISTER_SOURCE_MANUAL}),
                (
                    "node_firmware_version_changed",
                    {"node_id": meter.node_id, "firmware_version": dict(meter.firmware_version)},
                ),
            ]

    def unregister(self, node_id):
        """Unregister a node, keeping its record; returns the unregistration event."""
        with self._lock:
            meter = self.meters.get(node_id)
            if meter is None or not meter.registered:
                return [("node_to_unregister_unknown", {"node_id": node_id})]
            meter.registered = False
            return [("node_unregistered", {"node_id": node_id})]

    def configure(self, request):
        """Apply a validated ConfigureRequest, or reject it; returns the configuration events."""
        with self._lock:
            if request.rejected:
                return [
                    (
                        "invalid_electrical_meter_configuration",
                        {"invalid_configuration": request.as_compat_request()},
                    )
                ]
            meter = self._meter(request.node_id)
            meter.configuration = dict(request.configuration)
            meter.state = COMMAND_STATES[request.command]
            self.messages_sent += 1
            return [
                ("electrical_meter_configuration_accepted", {"node_id": meter.node_id}),
                (
                    "electrical_meter_configuration_applied",
                    {"node_id": meter.node_id, "configuration": dict(meter.configuration)},
                ),
            ]

    def set_balance(self, node_id, request):
        """Store a validated BalanceRequest; returns the acceptance event."""
        with self._lock:
            meter = self._meter(node_id)
            meter.balance = dict(request.balance)
            meter.low_balance_flag = request.low_balance_flag
            return [("electrical_meter_balance_and_flags_accepted", {"node_id": node_id})]

    def registered_meters(self):
        """Return the registered meters in node_id order."""
        with self._lock:
            return sorted((m for m in self.meters.values() if m.registered), key=lambda m: m.node_id)

    # -- readings ---------------------------------------------------------

    def reading(self, meter, period_start, period_end, period_seconds):
        """Produce one synthetic reading for a meter and accumulate its energy.

        Returns `(event_type, data)`: `electrical_meter_reading`, or the
        phased form when the meter asked for it.
        """
        with self._lock:
            nominal = self.nominal
            load = nominal.load_watts if meter.energized else 0.0
            power_factor = POWER_FACTOR if meter.energized else 0.0
            current = load / (nominal.voltage * POWER_FACTOR) if nominal.voltage else 0.0
            apparent = load / POWER_FACTOR
            meter.energy += load * period_seconds / 3600.0
            data = {
                "node_id": meter.node_id,
                "period_start": period_start,
                "period_end": period_end,
                "state": meter.state,
                "frequency": nominal.frequency,
                "current_avg": current,
                "current_min": current,
                "current_max": current,
                "voltage_avg": nominal.voltage,
                "voltage_min": nominal.voltage,
                "voltage_max": nominal.voltage,
                "true_power_avg": load,
                "true_power_inst": load,
                "apparent_power_avg": apparent,
                "power_factor_avg": power_factor,
                "energy": meter.energy,
                "uptime_secs": max(0, int(time.time() - meter.registered_at)),
                "user_power_limit": meter.user_power_limit,
            }
            self.messages_sent += 1
            if not meter.request_phased_readings:
                return "electrical_meter_reading", data
            for name in PHASED_FIELDS:
                data[f"{name}_a"] = data[name]
                data[f"{name}_b"] = 0.0
                data[f"{name}_c"] = 0.0
            data["phases"] = {"a": True, "b": False, "c": False}
            data["computed_fields_version"] = COMPUTED_FIELDS_VERSION
            return "electrical_meter_reading_phased", data

    def heartbeat_cycle(self, period_start, period_end, period_seconds):
        """Run one heartbeat cycle; returns readings, then heartbeat_statistics, then gateway_status."""
        with self._lock:
            meters = self.registered_meters()
            events = [self.reading(meter, period_start, period_end, period_seconds) for meter in meters]
            count = len(meters)
            self.total_packets_sent += count
            self.total_packets_received += count
            read_stats = _zero_statistics()
            if count:
                read_stats = {
                    "count": count,
                    "last_value": READ_REPLY_MILLISECONDS,
                    "max": READ_REPLY_MILLISECONDS,
                    "min": READ_REPLY_MILLISECONDS,
                    "avg": READ_REPLY_MILLISECONDS,
                }
            statistics = {
                "timestamp": period_end,
                "total_registered_nodes": count,
                "total_packets_sent": self.total_packets_sent,
                "total_packets_received": self.total_packets_received,
                "nodes_reached_out_to_in_current_heartbeat": count,
                "nodes_heard_from_in_current_heartbeat": count,
                "packets_sent_in_current_heartbeat": count,
                "packets_received_in_current_heartbeat": count,
                "millisecond_read_reply_stats": read_stats,
                "millisecond_set_config_reply_stats": _zero_statistics(),
            }
            events.append(("heartbeat_statistics", statistics))
            events.append(("gateway_status", self.gateway_status()))
            return events

    # -- status -----------------------------------------------------------

    def _status_fields(self):
        return {
            "connected": True,
            "firmware_version": package_version(),
            "messages_sent": self.messages_sent,
            "messages_received": self.messages_received,
            "gateway_type": GATEWAY_TYPE,
        }

    def status(self):
        """`StatusResponse` for GET /v1/status; the raw gateway fields are null."""
        with self._lock:
            return {**self._status_fields(), "gateway_firmware_raw": None, "gateway_bootloader_raw": None}

    def gateway_status(self):
        """`GatewayStatus` event payload; the event schema types the raw fields as strings."""
        with self._lock:
            return {
                **self._status_fields(),
                "gateway_firmware_raw": GATEWAY_TYPE,
                "gateway_bootloader_raw": GATEWAY_TYPE,
            }
