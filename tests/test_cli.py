"""The command-line entry point: options, --version, --help, and starting with python -m."""

import argparse
import http.client
import json
import os
import queue
import re
import signal
import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from meter_driver_emulator import __version__
from meter_driver_emulator.__main__ import build_parser, main, nonnegative_float, parse_bind
from meter_driver_emulator.server import DEFAULT_BIND

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
OPTIONS = {"--bind", "--voltage", "--frequency", "--load-watts", "--log-level"}
CONSOLE_SCRIPT = Path(sys.executable).parent / "meter-driver-emulator"


def run_module(*args, timeout=10):
    """Run `python -m meter_driver_emulator` with PYTHONPATH=src and return the completed process."""
    env = {**os.environ, "PYTHONPATH": str(SRC)}
    return subprocess.run(
        [sys.executable, "-m", "meter_driver_emulator", *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


class Running:
    """A started emulator process whose stderr is pumped to a queue."""

    def __init__(self, command, env):
        self.process = subprocess.Popen(
            command, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        self.lines = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()
        self.port = self._wait_for_port()

    def _pump(self):
        for line in self.process.stderr:
            self.lines.put(line)

    def _wait_for_port(self):
        while True:
            try:
                line = self.lines.get(timeout=10)
            except queue.Empty:
                pytest.fail("emulator did not log its listening address")
            match = re.search(r"listening on http://127\.0\.0\.1:(\d+)", line)
            if match:
                return int(match.group(1))

    def request(self, method, path):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request(method, path)
            response = conn.getresponse()
            return response.status, json.loads(response.read())
        finally:
            conn.close()

    def kill_if_alive(self):
        if self.process.poll() is None:
            self.process.kill()
            self.process.wait()


def test_version_flag_prints_package_version():
    completed = run_module("--version")
    assert completed.returncode == 0
    assert completed.stdout.strip() == f"meter-driver-emulator {__version__}"


def test_version_flag_ignores_other_options():
    completed = run_module("--bind", "127.0.0.1:0", "--version")
    assert completed.returncode == 0
    assert completed.stdout.strip() == f"meter-driver-emulator {__version__}"


def test_main_version_exits_zero(capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])
    assert excinfo.value.code == 0
    assert capsys.readouterr().out.strip() == f"meter-driver-emulator {__version__}"


def test_help_lists_only_the_spec_options():
    completed = run_module("--help")
    assert completed.returncode == 0
    listed = set(re.findall(r"(?<![\w-])--[a-z][a-z-]*", completed.stdout))
    assert listed == OPTIONS | {"--help", "--version"}


def test_option_defaults_and_parsing():
    args = build_parser().parse_args([])
    assert args.bind == DEFAULT_BIND == ("127.0.0.1", 18080)
    assert (args.voltage, args.frequency, args.load_watts, args.log_level) == (230.0, 50.0, 100.0, "INFO")
    args = build_parser().parse_args(
        [
            "--bind",
            "0.0.0.0:18080",
            "--voltage",
            "120",
            "--frequency",
            "60",
            "--load-watts",
            "50",
            "--log-level",
            "DEBUG",
        ]
    )
    assert args.bind == ("0.0.0.0", 18080)
    assert (args.voltage, args.frequency, args.load_watts, args.log_level) == (120.0, 60.0, 50.0, "DEBUG")


def test_parse_bind():
    assert parse_bind("127.0.0.1:0") == ("127.0.0.1", 0)
    assert parse_bind("[::1]:18080") == ("::1", 18080)
    assert parse_bind("0.0.0.0:65535") == ("0.0.0.0", 65535)
    for bad in ("localhost", ":18080", "host:port", "host:", "host:65536", "host:١٢٣", "host:-1"):
        with pytest.raises(argparse.ArgumentTypeError):
            parse_bind(bad)


def test_nonnegative_float():
    assert nonnegative_float("0") == 0.0
    assert nonnegative_float("230.5") == 230.5
    for bad in ("-1", "nan", "inf", "-inf", "volts"):
        with pytest.raises(argparse.ArgumentTypeError):
            nonnegative_float(bad)


@pytest.mark.parametrize(
    "argv",
    [
        ["--log-level", "LOUD"],
        ["--voltage", "-1"],
        ["--frequency", "nan"],
        ["--load-watts", "inf"],
        ["--bind", "127.0.0.1:70000"],
    ],
)
def test_invalid_option_values_are_usage_errors(argv):
    completed = run_module(*argv)
    assert completed.returncode == 2
    assert argv[0] in completed.stderr


def test_bind_failure_is_a_one_line_usage_error():
    holder = socket.socket()
    holder.bind(("127.0.0.1", 0))
    holder.listen(1)
    port = holder.getsockname()[1]
    try:
        completed = run_module("--bind", f"127.0.0.1:{port}")
    finally:
        holder.close()
    assert completed.returncode == 2
    assert f"cannot bind http://127.0.0.1:{port}" in completed.stderr
    assert "Traceback" not in completed.stderr


def test_python_m_start_answers_healthz_and_shuts_down():
    env = {**os.environ, "PYTHONPATH": str(SRC)}
    running = Running([sys.executable, "-m", "meter_driver_emulator", "--bind", "127.0.0.1:0"], env)
    try:
        assert running.request("GET", "/v1/healthz") == (200, {"ok": True})
        assert running.request("POST", "/v1/shutdown") == (202, {"accepted": True})
        assert running.process.wait(timeout=10) == 0
    finally:
        running.kill_if_alive()


def test_python_m_exits_cleanly_on_sigint():
    env = {**os.environ, "PYTHONPATH": str(SRC)}
    running = Running([sys.executable, "-m", "meter_driver_emulator", "--bind", "127.0.0.1:0"], env)
    try:
        assert running.request("GET", "/v1/healthz") == (200, {"ok": True})
        running.process.send_signal(signal.SIGINT)
        assert running.process.wait(timeout=10) == 0
    finally:
        running.kill_if_alive()


@pytest.mark.skipif(
    not CONSOLE_SCRIPT.exists(), reason="console script not installed next to the interpreter"
)
def test_console_script_serves_without_pythonpath():
    env = {name: value for name, value in os.environ.items() if name != "PYTHONPATH"}
    running = Running([str(CONSOLE_SCRIPT), "--bind", "127.0.0.1:0"], env)
    try:
        assert running.request("GET", "/v1/healthz") == (200, {"ok": True})
        status, document = running.request("GET", "/openapi.json")
        assert status == 200
        assert document["info"]["version"] == __version__
        assert document["x-meter-driver"]["interfaces"][0]["base_url"] == f"http://127.0.0.1:{running.port}"
        assert running.request("POST", "/v1/shutdown") == (202, {"accepted": True})
        assert running.process.wait(timeout=10) == 0
    finally:
        running.kill_if_alive()
