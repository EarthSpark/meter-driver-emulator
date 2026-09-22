"""Heartbeat cycles: synthetic readings, phased form, cycle statistics, scheduling."""

import time

import pytest
from conftest import FULL_CONFIGURATION, Client, Subscriber, make_emulator

from meter_driver_emulator.state import (
    PHASED_FIELDS,
    POWER_FACTOR,
    STATE_CALIBRATE,
    STATE_METER_DISABLED,
    STATE_ON,
    Nominal,
)

READING_FIELDS = {
    "node_id",
    "period_start",
    "period_end",
    "state",
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
    "energy",
    "uptime_secs",
    "user_power_limit",
}


def start_cycle(client, subscriber, node_id=65276, period=1, **register_extra):
    """Init with the given period, register one meter, and consume the resulting events."""
    client.init(period=period)
    subscriber.expect_sequence("driver_configuration_applied", "gateway_status")
    client.register(node_id, **register_extra)
    subscriber.expect_sequence("node_registered", "node_firmware_version_changed")


def test_reading_arrives_within_two_seconds_with_every_field(client, subscriber, validator):
    registered_at = time.time()
    start_cycle(client, subscriber)
    started = time.monotonic()
    reading = subscriber.expect("electrical_meter_reading", timeout=2.0)
    assert time.monotonic() - started < 2.0
    assert 0 <= reading.payload["uptime_secs"] <= int(time.time() - registered_at) + 1
    validator.assert_valid("ElectricalMeterReading", reading.payload)
    assert READING_FIELDS <= set(reading.payload)
    data = reading.payload
    assert data["node_id"] == 65276
    assert data["state"] == STATE_ON
    assert data["period_end"] >= data["period_start"]
    assert data["voltage_avg"] == data["voltage_min"] == data["voltage_max"] == 230.0
    assert data["frequency"] == 50.0
    assert data["true_power_avg"] == data["true_power_inst"] == 100.0
    assert data["apparent_power_avg"] == pytest.approx(100.0 / POWER_FACTOR)
    assert data["current_avg"] == data["current_min"] == data["current_max"]
    assert data["current_avg"] == pytest.approx(100.0 / (230.0 * POWER_FACTOR))
    assert data["power_factor_avg"] == POWER_FACTOR
    assert data["user_power_limit"] == 0.0
    assert data["energy"] == pytest.approx(100.0 / 3600.0)

    statistics, status = subscriber.expect_sequence("heartbeat_statistics", "gateway_status")
    validator.assert_valid("HeartbeatStatistics", statistics.payload)
    assert statistics.payload["timestamp"] == data["period_end"]
    for name in (
        "total_registered_nodes",
        "nodes_reached_out_to_in_current_heartbeat",
        "nodes_heard_from_in_current_heartbeat",
        "packets_sent_in_current_heartbeat",
        "packets_received_in_current_heartbeat",
        "total_packets_sent",
        "total_packets_received",
    ):
        assert statistics.payload[name] == 1, name
    assert statistics.payload["millisecond_read_reply_stats"] == {
        "count": 1,
        "last_value": 120.0,
        "max": 120.0,
        "min": 120.0,
        "avg": 120.0,
    }
    assert statistics.payload["millisecond_set_config_reply_stats"] == {
        "count": 0,
        "last_value": 0.0,
        "max": 0.0,
        "min": 0.0,
        "avg": 0.0,
    }
    validator.assert_valid("GatewayStatus", status.payload)

    second = subscriber.expect("electrical_meter_reading", timeout=2.0)
    assert second.payload["energy"] > data["energy"]
    assert second.payload["period_start"] >= data["period_end"] - 1
    later_statistics = subscriber.expect_sequence("heartbeat_statistics", "gateway_status")[0]
    assert later_statistics.payload["total_packets_sent"] == 2


def test_phased_reading(client, subscriber, validator):
    start_cycle(client, subscriber, request_phased_readings=True)
    reading = subscriber.expect("electrical_meter_reading_phased", timeout=2.0)
    validator.assert_valid("ElectricalMeterReadingPhased", reading.payload)
    data = reading.payload
    assert READING_FIELDS <= set(data)
    assert data["phases"] == {"a": True, "b": False, "c": False}
    assert data["computed_fields_version"] == 1
    for name in PHASED_FIELDS:
        assert data[f"{name}_a"] == data[name], name
        assert data[f"{name}_b"] == 0.0, name
        assert data[f"{name}_c"] == 0.0, name
    subscriber.expect_sequence("heartbeat_statistics", "gateway_status")


def test_period_zero_produces_no_cycle(client, subscriber):
    start_cycle(client, subscriber, period=0)
    subscriber.assert_silent(1.5)


def test_init_restarts_the_cycle_and_clears_registrations(client, subscriber):
    start_cycle(client, subscriber)
    subscriber.expect_sequence("electrical_meter_reading", "heartbeat_statistics", "gateway_status")

    reinit_at = time.time()
    client.init(period=1)
    subscriber.expect_sequence("driver_configuration_applied", "gateway_status")
    # The cleared registration means the next cycle carries no reading.
    statistics = subscriber.expect("heartbeat_statistics", timeout=2.0)
    assert statistics.payload["total_registered_nodes"] == 0
    assert statistics.payload["millisecond_read_reply_stats"]["count"] == 0
    assert statistics.payload["timestamp"] >= int(reinit_at)
    subscriber.expect("gateway_status")

    client.register(65276)
    subscriber.expect_sequence("node_registered", "node_firmware_version_changed")
    reading = subscriber.expect("electrical_meter_reading", timeout=2.0)
    assert reading.payload["period_start"] >= int(reinit_at)


def test_disable_then_enable_changes_state_and_power(client, subscriber):
    start_cycle(client, subscriber)
    client.configure(65276, command="ElectricalMeterCommandDisable")
    subscriber.expect_sequence(
        "electrical_meter_configuration_accepted", "electrical_meter_configuration_applied"
    )

    first = subscriber.expect("electrical_meter_reading", timeout=2.0).payload
    assert first["state"] == STATE_METER_DISABLED
    assert first["current_avg"] == first["current_min"] == first["current_max"] == 0.0
    assert first["true_power_avg"] == first["true_power_inst"] == 0.0
    assert first["apparent_power_avg"] == 0.0
    assert first["power_factor_avg"] == 0.0
    assert first["voltage_avg"] == 230.0 and first["frequency"] == 50.0
    assert first["user_power_limit"] == FULL_CONFIGURATION["power_limit"]
    subscriber.expect_sequence("heartbeat_statistics", "gateway_status")

    second = subscriber.expect("electrical_meter_reading", timeout=2.0).payload
    assert second["energy"] == first["energy"]
    subscriber.expect_sequence("heartbeat_statistics", "gateway_status")

    limit = {**FULL_CONFIGURATION, "power_limit": 2200.0}
    client.configure(65276, command="ElectricalMeterCommandEnable", configuration=limit)
    subscriber.expect_sequence(
        "electrical_meter_configuration_accepted", "electrical_meter_configuration_applied"
    )
    third = subscriber.expect("electrical_meter_reading", timeout=2.0).payload
    assert third["state"] == STATE_ON
    assert third["true_power_avg"] == 100.0
    assert third["user_power_limit"] == 2200.0
    assert third["energy"] > second["energy"]


@pytest.mark.parametrize(
    ("command", "state"),
    [
        ("ElectricalMeterCommandEnable", STATE_ON),
        ("ElectricalMeterCommandDisable", STATE_METER_DISABLED),
        ("ElectricalMeterCommandReboot", STATE_ON),
        ("ElectricalMeterCommandCalibrateStart", STATE_CALIBRATE),
        ("ElectricalMeterCommandCalibrateFinish", STATE_ON),
        ("ElectricalMeterCommandEnableSts", STATE_ON),
    ],
)
def test_command_sets_reported_state(client, subscriber, command, state):
    start_cycle(client, subscriber)
    client.configure(65276, command=command)
    subscriber.expect_sequence(
        "electrical_meter_configuration_accepted", "electrical_meter_configuration_applied"
    )
    reading = subscriber.expect("electrical_meter_reading", timeout=2.0).payload
    assert reading["state"] == state
    assert (reading["true_power_avg"] == 100.0) is (state == STATE_ON)


def test_configured_and_balanced_unregistered_node_gets_no_readings(client, subscriber):
    start_cycle(client, subscriber, node_id=2)
    client.configure(1)
    subscriber.expect_sequence(
        "electrical_meter_configuration_accepted", "electrical_meter_configuration_applied"
    )
    client.post(
        "/v1/nodes/1/balance-and-flags",
        {"balance": {"sign": 1, "coef": 5, "exp": 0}, "low_balance_flag": True},
    )
    subscriber.expect("electrical_meter_balance_and_flags_accepted")

    messages = subscriber.collect(2.2)
    readings = [m for m in messages if m.event == "electrical_meter_reading"]
    assert readings and all(m.payload["node_id"] == 2 for m in readings)
    statistics = [m for m in messages if m.event == "heartbeat_statistics"]
    assert statistics and all(m.payload["total_registered_nodes"] == 1 for m in statistics)


def test_unregistered_meter_stops_reading(client, subscriber):
    start_cycle(client, subscriber)
    subscriber.expect_sequence("electrical_meter_reading", "heartbeat_statistics", "gateway_status")
    client.delete("/v1/nodes/65276")
    subscriber.expect("node_unregistered")
    messages = subscriber.collect(1.5)
    assert all(m.event != "electrical_meter_reading" for m in messages)
    assert any(
        m.event == "heartbeat_statistics" and m.payload["total_registered_nodes"] == 0 for m in messages
    )


def test_status_counts_readings_as_messages_sent(client, subscriber):
    start_cycle(client, subscriber)
    before = client.get("/v1/status").json["messages_sent"]
    subscriber.expect_sequence("electrical_meter_reading", "heartbeat_statistics", "gateway_status")
    assert client.get("/v1/status").json["messages_sent"] == before + 1


def test_nominal_conditions_shape_readings(validator):
    emulator = make_emulator(nominal=Nominal(voltage=120.0, frequency=60.0, load_watts=50.0))
    emulator.start()
    try:
        client = Client(emulator.address)
        subscriber = Subscriber(emulator.address, validator)
        subscriber.expect("gateway_status")
        start_cycle(client, subscriber)
        reading = subscriber.expect("electrical_meter_reading", timeout=2.0).payload
        assert reading["voltage_avg"] == 120.0
        assert reading["frequency"] == 60.0
        assert reading["true_power_avg"] == 50.0
        assert reading["apparent_power_avg"] == pytest.approx(50.0 / POWER_FACTOR)
        assert reading["current_avg"] == pytest.approx(50.0 / (120.0 * POWER_FACTOR))
        assert reading["energy"] == pytest.approx(50.0 / 3600.0)
        subscriber.close()
    finally:
        emulator.shutdown()


def test_two_meters_per_cycle(client, subscriber):
    start_cycle(client, subscriber, node_id=1)
    client.register(2)
    subscriber.expect_sequence("node_registered", "node_firmware_version_changed")

    first, second = subscriber.expect_sequence("electrical_meter_reading", "electrical_meter_reading")
    assert {first.payload["node_id"], second.payload["node_id"]} == {1, 2}
    statistics = subscriber.expect("heartbeat_statistics").payload
    for name in (
        "total_registered_nodes",
        "nodes_reached_out_to_in_current_heartbeat",
        "nodes_heard_from_in_current_heartbeat",
        "packets_sent_in_current_heartbeat",
        "packets_received_in_current_heartbeat",
        "total_packets_sent",
        "total_packets_received",
    ):
        assert statistics[name] == 2, name
    assert statistics["millisecond_read_reply_stats"]["count"] == 2
    subscriber.expect("gateway_status")

    subscriber.expect_sequence("electrical_meter_reading", "electrical_meter_reading")
    later = subscriber.expect("heartbeat_statistics").payload
    assert later["total_packets_sent"] == later["total_packets_received"] == 4
    assert later["packets_sent_in_current_heartbeat"] == 2


def test_registering_a_configured_node_keeps_its_configuration(client, subscriber):
    client.init(period=1)
    subscriber.expect_sequence("driver_configuration_applied", "gateway_status")
    client.configure(7, command="ElectricalMeterCommandDisable")
    subscriber.expect_sequence(
        "electrical_meter_configuration_accepted", "electrical_meter_configuration_applied"
    )
    client.post(
        "/v1/nodes/7/balance-and-flags",
        {"balance": {"sign": 1, "coef": 5, "exp": 0}, "low_balance_flag": True},
    )
    subscriber.expect("electrical_meter_balance_and_flags_accepted")
    subscriber.assert_silent(0.1)

    client.register(7)
    registered, _ = subscriber.expect_sequence("node_registered", "node_firmware_version_changed")
    assert registered.payload["node_id"] == 7
    reading = subscriber.expect("electrical_meter_reading", timeout=2.0).payload
    assert reading["node_id"] == 7
    assert reading["state"] == STATE_METER_DISABLED
    assert reading["user_power_limit"] == FULL_CONFIGURATION["power_limit"]


def test_reregistration_keeps_state_and_energy(client, subscriber):
    start_cycle(client, subscriber)
    first = subscriber.expect("electrical_meter_reading", timeout=2.0).payload
    subscriber.expect_sequence("heartbeat_statistics", "gateway_status")
    client.configure(65276, command="ElectricalMeterCommandCalibrateStart")
    subscriber.expect_sequence(
        "electrical_meter_configuration_accepted", "electrical_meter_configuration_applied"
    )
    client.delete("/v1/nodes/65276")
    subscriber.expect("node_unregistered")
    client.register(65276)
    subscriber.expect_sequence("node_registered", "node_firmware_version_changed")

    messages = subscriber.collect(1.6)
    readings = [m.payload for m in messages if m.event == "electrical_meter_reading"]
    assert readings, [m.event for m in messages]
    # The kept record: energy continues from the earlier reading, state and
    # configuration from before unregistration, uptime from the new registration.
    assert readings[0]["energy"] == first["energy"]
    assert readings[0]["state"] == STATE_CALIBRATE
    assert readings[0]["user_power_limit"] == FULL_CONFIGURATION["power_limit"]
    assert readings[0]["uptime_secs"] <= 2


def test_heartbeat_thread_survives_a_failing_cycle(client, subscriber, emulator, monkeypatch):
    original = emulator.state.heartbeat_cycle
    calls = []

    def flaky(*args, **kwargs):
        calls.append(args)
        if len(calls) == 1:
            raise RuntimeError("synthetic cycle failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(emulator.state, "heartbeat_cycle", flaky)
    start_cycle(client, subscriber)
    reading = subscriber.expect("electrical_meter_reading", timeout=3.5)
    assert reading.payload["node_id"] == 65276
    assert len(calls) == 2
    assert emulator.heartbeat.running


def test_huge_period_does_not_break_the_heartbeat(client, subscriber, emulator):
    start_cycle(client, subscriber, period=2**32 - 1)
    subscriber.assert_silent(0.5)
    assert emulator.heartbeat.running
    client.init(period=1)
    subscriber.expect_sequence("driver_configuration_applied", "gateway_status")
    client.register(65276)
    subscriber.expect_sequence("node_registered", "node_firmware_version_changed")
    subscriber.expect("electrical_meter_reading", timeout=2.0)
