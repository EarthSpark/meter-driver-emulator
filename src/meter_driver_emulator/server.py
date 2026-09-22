"""HTTP+SSE server: the twelve spec routes, error shapes, and the event stream."""

import copy
import importlib.resources
import json
import logging
import queue
import re
import socket
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from meter_driver_emulator import SPEC_VERSION, __version__, validation
from meter_driver_emulator.events import EventBus
from meter_driver_emulator.heartbeat import Heartbeat
from meter_driver_emulator.state import DriverState, Nominal

log = logging.getLogger(__name__)

DEFAULT_BIND = ("127.0.0.1", 18080)
KEEPALIVE_SECONDS = 15.0
REQUIRED_INIT_FIELDS = ["heartbeat_period_duration", "aes_key"]

# Path patterns with a node_id segment. The segment is validated as a uint64
# by the handler so a non-numeric id answers 400 rather than 404.
NODE_PATH = re.compile(r"^/v1/nodes/(?P<node_id>[^/]+)$")
CONFIGURE_PATH = re.compile(r"^/v1/nodes/(?P<node_id>[^/]+)/configure-meter$")
BALANCE_PATH = re.compile(r"^/v1/nodes/(?P<node_id>[^/]+)/balance-and-flags$")


def load_openapi_template():
    """The committed spec document, converted by scripts/sync_openapi.py."""
    text = importlib.resources.files(__package__).joinpath("openapi.json").read_text(encoding="utf-8")
    return json.loads(text)


class Emulator:
    """One emulator instance: state, event bus, heartbeat thread and HTTP server.

    `bind` is `(host, port)`; port 0 picks a free port, readable afterwards
    from `address`. `keepalive_seconds` is the SSE idle interval before a
    comment line is written; the spec fixes it at 15 s and tests shorten it.
    """

    def __init__(self, bind=DEFAULT_BIND, nominal=None, keepalive_seconds=KEEPALIVE_SECONDS):
        self.state = DriverState(nominal or Nominal())
        self.bus = EventBus()
        self.heartbeat = Heartbeat(self.state, self.bus)
        self.keepalive_seconds = keepalive_seconds
        self.closing = threading.Event()
        self._openapi_template = load_openapi_template()
        self._serving = False
        self._shutdown_lock = threading.Lock()
        self._shut_down = False
        self.stopped = threading.Event()
        server_class = ThreadingHTTPServer
        if ":" in bind[0]:
            server_class = type(
                "ThreadingHTTPServer6", (ThreadingHTTPServer,), {"address_family": socket.AF_INET6}
            )
        self.server = server_class(bind, Handler)
        self.server.emulator = self

    @property
    def address(self):
        """`(host, port)` the server is bound to."""
        host, port = self.server.server_address[:2]
        return host, port

    @property
    def base_url(self):
        """`http://host:port` for the bound address."""
        host, port = self.address
        if ":" in host:
            host = f"[{host}]"
        return f"http://{host}:{port}"

    def openapi_document(self, base_url):
        """The spec document with `info` and `x-meter-driver` rewritten for this instance."""
        document = copy.deepcopy(self._openapi_template)
        document["info"] = {
            "title": "Meter Driver Emulator",
            "version": __version__,
            "description": (
                f"Meter driver emulator {__version__}: the required HTTP+SSE contract of the "
                f"Meter Driver Specification {SPEC_VERSION}, with synthetic meters."
            ),
        }
        document["x-meter-driver"] = {
            "interfaces": [{"type": "http", "label": "HTTP API", "base_url": base_url}],
            "default_interface": "http",
        }
        return document

    def start(self):
        """Serve on a background thread; returns the thread."""
        self.heartbeat.start()
        self._serving = True
        thread = threading.Thread(target=self.server.serve_forever, name="http-server", daemon=True)
        thread.start()
        return thread

    def serve_forever(self):
        """Serve on the calling thread until `shutdown` is called or the thread is interrupted."""
        self.heartbeat.start()
        self._serving = True
        try:
            self.server.serve_forever()
        finally:
            self.shutdown()

    def shutdown(self):
        """Stop the heartbeat, end every event stream, and close the listening socket.

        Idempotent: a second caller waits for the first to finish. Safe from
        a request handler's thread, from the serving thread after
        serve_forever() has returned, and for a server that was never started.
        """
        with self._shutdown_lock:
            if self._shut_down:
                self.stopped.wait(timeout=5)
                return
            self._shut_down = True
        log.info("shutting down")
        self.closing.set()
        self.heartbeat.stop()
        self.bus.close()
        # BaseServer.shutdown() waits on a flag that only serve_forever() sets,
        # so it must not be called for a server that never served.
        if self._serving:
            self.server.shutdown()
        self.server.server_close()
        self.stopped.set()

    def request_shutdown(self):
        """Stop the server from a request handler, after that handler's response has gone out."""
        threading.Thread(target=self.shutdown, name="shutdown", daemon=True).start()


class Handler(BaseHTTPRequestHandler):
    """Routes one HTTP request to the emulator."""

    protocol_version = "HTTP/1.1"
    server_version = f"meter-driver-emulator/{__version__}"
    sys_version = ""

    @property
    def emulator(self):
        """The Emulator that owns this server."""
        return self.server.emulator

    def log_message(self, format, *args):
        """Route http.server's access log through the logging module."""
        log.debug("%s %s", self.address_string(), format % args)

    # -- dispatch -----------------------------------------------------------

    def do_GET(self):
        """Dispatch a GET."""
        self._dispatch("GET")

    def do_POST(self):
        """Dispatch a POST."""
        self._dispatch("POST")

    def do_DELETE(self):
        """Dispatch a DELETE."""
        self._dispatch("DELETE")

    def _dispatch(self, method):
        self.emulator.state.count_request()
        path = urlsplit(self.path).path
        try:
            # Consume the body up front so an error answer leaves the
            # keep-alive connection positioned at the next request.
            self.body = self._read_body()
            route = self._route(method, path)
            if route is None:
                allowed = self._allowed_methods(path)
                if allowed:
                    self.send_json(
                        HTTPStatus.METHOD_NOT_ALLOWED,
                        {"error": "method_not_allowed", "message": f"{method} is not allowed on {path}"},
                        extra_headers={"Allow": ", ".join(allowed)},
                    )
                else:
                    self.send_json(
                        HTTPStatus.NOT_FOUND, {"error": "not_found", "message": f"no route for {path}"}
                    )
                return
            route()
        except validation.BadRequest as exc:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": exc.error, "message": str(exc)})
        except BrokenPipeError, ConnectionResetError:
            log.debug("client %s disconnected", self.address_string())

    def _route(self, method, path):
        if method == "GET":
            if path == "/openapi.json":
                return self.get_openapi
            if path == "/v1/requirements":
                return self.get_requirements
            if path == "/v1/events":
                return self.get_events
            if path == "/v1/status":
                return self.get_status
            if path == "/v1/healthz":
                return self.get_healthz
        elif method == "POST":
            if path == "/v1/init":
                return self.post_init
            if path == "/v1/nodes/register":
                return self.post_register
            if path == "/v1/meters/configure":
                return lambda: self.post_configure(None)
            if path == "/v1/shutdown":
                return self.post_shutdown
            match = CONFIGURE_PATH.match(path)
            if match:
                return lambda: self.post_configure(match.group("node_id"))
            match = BALANCE_PATH.match(path)
            if match:
                return lambda: self.post_balance(match.group("node_id"))
        elif method == "DELETE":
            match = NODE_PATH.match(path)
            if match:
                return lambda: self.delete_node(match.group("node_id"))
        return None

    def _allowed_methods(self, path):
        return [method for method in ("GET", "POST", "DELETE") if self._route(method, path) is not None]

    # -- helpers ------------------------------------------------------------

    def _read_body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise validation.BadRequest("Content-Length must be an integer") from None
        return self.rfile.read(length) if length > 0 else b""

    def read_json_object(self):
        """Parse the request body as a JSON object."""
        return validation.parse_json_object(self.body)

    def send_json(self, status, payload, extra_headers=None):
        """Write a JSON response with Content-Length."""
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()

    def send_accepted(self):
        """The spec's 202 `{accepted: true}`."""
        self.send_json(HTTPStatus.ACCEPTED, {"accepted": True})

    def publish(self, events):
        """Publish `(type, data)` tuples in order."""
        for event_type, data in events:
            self.emulator.bus.publish(event_type, data)

    # -- routes -------------------------------------------------------------

    def get_openapi(self):
        """GET /openapi.json: the spec document with this instance's interface block."""
        host = self.headers.get("Host") or self.emulator.base_url.removeprefix("http://")
        self.send_json(HTTPStatus.OK, self.emulator.openapi_document(f"http://{host}"))

    def get_requirements(self):
        """GET /v1/requirements."""
        self.send_json(HTTPStatus.OK, {"required_fields": list(REQUIRED_INIT_FIELDS)})

    def post_init(self):
        """POST /v1/init: reset, store configuration, restart the heartbeat."""
        request = validation.validate_init(self.read_json_object())
        events = self.emulator.state.init(
            request.heartbeat_period_duration,
            request.channel,
            request.aes_key,
            validation.mask_aes_key(request.aes_key),
        )
        self.emulator.heartbeat.restart()
        self.send_accepted()
        self.publish(events)

    def post_register(self):
        """POST /v1/nodes/register."""
        request = validation.validate_register(self.read_json_object())
        events = self.emulator.state.register(request)
        self.send_accepted()
        self.publish(events)

    def delete_node(self, node_id_text):
        """DELETE /v1/nodes/{node_id}."""
        node_id = validation.path_node_id(node_id_text)
        events = self.emulator.state.unregister(node_id)
        self.send_accepted()
        self.publish(events)

    def post_configure(self, node_id_text):
        """POST /v1/nodes/{node_id}/configure-meter, or /v1/meters/configure when node_id_text is None."""
        node_id = validation.path_node_id(node_id_text) if node_id_text is not None else None
        request = validation.validate_configure(self.read_json_object(), node_id)
        events = self.emulator.state.configure(request)
        self.send_accepted()
        self.publish(events)

    def post_balance(self, node_id_text):
        """POST /v1/nodes/{node_id}/balance-and-flags."""
        node_id = validation.path_node_id(node_id_text)
        request = validation.validate_balance(self.read_json_object())
        events = self.emulator.state.set_balance(node_id, request)
        self.send_accepted()
        self.publish(events)

    def get_status(self):
        """GET /v1/status."""
        self.send_json(HTTPStatus.OK, self.emulator.state.status())

    def get_healthz(self):
        """GET /v1/healthz."""
        self.send_json(HTTPStatus.OK, {"ok": True})

    def post_shutdown(self):
        """POST /v1/shutdown: answer, then stop the server."""
        self.send_accepted()
        self.emulator.request_shutdown()

    def get_events(self):
        """GET /v1/events: SSE stream, current gateway_status first, keep-alive comment when idle."""
        emulator = self.emulator
        subscriber = emulator.bus.subscribe(initial=("gateway_status", emulator.state.gateway_status()))
        self.close_connection = True
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            while not emulator.closing.is_set():
                try:
                    event = subscriber.get(timeout=emulator.keepalive_seconds)
                except queue.Empty:
                    self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
                    continue
                if event is None:
                    break
                self.wfile.write(event.to_sse().encode("utf-8"))
                self.wfile.flush()
        finally:
            emulator.bus.unsubscribe(subscriber)
