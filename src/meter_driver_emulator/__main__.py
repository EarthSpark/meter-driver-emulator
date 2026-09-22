"""Command-line entry point: `meter-driver-emulator` or `python -m meter_driver_emulator`."""

import argparse
import logging
import sys

from meter_driver_emulator import __version__
from meter_driver_emulator.server import DEFAULT_BIND, Emulator
from meter_driver_emulator.state import Nominal

log = logging.getLogger("meter_driver_emulator")


def parse_bind(text):
    """Parse `HOST:PORT`, allowing a bracketed IPv6 host such as `[::1]:18080`."""
    host, sep, port = text.rpartition(":")
    if not sep or not port.isdigit():
        raise argparse.ArgumentTypeError(f"expected HOST:PORT, got {text!r}")
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    if not host:
        raise argparse.ArgumentTypeError(f"expected HOST:PORT, got {text!r}")
    return host, int(port)


def build_parser():
    """The CLI: bind address, nominal conditions, log level, version."""
    parser = argparse.ArgumentParser(
        prog="meter-driver-emulator",
        description="Meter Driver Specification emulator: a compliant HTTP+SSE driver with synthetic meters.",
    )
    parser.add_argument(
        "--bind",
        type=parse_bind,
        default=DEFAULT_BIND,
        metavar="HOST:PORT",
        help="listen address (default: %s:%d)" % DEFAULT_BIND,
    )
    parser.add_argument(
        "--voltage", type=float, default=230.0, help="nominal voltage in volts (default: 230)"
    )
    parser.add_argument(
        "--frequency", type=float, default=50.0, help="nominal frequency in hertz (default: 50)"
    )
    parser.add_argument(
        "--load-watts", type=float, default=100.0, help="load per energized meter in watts (default: 100)"
    )
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default="INFO",
        help="logging verbosity (default: INFO)",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def main(argv=None):
    """Run the emulator until interrupted or asked to shut down."""
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=args.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    emulator = Emulator(
        bind=args.bind,
        nominal=Nominal(voltage=args.voltage, frequency=args.frequency, load_watts=args.load_watts),
    )
    log.info("meter-driver-emulator %s listening on %s", __version__, emulator.base_url)
    try:
        emulator.serve_forever()
    except KeyboardInterrupt:
        emulator.shutdown()
    log.info("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
