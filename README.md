# Meter Driver Emulator

A meter driver with no meters behind it, for developing against the
[Meter Driver Specification](https://github.com/EarthSpark/meter-driver-spec)
without any hardware.

It implements the specification's required HTTP+SSE contract in pure Python,
standard library only, and behaves as if every meter exists: any `node_id` an
application registers, configures or updates is treated as a real, reachable
meter, and each registered meter produces a synthetic reading every heartbeat
period.

The OpenAPI document it serves at `/openapi.json` is the specification's own,
so the surface is exactly what the spec describes. This release tracks spec
tag `v1.4.0`.

## Running it

With [uv](https://docs.astral.sh/uv/):

```sh
uv run meter-driver-emulator                      # http://127.0.0.1:18080
uv run meter-driver-emulator --bind 0.0.0.0:18080
```

With plain Python 3.14 or newer, no install:

```sh
PYTHONPATH=src python -m meter_driver_emulator
```

As a container:

```sh
docker build -t meter-driver-emulator .
docker run --rm -p 18080:18080 meter-driver-emulator
```

Options:

- `--bind HOST:PORT` — listen address, default `127.0.0.1:18080`.
- `--voltage`, `--frequency`, `--load-watts` — the nominal conditions every
  meter reports, default 230 V, 50 Hz and 100 W per energized meter.
- `--log-level` — `DEBUG`, `INFO`, `WARNING` or `ERROR`.

## What it does

| Route | Behavior |
| --- | --- |
| `GET /openapi.json` | The spec's OpenAPI document with this instance's `x-meter-driver` block: one `http` interface at the address the request arrived on. |
| `GET /v1/requirements` | `heartbeat_period_duration` and `aes_key`, matching the spec's `InitRequest`. |
| `POST /v1/init` | Clears all registrations, stores the configuration, emits `driver_configuration_applied` and `gateway_status`. A heartbeat period of 0 disables periodic readings. |
| `POST /v1/nodes/register` | Emits `node_registered` then `node_firmware_version_changed`, or `node_already_registered` for a repeat. |
| `DELETE /v1/nodes/{node_id}` | Emits `node_unregistered`, or `node_to_unregister_unknown` for a node that was never registered. |
| `POST /v1/nodes/{node_id}/configure-meter`, `POST /v1/meters/configure` | Emits `electrical_meter_configuration_accepted` then `electrical_meter_configuration_applied`. A command outside the spec's enum or a negative limit emits `invalid_electrical_meter_configuration` instead. The command sets the meter's reported state: Enable, Reboot, CalibrateFinish and EnableSts leave it On; Disable puts it in MeterDisabled; CalibrateStart in Calibrate. |
| `POST /v1/nodes/{node_id}/balance-and-flags` | Stores the balance and emits `electrical_meter_balance_and_flags_accepted`. |
| `GET /v1/events` | SSE stream. Every subscriber first receives the current `gateway_status`. Messages carry `id:`, `event:` and `data:` lines; a comment line is sent every 15 seconds as a keep-alive. |
| `GET /v1/status` | Always `connected: true`, `gateway_type: "emulator"`, with request and event counters. |
| `GET /v1/healthz` | `{"ok": true}`. |
| `POST /v1/shutdown` | Accepts, then stops the server. |

Malformed bodies, wrong types and missing required fields are answered with
HTTP 400 and the spec's `ErrorResponse` shape.

### Readings

Every `heartbeat_period_duration` seconds, each registered meter gets one
`electrical_meter_reading` (or `electrical_meter_reading_phased` when it was
registered with `request_phased_readings`), followed by one
`heartbeat_statistics` and one `gateway_status` for the cycle.

Readings are synthetic and steady by design. An energized meter reports the
configured nominal voltage and frequency, the configured constant load,
a power factor of 0.99, and energy accumulating from that load over the
period. A meter in MeterDisabled or Calibrate reports zero current and power.
`user_power_limit` reflects the last applied configuration's `power_limit`,
or 0 before any configuration.

## Development

```sh
uv sync --group dev
uv run pre-commit install
uv run pytest
uv run ruff format . && uv run ruff check .
```

The OpenAPI document under `src/meter_driver_emulator/openapi.json` is
generated from the spec repository's YAML:

```sh
uv run scripts/sync_openapi.py ../meter-driver-spec/openapi/meter-driver.yaml
```

When updating it, also update `SPEC_VERSION` in
`src/meter_driver_emulator/__init__.py`; CI checks that the two agree.

## License

[Apache 2.0](LICENSE).
