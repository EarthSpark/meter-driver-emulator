# Changelog

## Unreleased

- Initial emulator: the specification's required HTTP+SSE contract, every
  node_id treated as an existing meter, synthetic readings per heartbeat.
  Tracks Meter Driver Specification v1.4.0.
- `/openapi.json` serves the specification's own document, converted from
  its YAML by `scripts/sync_openapi.py`, with `info` and `x-meter-driver`
  rewritten for the address the request arrived on.
- Command line: `--bind`, `--voltage`, `--frequency`, `--load-watts`,
  `--log-level`, `--version`; runnable with uv, `python -m`, or as a
  container.
