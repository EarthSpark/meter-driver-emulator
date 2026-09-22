"""Test fixtures: an emulator on a free port, an HTTP client, and an SSE subscriber."""

import http.client
import json
import queue
import socket
import threading
import time
from dataclasses import dataclass

import pytest
from openapi_schema import SchemaValidator

from meter_driver_emulator.server import Emulator, load_openapi_template
from meter_driver_emulator.state import Nominal

# Short enough that a keep-alive test finishes quickly; the spec's 15 s
# default is asserted separately in test_events.py.
TEST_KEEPALIVE_SECONDS = 0.5

HEX_KEY = "8e112233445566778899aabbccddeeb0"

FULL_CONFIGURATION = {
    "power_limit": 1500.0,
    "current_limit": 10.0,
    "startup_delay": 2,
    "throttle_on_time": 12,
    "throttle_off_time": 34,
    "throttle_count_limit": 56,
}


@pytest.fixture(scope="session")
def validator():
    """Schema checker over the committed OpenAPI document."""
    return SchemaValidator(load_openapi_template())


@dataclass
class Response:
    """One HTTP response: status, headers and the decoded JSON body (None when empty)."""

    status: int
    headers: dict
    json: object


class Client:
    """Stdlib HTTP client bound to one emulator address; one connection per request."""

    def __init__(self, address):
        self.host, self.port = address

    def request(self, method, path, body=None, raw=None, headers=None):
        """Send a request; `body` is JSON-encoded, `raw` is sent as given bytes."""
        data = raw if raw is not None else (json.dumps(body).encode("utf-8") if body is not None else None)
        request_headers = dict(headers or {})
        if data is not None:
            request_headers.setdefault("Content-Type", "application/json")
        conn = http.client.HTTPConnection(self.host, self.port, timeout=5)
        try:
            conn.request(method, path, body=data, headers=request_headers)
            response = conn.getresponse()
            payload = response.read()
        finally:
            conn.close()
        decoded = json.loads(payload) if payload else None
        return Response(response.status, dict(response.getheaders()), decoded)

    def get(self, path, **kwargs):
        """GET."""
        return self.request("GET", path, **kwargs)

    def post(self, path, body=None, **kwargs):
        """POST with a JSON body."""
        return self.request("POST", path, body=body, **kwargs)

    def delete(self, path, **kwargs):
        """DELETE."""
        return self.request("DELETE", path, **kwargs)

    # -- spec operations ----------------------------------------------------

    def init(self, period=60, channel=25, aes_key=HEX_KEY):
        """POST /v1/init with the reference fields."""
        body = {"heartbeat_period_duration": period, "aes_key": aes_key}
        if channel is not None:
            body["channel"] = channel
        return self.post("/v1/init", body)

    def register(self, node_id, node_type="SMRSDRF", **extra):
        """POST /v1/nodes/register."""
        return self.post("/v1/nodes/register", {"node_id": node_id, "node_type": node_type, **extra})

    def configure(self, node_id, command="ElectricalMeterCommandEnable", configuration=None):
        """POST /v1/nodes/{node_id}/configure-meter."""
        body = {"command": command, "configuration": configuration or FULL_CONFIGURATION}
        return self.post(f"/v1/nodes/{node_id}/configure-meter", body)


@dataclass
class Message:
    """One parsed SSE message."""

    id: int
    event: str
    data: dict
    lines: list

    @property
    def payload(self):
        """The inner `data` object of the `{type, data}` envelope."""
        return self.data["data"]


@dataclass
class Comment:
    """One SSE comment line."""

    text: str


class Subscriber:
    """Reads GET /v1/events on a thread and hands out parsed messages.

    Every message is validated against the spec's `Event` schema when read.
    """

    def __init__(self, address, validator, timeout=10.0):
        self.validator = validator
        host, port = address
        # The timeout bounds every socket read, so a server that stops
        # answering (keep-alives arrive every TEST_KEEPALIVE_SECONDS) ends the
        # reader instead of hanging the suite.
        self.conn = http.client.HTTPConnection(host, port, timeout=timeout)
        self.conn.request("GET", "/v1/events")
        self.response = self.conn.getresponse()
        self.status = self.response.status
        self.headers = dict(self.response.getheaders())
        self.items = queue.Queue()
        self.closed = threading.Event()
        self._thread = threading.Thread(target=self._read, daemon=True)
        self._thread.start()

    def _read(self):
        lines = []
        try:
            while True:
                raw = self.response.readline()
                if not raw:
                    break
                line = raw.decode("utf-8").rstrip("\r\n")
                if line == "":
                    if lines:
                        self.items.put(self._parse(lines))
                        lines = []
                    continue
                if line.startswith(":"):
                    self.items.put(Comment(line))
                    continue
                lines.append(line)
        except OSError:
            # The connection was closed or timed out; the stream is over.
            pass
        except Exception as exc:
            # A framing or parsing problem is a test failure, not a timeout:
            # hand it to the consumer, which re-raises it from next().
            self.items.put(exc)
        finally:
            self.closed.set()

    @staticmethod
    def _parse(lines):
        fields = {}
        for line in lines:
            name, _, value = line.partition(":")
            fields[name] = value.lstrip(" ")
        return Message(int(fields["id"]), fields["event"], json.loads(fields["data"]), lines)

    def next(self, timeout=2.0, comments=False):
        """The next item; comments are skipped unless `comments` is set. None on timeout."""
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                item = self.items.get(timeout=remaining)
            except queue.Empty:
                return None
            if isinstance(item, Exception):
                raise AssertionError(f"event stream framing error: {item!r}") from item
            if isinstance(item, Comment) and not comments:
                continue
            if isinstance(item, Message):
                # The invalid-configuration event carries the parsed node_id,
                # command and configuration of the rejected request, so its
                # enum and minimum constraints cannot hold; required
                # properties and JSON types still must.
                strict = item.event != "invalid_electrical_meter_configuration"
                self.validator.assert_valid("Event", item.data, strict=strict)
                assert item.data["type"] == item.event, "event: line must name the envelope type"
            return item

    def expect(self, event_type, timeout=2.0):
        """The next message, which must be of `event_type`."""
        message = self.next(timeout=timeout)
        assert message is not None, f"timed out waiting for {event_type}"
        assert message.event == event_type, f"expected {event_type}, got {message.event}: {message.data}"
        return message

    def expect_sequence(self, *event_types, timeout=2.0):
        """Consecutive messages of exactly these types, in order."""
        return [self.expect(event_type, timeout=timeout) for event_type in event_types]

    def collect(self, duration):
        """Every message that arrives within `duration` seconds."""
        deadline = time.monotonic() + duration
        messages = []
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return messages
            message = self.next(timeout=remaining)
            if message is not None:
                messages.append(message)

    def assert_silent(self, duration=0.3):
        """No message arrives within `duration` seconds."""
        messages = self.collect(duration)
        assert messages == [], f"unexpected events: {[m.event for m in messages]}"

    def close(self):
        """Drop the TCP connection so the server sees the disconnect.

        HTTPConnection.close() alone leaves the response's file object holding
        the socket open; shutting the socket down closes it for the peer and
        unblocks the reader thread with EOF.
        """
        sock = self.conn.sock
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        self.response.close()
        self.conn.close()
        self.closed.wait(timeout=2)


def make_emulator(**kwargs):
    """An Emulator on a free loopback port with the test keep-alive interval."""
    kwargs.setdefault("bind", ("127.0.0.1", 0))
    kwargs.setdefault("keepalive_seconds", TEST_KEEPALIVE_SECONDS)
    kwargs.setdefault("nominal", Nominal())
    return Emulator(**kwargs)


@pytest.fixture
def emulator():
    """A running emulator, shut down after the test."""
    instance = make_emulator()
    instance.start()
    yield instance
    instance.shutdown()


@pytest.fixture
def client(emulator):
    """HTTP client for the running emulator."""
    return Client(emulator.address)


@pytest.fixture
def subscribe(emulator, validator):
    """Factory for SSE subscribers; by default the initial gateway_status is consumed and kept as `.initial`."""
    subscribers = []

    def factory(consume_initial=True):
        subscriber = Subscriber(emulator.address, validator)
        subscribers.append(subscriber)
        subscriber.initial = subscriber.expect("gateway_status") if consume_initial else None
        return subscriber

    yield factory
    for subscriber in subscribers:
        subscriber.close()


@pytest.fixture
def subscriber(subscribe):
    """One SSE subscriber with the initial gateway_status already consumed."""
    return subscribe()
