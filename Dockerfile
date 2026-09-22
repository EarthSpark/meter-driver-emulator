# syntax=docker/dockerfile:1

# Stage 1 builds a wheel. This is the only stage that sees the spec submodule:
# hatch_build.py converts its YAML into the package's openapi.json, which then
# travels inside the wheel.
FROM python:3.14-slim AS builder
WORKDIR /src
COPY pyproject.toml README.md LICENSE hatch_build.py ./
COPY src ./src
COPY meter-driver-spec ./meter-driver-spec
RUN pip install --no-cache-dir build \
    && python -m build --wheel --outdir /dist

# Stage 2 is the shipped image: the slim interpreter plus the installed
# package. The emulator has no runtime dependencies beyond the standard
# library, and neither the spec checkout nor the source tree is present here.
FROM python:3.14-slim
WORKDIR /app
RUN --mount=from=builder,source=/dist,target=/dist \
    pip install --no-cache-dir /dist/*.whl

# The HTTP API and SSE stream. The bind address is fixed to every interface so
# the port is reachable from outside the container. The port can be changed by
# passing --bind as the container command, but the HEALTHCHECK below probes
# 18080, so a different port also needs the healthcheck overridden
# (docker run --health-cmd, or healthcheck: in compose).
EXPOSE 18080
HEALTHCHECK --interval=10s --timeout=3s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:18080/v1/healthz', timeout=2)"]

ENTRYPOINT ["meter-driver-emulator"]
CMD ["--bind", "0.0.0.0:18080"]
