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
so the surface is exactly what the spec describes. This release implements
spec v1.4.0.

## Running it

### With [uv](https://docs.astral.sh/uv/)

```sh
uvx --from git+https://github.com/EarthSpark/meter-driver-emulator \
    meter-driver-emulator                    # http://127.0.0.1:18080
```

Append `@` and a branch, tag or commit to pin a revision:

```sh
uvx --from git+https://github.com/EarthSpark/meter-driver-emulator@v0.1.0 \
    meter-driver-emulator
```

### With Docker

```sh
docker run --rm -p 18080:18080 ghcr.io/earthspark/meter-driver-emulator:main
```

Published for `linux/amd64` and `linux/arm64`. `main` follows the default
branch, releases are tagged `v<version>`, and any build can be addressed as
`sha-<commit>`.

### From a clone

```sh
git clone --recurse-submodules https://github.com/EarthSpark/meter-driver-emulator.git
cd meter-driver-emulator
uv run meter-driver-emulator --bind 0.0.0.0:18080
docker build -t meter-driver-emulator .
```

Options:

- `--bind HOST:PORT` — listen address, default `127.0.0.1:18080`.
- `--voltage`, `--frequency`, `--load-watts` — the nominal conditions every
  meter reports, default 230 V, 50 Hz and 100 W per energized meter.
- `--log-level` — `DEBUG`, `INFO`, `WARNING` or `ERROR`.
- `--version` — print `meter-driver-emulator <version>` and exit.

On start the emulator logs `listening on http://HOST:PORT`, which is the
address to give a client. Until `POST /v1/init` arrives it accepts every
route but runs no heartbeat cycles.

## What it does

| Route | Behavior |
| --- | --- |
| `GET /openapi.json` | The spec's OpenAPI document with this instance's `x-meter-driver` block: one `http` interface at the address the request arrived on. |
| `GET /v1/requirements` | `heartbeat_period_duration` and `aes_key`, matching the spec's `InitRequest`. |
| `POST /v1/init` | Clears every meter record, stores the configuration, restarts the heartbeat cycle, emits `driver_configuration_applied` and `gateway_status`. A heartbeat period of 0 disables periodic readings. The request, message and packet counters describe the process and are not reset. |
| `POST /v1/nodes/register` | Emits `node_registered` then `node_firmware_version_changed`, or `node_already_registered` when the node is currently registered. |
| `DELETE /v1/nodes/{node_id}` | Emits `node_unregistered` for a registered node, or `node_to_unregister_unknown` for any node not currently registered, including one already unregistered. The node's record is kept: registering it again keeps its configuration, reported state and accumulated energy, and restarts its uptime. |
| `POST /v1/nodes/{node_id}/configure-meter`, `POST /v1/meters/configure` | Emits `electrical_meter_configuration_accepted` then `electrical_meter_configuration_applied`. A command outside the spec's enum, or a negative `power_limit`, `current_limit` or unsigned field, emits `invalid_electrical_meter_configuration` instead and changes nothing; that event carries the parsed `node_id`, `command` and `configuration` (limits as numbers, unsigned fields as integers, unknown keys dropped). In the path form a body `node_id` is allowed only when it equals the path. The command sets the meter's reported state: Enable, Reboot, CalibrateFinish and EnableSts leave it On; Disable puts it in MeterDisabled; CalibrateStart in Calibrate. |
| `POST /v1/nodes/{node_id}/balance-and-flags` | Stores the balance and flag and emits `electrical_meter_balance_and_flags_accepted`. |
| `GET /v1/events` | SSE stream. Every subscriber first receives the current `gateway_status`, then every event as it happens. Messages carry `id:`, `event:` and `data:` lines with `data` = `{"type": ..., "data": {...}}`; a `: keep-alive` comment is sent after 15 seconds of silence. `Last-Event-ID` is not honored and nothing is replayed: events published while a client was disconnected are lost to it. A subscriber that falls 1000 events behind has its stream ended. |
| `GET /v1/status` | Always `connected: true`, `gateway_type: "emulator"`, the package version as `firmware_version`, null raw gateway fields. `messages_received` counts HTTP requests handled; `messages_sent` counts readings emitted plus configurations applied. |
| `GET /v1/healthz` | `{"ok": true}`. |
| `POST /v1/shutdown` | Accepts, then stops the server. |

Every `node_id` is a real meter as far as the emulator is concerned:
configuring or setting the balance of a node that was never registered
succeeds and emits the usual events, but does not register it, so it produces
no readings until `POST /v1/nodes/register` names it.

Malformed bodies, wrong JSON types, out-of-range unsigned integers, malformed
AES keys and missing required fields are answered with HTTP 400 and the
spec's `ErrorResponse` shape (`error: "invalid_request"`), and emit no event.
An unknown path answers 404 (`error: "not_found"`); a spec path with any other
method, including HEAD and OPTIONS, answers 405 (`error: "method_not_allowed"`)
with an `Allow` header. A body over 1 MiB answers 413, a malformed
`Content-Length` or a `Transfer-Encoding` header answers 400, and an
unexpected internal failure answers 500, all in the same shape; a connection
idle for 60 seconds is closed.

### Readings

Every `heartbeat_period_duration` seconds, each registered meter gets one
`electrical_meter_reading` (or `electrical_meter_reading_phased` when it was
registered with `request_phased_readings`), followed by one
`heartbeat_statistics` and one `gateway_status` for the cycle. The first
reading arrives one period after `POST /v1/init`; init also restarts the
cycle, and a period of 0 runs no cycles at all.

Readings are synthetic and steady by design. An energized meter reports the
configured nominal voltage and frequency, the configured constant load as
true power, apparent power = load / 0.99, current = load / (voltage × 0.99),
a power factor of 0.99, and `energy` accumulating by load × period / 3600 Wh
each cycle. `uptime_secs` counts from registration. A meter in MeterDisabled
or Calibrate reports zero current, power and power factor and its energy
stops growing, while voltage and frequency stay nominal. `user_power_limit`
reflects the last applied configuration's `power_limit`, or 0 before any
configuration.

The phased form carries the same fields plus `phases {a: true, b: false,
c: false}`, `computed_fields_version: 1`, every `*_a` field equal to its
unsuffixed value, and every `*_b` and `*_c` field 0.

In `heartbeat_statistics`, every node count and per-cycle packet count equals
the number of registered meters, the cumulative packet totals grow by that
number each cycle, the read-reply latency is a fixed 120 ms when any meter
was read, and the set-config latency is zero.

## Development

From a clone made as above:

```sh
uv sync --group dev
uv run pre-commit install
uv run pytest
uv run ruff format . && uv run ruff check .
```

On a clone made without `--recurse-submodules`, run `git submodule update
--init` before anything else.

After any build in the checkout, the emulator also runs on plain Python 3.14
or newer with no dependencies:

```sh
PYTHONPATH=src python -m meter_driver_emulator
```

### The spec submodule

`meter-driver-spec` pins the exact commit of the
[Meter Driver Specification](https://github.com/EarthSpark/meter-driver-spec)
this emulator implements, and is the only copy of the spec in the repository.

The spec is authored as YAML. The emulator serves it at `/openapi.json` but
declares no runtime dependencies, and the standard library has no YAML parser,
so `hatch_build.py` converts the pinned YAML into
`src/meter_driver_emulator/openapi.json` at build time and it ships as package
data. That file is generated, gitignored and never committed: the spec is the
source of truth, and this is a conversion of it.

Because it is a build hook, it runs for `uv sync`, `uv build`, `pip install .`
and the Docker build alike. To move to a new spec release, advance the
submodule to that commit and rebuild:

```sh
uv sync --reinstall-package meter-driver-emulator
```

`SPEC_VERSION` is read out of the generated document at import time, so the
spec version follows the submodule and is never written by hand. The document
the emulator serves is that file with `info` and `x-meter-driver` replaced at
request time.

The container build is two stages. The first has the submodule and produces a
wheel; the second installs only that wheel, so the spec is an input to the
build and never part of the shipped image.

The tests use only the standard library and pytest: `tests/conftest.py`
starts an emulator on a free port and reads `/v1/events` with
`http.client`, and `tests/openapi_schema.py` checks every response and
event against the served document's schemas.

## License

[Apache 2.0](LICENSE).
