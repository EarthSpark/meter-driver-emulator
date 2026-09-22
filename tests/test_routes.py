"""Route responses, validation errors, and the events each route emits."""

import http.client
import json
import socket

import pytest
from conftest import FULL_CONFIGURATION, HEX_KEY, Client, make_emulator

from meter_driver_emulator import SPEC_VERSION, __version__
from meter_driver_emulator.server import MAX_BODY_BYTES
from meter_driver_emulator.state import STATE_ON


def raw_request(address, payload):
    """Send raw bytes and parse the single response the server answers with before closing."""
    with socket.create_connection(address, timeout=5) as sock:
        sock.sendall(payload)
        chunks = []
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
    head, _, body = b"".join(chunks).partition(b"\r\n\r\n")
    status_line, *header_lines = head.decode("latin-1").split("\r\n")
    headers = dict(line.split(": ", 1) for line in header_lines)
    return int(status_line.split(" ")[1]), headers, json.loads(body)


SPEC_PATHS = {
    "/openapi.json",
    "/v1/requirements",
    "/v1/init",
    "/v1/nodes/register",
    "/v1/nodes/{node_id}",
    "/v1/nodes/{node_id}/configure-meter",
    "/v1/meters/configure",
    "/v1/nodes/{node_id}/balance-and-flags",
    "/v1/events",
    "/v1/status",
    "/v1/healthz",
    "/v1/shutdown",
}


def assert_accepted(response, validator):
    assert response.status == 202
    validator.assert_valid("AcceptedResponse", response.json)
    assert response.json == {"accepted": True}


def assert_bad_request(response, validator):
    assert response.status == 400
    validator.assert_valid("ErrorResponse", response.json)
    assert response.json["error"] == "invalid_request"
    assert isinstance(response.json["message"], str) and response.json["message"]


# -- CAP-1 discover -----------------------------------------------------------


def test_openapi_document_lists_spec_paths_and_this_interface(client, emulator, validator):
    host, port = emulator.address
    response = client.get("/openapi.json", headers={"Host": f"{host}:{port}"})
    assert response.status == 200
    assert response.headers["Content-Type"] == "application/json"
    document = response.json
    assert document["openapi"].startswith("3.")
    # The document itself is the twelfth route; the other eleven are in paths.
    assert set(document["paths"]) | {"/openapi.json"} == SPEC_PATHS
    assert len(document["components"]["schemas"]["Event"]["oneOf"]) == 15
    assert document["info"]["title"] == "Meter Driver Emulator"
    assert document["info"]["version"] == __version__
    assert SPEC_VERSION in document["info"]["description"]
    assert document["x-meter-driver"] == {
        "interfaces": [{"type": "http", "label": "HTTP API", "base_url": f"http://{host}:{port}"}],
        "default_interface": "http",
    }
    # The advertised base_url answers /v1/healthz.
    base_url = document["x-meter-driver"]["interfaces"][0]["base_url"]
    assert base_url == emulator.base_url
    health = client.get("/v1/healthz")
    validator.assert_valid("HealthResponse", health.json)


def test_openapi_base_url_follows_host_header(client):
    response = client.get("/openapi.json", headers={"Host": "driver.example:18080"})
    assert response.json["x-meter-driver"]["interfaces"][0]["base_url"] == "http://driver.example:18080"


# -- CAP-2 requirements ---------------------------------------------------------


def test_requirements_match_served_init_request(client, validator):
    response = client.get("/v1/requirements")
    assert response.status == 200
    validator.assert_valid("RequirementsResponse", response.json)
    served = client.get("/openapi.json").json["components"]["schemas"]["InitRequest"]["required"]
    assert response.json["required_fields"] == served == ["heartbeat_period_duration", "aes_key"]


# -- CAP-3 init -----------------------------------------------------------------


def test_init_clears_registrations_and_emits_configuration(client, subscriber, validator):
    assert_accepted(client.register(65276, mac=65276), validator)
    subscriber.expect_sequence("node_registered", "node_firmware_version_changed")

    assert_accepted(client.init(period=60, channel=25, aes_key=HEX_KEY), validator)
    applied, status = subscriber.expect_sequence("driver_configuration_applied", "gateway_status")
    assert applied.payload == {"masked_aes_key": "8E..B0", "channel": 25, "heartbeat_period_duration": 60}
    assert status.payload["connected"] is True

    assert_accepted(client.delete("/v1/nodes/65276"), validator)
    assert subscriber.expect("node_to_unregister_unknown").payload == {"node_id": 65276}


def test_init_missing_aes_key_is_400_without_event(client, subscriber, validator):
    response = client.post("/v1/init", {"heartbeat_period_duration": 60, "channel": 25})
    assert_bad_request(response, validator)
    subscriber.assert_silent()


def test_init_byte_array_key_masks_from_hex(client, subscriber, validator):
    key = [0x8E, 0x11, 0x22, 0x33, 0x44, 0x55, 0x66, 0x77, 0x88, 0x99, 0xAA, 0xBB, 0xCC, 0xDD, 0xEE, 0xB0]
    assert_accepted(client.init(aes_key=key), validator)
    assert subscriber.expect("driver_configuration_applied").payload["masked_aes_key"] == "8E..B0"


def test_init_fifteen_byte_key_is_400(client, subscriber, validator):
    assert_bad_request(client.init(aes_key=[1] * 15), validator)
    assert_bad_request(client.init(aes_key=[256] + [1] * 15), validator)
    assert_bad_request(client.init(aes_key="8e11"), validator)
    subscriber.assert_silent()


def test_init_channel_defaults_to_zero(client, subscriber, validator):
    assert_accepted(client.init(channel=None), validator)
    assert subscriber.expect("driver_configuration_applied").payload["channel"] == 0


@pytest.mark.parametrize(
    "raw",
    [
        b"not json",
        b"[]",
        b"42",
        b"",
        b'{"heartbeat_period_duration": "60", "aes_key": "%s"}' % HEX_KEY.encode(),
        b'{"heartbeat_period_duration": 60.0, "aes_key": "%s"}' % HEX_KEY.encode(),
        b'{"heartbeat_period_duration": -1, "aes_key": "%s"}' % HEX_KEY.encode(),
        b'{"heartbeat_period_duration": true, "aes_key": "%s"}' % HEX_KEY.encode(),
    ],
)
def test_init_structural_errors_are_400(client, subscriber, validator, raw):
    assert_bad_request(client.request("POST", "/v1/init", raw=raw), validator)
    subscriber.assert_silent(0.1)


# -- CAP-4 register and unregister ----------------------------------------------


def test_register_then_repeat(client, subscriber, validator):
    body = {"node_id": 65276, "node_type": "SMRSDRF", "mac": 65276}
    assert_accepted(client.post("/v1/nodes/register", body), validator)
    registered, firmware = subscriber.expect_sequence("node_registered", "node_firmware_version_changed")
    assert registered.payload == {"node_id": 65276, "source_type": 1}
    assert firmware.payload == {"node_id": 65276, "firmware_version": {"major": 0, "minor": 1, "patch": 0}}

    assert_accepted(client.post("/v1/nodes/register", body), validator)
    assert subscriber.expect("node_already_registered").payload == {"node_id": 65276}
    subscriber.assert_silent()


def test_register_keeps_supplied_firmware_version(client, subscriber):
    client.register(5, firmware_version={"major": 3, "minor": 2, "patch": 1})
    _, firmware = subscriber.expect_sequence("node_registered", "node_firmware_version_changed")
    assert firmware.payload["firmware_version"] == {"major": 3, "minor": 2, "patch": 1}


def test_register_missing_node_type_is_400(client, subscriber, validator):
    assert_bad_request(client.post("/v1/nodes/register", {"node_id": 65276}), validator)
    assert_bad_request(
        client.post("/v1/nodes/register", {"node_id": "65276", "node_type": "SMRSDRF"}), validator
    )
    assert_bad_request(client.post("/v1/nodes/register", {"node_id": 1, "node_type": 7}), validator)
    assert_bad_request(client.register(1, request_phased_readings="yes"), validator)
    assert_bad_request(client.register(1, balance={"sign": 1, "coef": -5, "exp": 0}), validator)
    subscriber.assert_silent()


def test_unregister_then_unknown(client, subscriber, validator):
    client.register(65276)
    subscriber.expect_sequence("node_registered", "node_firmware_version_changed")
    assert_accepted(client.delete("/v1/nodes/65276"), validator)
    assert subscriber.expect("node_unregistered").payload == {"node_id": 65276}
    assert_accepted(client.delete("/v1/nodes/65276"), validator)
    assert subscriber.expect("node_to_unregister_unknown").payload == {"node_id": 65276}


def test_unregister_non_numeric_id_is_400(client, subscriber, validator):
    assert_bad_request(client.delete("/v1/nodes/abc"), validator)
    assert_bad_request(client.delete("/v1/nodes/-1"), validator)
    subscriber.assert_silent()


# -- CAP-5 configure --------------------------------------------------------------


@pytest.mark.parametrize("form", ["path", "body"])
def test_configure_both_forms_accept_and_apply(client, subscriber, validator, form):
    if form == "path":
        response = client.post(
            "/v1/nodes/65276/configure-meter",
            {"command": "ElectricalMeterCommandEnable", "configuration": FULL_CONFIGURATION},
        )
    else:
        response = client.post(
            "/v1/meters/configure",
            {
                "node_id": 65276,
                "command": "ElectricalMeterCommandEnable",
                "configuration": FULL_CONFIGURATION,
            },
        )
    assert_accepted(response, validator)
    accepted, applied = subscriber.expect_sequence(
        "electrical_meter_configuration_accepted", "electrical_meter_configuration_applied"
    )
    assert accepted.payload == {"node_id": 65276}
    assert applied.payload == {"node_id": 65276, "configuration": FULL_CONFIGURATION}
    subscriber.assert_silent()


def test_configure_missing_configuration_field_is_400(client, subscriber, validator):
    partial = {k: v for k, v in FULL_CONFIGURATION.items() if k != "throttle_count_limit"}
    assert_bad_request(client.configure(1, configuration=partial), validator)
    assert_bad_request(
        client.post("/v1/nodes/1/configure-meter", {"configuration": FULL_CONFIGURATION}), validator
    )
    assert_bad_request(client.configure(1, command=7), validator)
    assert_bad_request(
        client.configure(1, configuration={**FULL_CONFIGURATION, "power_limit": "x"}), validator
    )
    assert_bad_request(
        client.configure(1, configuration={**FULL_CONFIGURATION, "startup_delay": 1.5}), validator
    )
    assert_bad_request(
        client.post("/v1/meters/configure", {"command": "ElectricalMeterCommandEnable"}), validator
    )
    subscriber.assert_silent()


@pytest.mark.parametrize(
    ("command", "configuration"),
    [
        ("Bogus", FULL_CONFIGURATION),
        ("ElectricalMeterCommandEnable", {**FULL_CONFIGURATION, "current_limit": -1}),
        ("ElectricalMeterCommandEnable", {**FULL_CONFIGURATION, "power_limit": -0.5}),
        ("ElectricalMeterCommandEnable", {**FULL_CONFIGURATION, "startup_delay": -1}),
    ],
)
def test_configure_rejected_emits_only_invalid_event(
    client, subscriber, emulator, validator, command, configuration
):
    client.register(65276)
    subscriber.expect_sequence("node_registered", "node_firmware_version_changed")
    client.configure(65276, command="ElectricalMeterCommandDisable")
    subscriber.expect_sequence(
        "electrical_meter_configuration_accepted", "electrical_meter_configuration_applied"
    )
    before = emulator.state.meters[65276]
    state_before, configuration_before = before.state, dict(before.configuration)

    response = client.post(
        "/v1/meters/configure", {"node_id": 65276, "command": command, "configuration": configuration}
    )
    assert_accepted(response, validator)
    invalid = subscriber.expect("invalid_electrical_meter_configuration")
    assert invalid.payload == {
        "invalid_configuration": {"node_id": 65276, "command": command, "configuration": configuration}
    }
    subscriber.assert_silent()
    assert emulator.state.meters[65276].state == state_before
    assert emulator.state.meters[65276].configuration == configuration_before


def test_configure_unregistered_node_succeeds_without_registering(client, subscriber, emulator, validator):
    assert_accepted(client.configure(9), validator)
    subscriber.expect_sequence(
        "electrical_meter_configuration_accepted", "electrical_meter_configuration_applied"
    )
    assert emulator.state.meters[9].registered is False
    assert emulator.state.meters[9].state == STATE_ON


# -- CAP-6 balance ------------------------------------------------------------------


def test_balance_on_never_registered_node(client, subscriber, emulator, validator):
    body = {"balance": {"sign": 1, "coef": 12345, "exp": -2}, "low_balance_flag": False}
    assert_accepted(client.post("/v1/nodes/1/balance-and-flags", body), validator)
    assert subscriber.expect("electrical_meter_balance_and_flags_accepted").payload == {"node_id": 1}
    assert emulator.state.meters[1].balance == {"sign": 1, "coef": 12345, "exp": -2}
    assert emulator.state.meters[1].registered is False


def test_balance_missing_flag_is_400(client, subscriber, validator):
    assert_bad_request(
        client.post("/v1/nodes/1/balance-and-flags", {"balance": {"sign": 1, "coef": 1, "exp": 0}}), validator
    )
    assert_bad_request(
        client.post("/v1/nodes/1/balance-and-flags", {"balance": {"sign": 1}, "low_balance_flag": True}),
        validator,
    )
    assert_bad_request(
        client.post(
            "/v1/nodes/x/balance-and-flags",
            {"balance": {"sign": 1, "coef": 1, "exp": 0}, "low_balance_flag": True},
        ),
        validator,
    )
    subscriber.assert_silent()


# -- CAP-9 status, health, shutdown -------------------------------------------------


def test_status_and_health(client, validator):
    response = client.get("/v1/status")
    assert response.status == 200
    validator.assert_valid("StatusResponse", response.json)
    assert response.json["connected"] is True
    assert response.json["gateway_type"] == "emulator"
    assert response.json["firmware_version"] == {"major": 0, "minor": 1, "patch": 0}
    assert response.json["gateway_firmware_raw"] is None
    assert response.json["gateway_bootloader_raw"] is None
    received = response.json["messages_received"]

    health = client.get("/v1/healthz")
    assert health.status == 200
    validator.assert_valid("HealthResponse", health.json)
    assert health.json == {"ok": True}

    # messages_received counts every HTTP request handled.
    assert client.get("/v1/status").json["messages_received"] == received + 2


def test_status_messages_sent_counts_applied_configurations(client):
    before = client.get("/v1/status").json["messages_sent"]
    client.configure(3)
    client.post(
        "/v1/meters/configure", {"node_id": 3, "command": "Bogus", "configuration": FULL_CONFIGURATION}
    )
    assert client.get("/v1/status").json["messages_sent"] == before + 1


def test_shutdown_stops_accepting_connections(client, emulator, validator):
    assert_accepted(client.post("/v1/shutdown"), validator)
    assert emulator.stopped.wait(timeout=5)
    assert emulator.heartbeat.running is False
    host, port = emulator.address
    with pytest.raises(OSError):
        conn = http.client.HTTPConnection(host, port, timeout=2)
        conn.request("GET", "/v1/healthz")
        conn.getresponse()


# -- CAP-10 unknown route ---------------------------------------------------------------


def test_unknown_route_is_404(client, validator):
    response = client.get("/nope")
    assert response.status == 404
    validator.assert_valid("ErrorResponse", response.json)
    assert response.json["error"] == "not_found"
    assert response.json["message"]
    assert client.post("/v1/nodes/1/unknown", {}).status == 404


def test_known_route_wrong_method_is_405(client, validator):
    response = client.delete("/v1/init")
    assert response.status == 405
    validator.assert_valid("ErrorResponse", response.json)
    assert response.json["error"] == "method_not_allowed"
    assert response.headers["Allow"] == "POST"


def test_keep_alive_connection_survives_error_responses(emulator):
    host, port = emulator.address
    conn = http.client.HTTPConnection(host, port, timeout=5)
    try:
        conn.request(
            "POST",
            "/v1/nodes/abc/configure-meter",
            body=b'{"x": 1}',
            headers={"Content-Type": "application/json"},
        )
        first = conn.getresponse()
        first.read()
        assert first.status == 400
        conn.request("GET", "/v1/healthz")
        second = conn.getresponse()
        assert second.status == 200
        assert second.read() == b'{"ok": true}'
    finally:
        conn.close()


def test_register_path_is_not_a_node_id(client, validator):
    response = client.delete("/v1/nodes/register")
    assert response.status == 405
    validator.assert_valid("ErrorResponse", response.json)
    assert response.headers["Allow"] == "POST"


@pytest.mark.parametrize("method", ["HEAD", "PUT", "PATCH", "OPTIONS", "FOO"])
def test_other_methods_answer_405_on_spec_paths_and_404_elsewhere(client, validator, method):
    response = client.request(method, "/v1/healthz")
    assert response.status == 405
    assert response.headers["Allow"] == "GET"
    if method == "HEAD":
        # No body on a HEAD answer; Content-Length still describes the JSON.
        assert response.json is None
        assert int(response.headers["Content-Length"]) > 0
    else:
        validator.assert_valid("ErrorResponse", response.json)
        assert response.json["error"] == "method_not_allowed"
    response = client.request(method, "/nope")
    assert response.status == 404
    if method != "HEAD":
        assert response.json["error"] == "not_found"


def test_head_answer_has_no_body_on_a_kept_alive_connection(emulator):
    host, port = emulator.address
    conn = http.client.HTTPConnection(host, port, timeout=5)
    try:
        conn.request("HEAD", "/v1/healthz")
        first = conn.getresponse()
        first.read()
        assert first.status == 405
        # Had the HEAD answer carried a body, it would corrupt this response.
        conn.request("GET", "/v1/healthz")
        second = conn.getresponse()
        assert second.status == 200
        assert json.loads(second.read()) == {"ok": True}
    finally:
        conn.close()


def test_malformed_request_line_and_bad_version_answer_json(client, emulator, validator):
    received = client.get("/v1/status").json["messages_received"]
    status, headers, body = raw_request(emulator.address, b"BOGUS\r\n\r\n")
    assert status == 400
    assert headers["Content-Type"] == "application/json"
    assert headers["Connection"] == "close"
    validator.assert_valid("ErrorResponse", body)
    assert body["error"] == "bad_request"

    status, _, body = raw_request(emulator.address, b"GET /v1/healthz HTTP/9.9\r\n\r\n")
    assert status == 505
    assert body["error"] == "http_version_not_supported"
    # Both parser-level errors were counted as requests, as was this status call.
    assert client.get("/v1/status").json["messages_received"] == received + 3


def test_unexpected_exception_answers_500_json(client, emulator, validator, monkeypatch):
    def broken():
        raise ZeroDivisionError("synthetic failure")

    monkeypatch.setattr(emulator.state, "status", broken)
    response = client.get("/v1/status")
    assert response.status == 500
    validator.assert_valid("ErrorResponse", response.json)
    assert response.json["error"] == "internal_error"
    assert "ZeroDivisionError" in response.json["message"]
    assert response.headers["Connection"] == "close"
    monkeypatch.undo()
    assert client.get("/v1/healthz").status == 200


def test_body_framing_limits(client, emulator, validator):
    host, port = emulator.address

    def send(headers, body=None):
        conn = http.client.HTTPConnection(host, port, timeout=5)
        try:
            conn.request("POST", "/v1/init", body=body, headers=headers)
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), json.loads(response.read())
        finally:
            conn.close()

    status, headers, body = send({"Content-Length": str(MAX_BODY_BYTES + 1)})
    assert status == 413
    validator.assert_valid("ErrorResponse", body)
    assert body["error"] == "payload_too_large"
    assert headers["Connection"] == "close"

    # "²" is a latin-1 character str.isdigit() accepts; int() does not.
    for bad_length in ("-5", "abc", "²"):
        status, headers, body = send({"Content-Length": bad_length})
        assert status == 400, bad_length
        assert body["error"] == "invalid_request"
        assert headers["Connection"] == "close"

    status, headers, body = send({"Transfer-Encoding": "chunked"})
    assert status == 400
    assert "Transfer-Encoding" in body["message"]
    assert headers["Connection"] == "close"

    # A body exactly at the limit is read and validated normally.
    padding = {"heartbeat_period_duration": 60, "aes_key": HEX_KEY, "pad": "x" * (MAX_BODY_BYTES - 200)}
    raw = json.dumps(padding).encode()
    assert len(raw) <= MAX_BODY_BYTES
    assert client.request("POST", "/v1/init", raw=raw).status == 202


def test_openapi_base_url_without_host_header_uses_connection_address(emulator):
    status, _, document = raw_request(
        emulator.address, b"GET /openapi.json HTTP/1.1\r\nConnection: close\r\n\r\n"
    )
    assert status == 200
    assert document["x-meter-driver"]["interfaces"][0]["base_url"] == emulator.base_url


def test_openapi_base_url_on_wildcard_bind_is_the_connected_address():
    emulator = make_emulator(bind=("0.0.0.0", 0))
    emulator.start()
    try:
        port = emulator.address[1]
        status, _, document = raw_request(
            ("127.0.0.1", port), b"GET /openapi.json HTTP/1.1\r\nConnection: close\r\n\r\n"
        )
        assert status == 200
        assert document["x-meter-driver"]["interfaces"][0]["base_url"] == f"http://127.0.0.1:{port}"
    finally:
        emulator.shutdown()


def test_ipv6_loopback(validator):
    try:
        emulator = make_emulator(bind=("::1", 0))
    except OSError as exc:
        pytest.skip(f"IPv6 loopback unavailable: {exc}")
    emulator.start()
    try:
        host, port = emulator.address
        assert emulator.base_url == f"http://[::1]:{port}"
        client = Client((host, port))
        health = client.get("/v1/healthz")
        assert health.status == 200
        validator.assert_valid("HealthResponse", health.json)
        document = client.get("/openapi.json").json
        assert document["x-meter-driver"]["interfaces"][0]["base_url"] == f"http://[::1]:{port}"
    finally:
        emulator.shutdown()


def test_start_twice_is_refused(emulator):
    with pytest.raises(RuntimeError):
        emulator.start()


def test_requests_after_shutdown_began_answer_503(emulator, validator):
    host, port = emulator.address
    conn = http.client.HTTPConnection(host, port, timeout=5)
    try:
        conn.request("GET", "/v1/healthz")
        first = conn.getresponse()
        first.read()
        assert first.status == 200
        emulator.closing.set()
        conn.request("GET", "/v1/healthz")
        second = conn.getresponse()
        body = json.loads(second.read())
        assert second.status == 503
        validator.assert_valid("ErrorResponse", body)
        assert body["error"] == "service_unavailable"
        assert second.getheader("Connection") == "close"
    finally:
        conn.close()


# -- validation edge cases -------------------------------------------------------------


def test_configure_path_form_body_node_id_must_match_path(client, subscriber, validator):
    body = {"node_id": 6, "command": "ElectricalMeterCommandEnable", "configuration": FULL_CONFIGURATION}
    response = client.post("/v1/nodes/5/configure-meter", body)
    assert_bad_request(response, validator)
    assert "does not match path" in response.json["message"]
    subscriber.assert_silent()

    body["node_id"] = 5
    assert_accepted(client.post("/v1/nodes/5/configure-meter", body), validator)
    accepted, _ = subscriber.expect_sequence(
        "electrical_meter_configuration_accepted", "electrical_meter_configuration_applied"
    )
    assert accepted.payload == {"node_id": 5}


@pytest.mark.parametrize(
    "raw",
    [
        b'{"heartbeat_period_duration": NaN, "aes_key": "%s"}' % HEX_KEY.encode(),
        b'{"heartbeat_period_duration": Infinity, "aes_key": "%s"}' % HEX_KEY.encode(),
        b'{"heartbeat_period_duration": -Infinity, "aes_key": "%s"}' % HEX_KEY.encode(),
        b'{"heartbeat_period_duration": %s, "aes_key": "%s"}' % (b"1" * 5000, HEX_KEY.encode()),
    ],
)
def test_non_json_constants_and_huge_literals_are_400(client, subscriber, validator, raw):
    assert_bad_request(client.request("POST", "/v1/init", raw=raw), validator)
    subscriber.assert_silent(0.1)


def test_non_finite_limits_are_400(client, subscriber, validator):
    raw = json.dumps({"command": "ElectricalMeterCommandEnable", "configuration": FULL_CONFIGURATION})
    raw = raw.replace("1500.0", "1e999").encode()
    assert_bad_request(client.request("POST", "/v1/nodes/1/configure-meter", raw=raw), validator)
    subscriber.assert_silent(0.1)


def test_upper_bounds_are_400(client, subscriber, validator):
    assert_bad_request(client.init(period=2**32), validator)
    assert_bad_request(client.init(channel=2**32), validator)
    assert_bad_request(
        client.configure(1, configuration={**FULL_CONFIGURATION, "startup_delay": 2**32}), validator
    )
    assert_bad_request(client.register(2**64), validator)
    assert_bad_request(client.delete(f"/v1/nodes/{2**64}"), validator)
    assert_bad_request(client.configure(2**64), validator)
    subscriber.assert_silent()
    # The largest representable values are accepted.
    assert_accepted(client.init(period=2**32 - 1, channel=2**32 - 1), validator)
    assert_accepted(client.register(2**64 - 1), validator)


# -- registration lifecycle --------------------------------------------------------------


def test_reregistration_after_delete_registers_again(client, subscriber, validator):
    client.register(65276)
    subscriber.expect_sequence("node_registered", "node_firmware_version_changed")
    client.delete("/v1/nodes/65276")
    subscriber.expect("node_unregistered")
    assert_accepted(client.register(65276), validator)
    registered, firmware = subscriber.expect_sequence("node_registered", "node_firmware_version_changed")
    assert registered.payload == {"node_id": 65276, "source_type": 1}
    assert firmware.payload["node_id"] == 65276
    subscriber.assert_silent()


def test_registration_after_configure_and_balance_of_unknown_node(client, subscriber, validator):
    client.configure(9)
    subscriber.expect_sequence(
        "electrical_meter_configuration_accepted", "electrical_meter_configuration_applied"
    )
    client.post(
        "/v1/nodes/9/balance-and-flags",
        {"balance": {"sign": 1, "coef": 1, "exp": 0}, "low_balance_flag": True},
    )
    subscriber.expect("electrical_meter_balance_and_flags_accepted")
    assert_accepted(client.register(9), validator)
    subscriber.expect_sequence("node_registered", "node_firmware_version_changed")
    subscriber.assert_silent()


def test_counters_persist_across_init(client):
    client.configure(3)
    before = client.get("/v1/status").json
    assert before["messages_sent"] >= 1
    client.init()
    after = client.get("/v1/status").json
    assert after["messages_sent"] == before["messages_sent"]
    assert after["messages_received"] == before["messages_received"] + 2
