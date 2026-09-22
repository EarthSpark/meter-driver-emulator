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
# Largest request body accepted; every spec body is a few hundred bytes.
MAX_BODY_BYTES = 1024 * 1024
# Socket timeout for one connection, so a stalled client does not hold a
# handler thread forever. Idle keep-alive connections close after this.
SOCKET_TIMEOUT_SECONDS = 60.0

# Path patterns with a node_id segment. The segment is validated as a uint64
# by the handler so a non-numeric id answers 400 rather than 404. The literal
# "register" segment belongs to POST /v1/nodes/register, not to a node.
NODE_PATH = re.compile(r"^/v1/nodes/(?!register$)(?P<node_id>[^/]+)$")
CONFIGURE_PATH = re.compile(r"^/v1/nodes/(?P<node_id>[^/]+)/configure-meter$")
BALANCE_PATH = re.compile(r"^/v1/nodes/(?P<node_id>[^/]+)/balance-and-flags$")


def load_openapi_template():
    """Load the committed spec document, converted by scripts/sync_openapi.py."""
    text = importlib.resources.files(__package__).joinpath("openapi.json").read_text(encoding="utf-8")
    return json.loads(text)


class HttpError(Exception):
    """A transport-level request problem answered in the ErrorResponse shape.

    `close` asks the handler to drop the connection afterwards, used when the
    request framing leaves no way to find the next request's start.
    """

    def __init__(self, status, error, message, close=False):
        super().__init__(message)
        self.status = status
        self.error = error
        self.close = close


class BurstTolerantHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer with a listen backlog that survives a burst of clients.

    socketserver's default request_queue_size is 5. The spec's HTTP contract
    invites a client to open a connection per request, so a handful of
    concurrent requests overruns that queue, and the kernel answers the
    overflow by resetting connections instead of letting them be served.
    """

    request_queue_size = socket.SOMAXCONN


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
        self.stopped = threading.Event()
        self._openapi_template = load_openapi_template()
        self._lifecycle_lock = threading.Lock()
        self._started = False
        self._shut_down = False
        server_class = BurstTolerantHTTPServer
        if ":" in bind[0]:
            server_class = type(
                "BurstTolerantHTTPServer6",
                (BurstTolerantHTTPServer,),
                {"address_family": socket.AF_INET6},
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
        return format_base_url(*self.address)

    def openapi_document(self, base_url):
        """Return the spec document with `info` and `x-meter-driver` rewritten for this instance."""
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

    def _mark_started(self):
        with self._lifecycle_lock:
            if self._started:
                raise RuntimeError("emulator already started")
            if self._shut_down:
                raise RuntimeError("emulator already shut down")
            self._started = True

    def start(self):
        """Serve on a background thread; returns the thread. Raises if already started."""
        self._mark_started()
        self.heartbeat.start()
        thread = threading.Thread(target=self.server.serve_forever, name="http-server", daemon=True)
        thread.start()
        return thread

    def serve_forever(self):
        """Serve on the calling thread until `shutdown` is called or the thread is interrupted."""
        self._mark_started()
        self.heartbeat.start()
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
        with self._lifecycle_lock:
            if self._shut_down:
                self.stopped.wait(timeout=5)
                return
            self._shut_down = True
            started = self._started
        log.info("shutting down")
        self.closing.set()
        self.heartbeat.stop()
        self.bus.close()
        # BaseServer.shutdown() waits on a flag that only serve_forever() sets,
        # so it must not be called for a server that never served.
        if started:
            self.server.shutdown()
        self.server.server_close()
        self.stopped.set()

    def request_shutdown(self):
        """Stop the server from a request handler, after that handler's response has gone out."""
        threading.Thread(target=self.shutdown, name="shutdown", daemon=True).start()


def format_base_url(host, port):
    """`http://host:port`, bracketing an IPv6 host."""
    if ":" in host:
        host = f"[{host}]"
    return f"http://{host}:{port}"


class Handler(BaseHTTPRequestHandler):
    """Routes one HTTP request to the emulator."""

    protocol_version = "HTTP/1.1"
    server_version = f"meter-driver-emulator/{__version__}"
    sys_version = ""
    timeout = SOCKET_TIMEOUT_SECONDS

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

    def __getattr__(self, name):
        """Route any other method through the dispatcher.

        handle_one_request() looks up `do_<METHOD>` and answers an HTML 501
        when it is missing; resolving every such name here means HEAD, PUT,
        PATCH, OPTIONS and unknown methods get the same 404/405 JSON answers.
        """
        if name.startswith("do_") and len(name) > 3:
            return lambda: self._dispatch(name[3:])
        raise AttributeError(name)

    def handle_one_request(self):
        """Read and dispatch one request; a peer that resets the connection ends it quietly.

        The reset surfaces while reading the next request line, outside the
        dispatcher, so it is caught here instead of reaching handle_error().
        """
        try:
            super().handle_one_request()
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError) as exc:
            self.close_connection = True
            log.debug("client %s reset the connection: %s", self.address_string(), exc)

    def send_error(self, code, message=None, explain=None):
        """Answer errors raised by the request parser itself in the ErrorResponse shape.

        BaseHTTPRequestHandler calls this for a malformed request line, an
        unsupported HTTP version or an over-long URI, and closes afterwards.
        """
        self.emulator.state.count_request()
        self.close_connection = True
        status = HTTPStatus(code)
        payload = {"error": status.phrase.lower().replace(" ", "_"), "message": message or status.description}
        try:
            self.send_json(status, payload)
        except OSError:
            log.debug("client %s disconnected before the error answer", self.address_string())

    def _dispatch(self, method):
        emulator = self.emulator
        emulator.state.count_request()
        path = urlsplit(self.path).path
        try:
            try:
                if emulator.closing.is_set():
                    raise HttpError(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        "service_unavailable",
                        "the emulator is shutting down",
                        close=True,
                    )
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
            except HttpError as exc:
                if exc.close:
                    self.close_connection = True
                self.send_json(exc.status, {"error": exc.error, "message": str(exc)})
            except OSError:
                raise
            except Exception as exc:
                log.exception("unhandled error serving %s %s", method, path)
                self.close_connection = True
                self.send_json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {"error": "internal_error", "message": f"{type(exc).__name__}: {exc}"},
                )
        except OSError as exc:
            # BrokenPipeError, ConnectionResetError, ConnectionAbortedError and
            # socket timeouts: the peer is gone, so there is nobody to answer.
            self.close_connection = True
            log.debug("client %s disconnected: %s", self.address_string(), exc)

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
        if self.headers.get("Transfer-Encoding"):
            # Without a Content-Length the request's end cannot be found, so
            # the connection is closed rather than parsing chunk framing.
            raise HttpError(
                HTTPStatus.BAD_REQUEST,
                "invalid_request",
                "Transfer-Encoding is not supported; send Content-Length",
                close=True,
            )
        header = (self.headers.get("Content-Length") or "0").strip()
        if not (header.isascii() and header.isdigit()):
            raise HttpError(
                HTTPStatus.BAD_REQUEST,
                "invalid_request",
                "Content-Length must be a non-negative integer",
                close=True,
            )
        length = int(header)
        if length > MAX_BODY_BYTES:
            raise HttpError(
                HTTPStatus.CONTENT_TOO_LARGE,
                "payload_too_large",
                f"request body exceeds {MAX_BODY_BYTES} bytes",
                close=True,
            )
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
        if self.close_connection:
            self.send_header("Connection", "close")
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        # A HEAD response carries the headers of the GET answer and no body.
        if self.command != "HEAD":
            self.wfile.write(body)
        self.wfile.flush()

    def send_accepted(self):
        """Send the spec's 202 `{accepted: true}`."""
        self.send_json(HTTPStatus.ACCEPTED, {"accepted": True})

    def apply(self, mutate):
        """Run `mutate()` on the state and publish the events it returns as one atomic step.

        The state lock is held across both, so the stream order matches the
        order state changes were applied, across concurrent requests and the
        heartbeat. Publication happens before the response is written, so a
        client that disconnects early still leaves consistent state and
        events.
        """
        emulator = self.emulator
        with emulator.state.lock:
            events = mutate()
            for event_type, data in events:
                emulator.bus.publish(event_type, data)
        return events

    def request_base_url(self):
        """`http://` plus the Host header, or the connection's own address when there is none."""
        host = self.headers.get("Host")
        if host:
            return f"http://{host}"
        local = self.connection.getsockname()
        return format_base_url(local[0], local[1])

    # -- routes -------------------------------------------------------------

    def get_openapi(self):
        """GET /openapi.json: the spec document with this instance's interface block."""
        self.send_json(HTTPStatus.OK, self.emulator.openapi_document(self.request_base_url()))

    def get_requirements(self):
        """GET /v1/requirements."""
        self.send_json(HTTPStatus.OK, {"required_fields": list(REQUIRED_INIT_FIELDS)})

    def post_init(self):
        """POST /v1/init: reset, store configuration, restart the heartbeat."""
        request = validation.validate_init(self.read_json_object())
        emulator = self.emulator

        def mutate():
            events = emulator.state.init(
                request.heartbeat_period_duration,
                request.channel,
                request.aes_key,
                validation.mask_aes_key(request.aes_key),
            )
            emulator.heartbeat.restart()
            return events

        self.apply(mutate)
        self.send_accepted()

    def post_register(self):
        """POST /v1/nodes/register."""
        request = validation.validate_register(self.read_json_object())
        self.apply(lambda: self.emulator.state.register(request))
        self.send_accepted()

    def delete_node(self, node_id_text):
        """DELETE /v1/nodes/{node_id}."""
        node_id = validation.path_node_id(node_id_text)
        self.apply(lambda: self.emulator.state.unregister(node_id))
        self.send_accepted()

    def post_configure(self, node_id_text):
        """POST /v1/nodes/{node_id}/configure-meter, or /v1/meters/configure when node_id_text is None."""
        node_id = validation.path_node_id(node_id_text) if node_id_text is not None else None
        request = validation.validate_configure(self.read_json_object(), node_id)
        self.apply(lambda: self.emulator.state.configure(request))
        self.send_accepted()

    def post_balance(self, node_id_text):
        """POST /v1/nodes/{node_id}/balance-and-flags."""
        node_id = validation.path_node_id(node_id_text)
        request = validation.validate_balance(self.read_json_object())
        self.apply(lambda: self.emulator.state.set_balance(node_id, request))
        self.send_accepted()

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
        """GET /v1/events: SSE stream, current gateway_status first, keep-alive comment when idle.

        `Last-Event-ID` is not honored: there is no replay, so events
        published while a client was disconnected are not delivered to it.
        """
        emulator = self.emulator
        # Snapshot and subscribe under the state lock so no event can slip
        # between the status the stream opens with and the first live event.
        with emulator.state.lock:
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
