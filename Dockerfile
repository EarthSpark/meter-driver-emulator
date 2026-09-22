# syntax=docker/dockerfile:1
# The emulator has no runtime dependencies beyond the standard library, so the
# image is the slim interpreter plus the installed package.
FROM python:3.14-slim

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir . && rm -rf /app/src /app/pyproject.toml

# The HTTP API and SSE stream. The bind address is fixed to every interface so
# the port is reachable from outside the container; override the port by
# passing --bind as the container command.
EXPOSE 18080
HEALTHCHECK --interval=10s --timeout=3s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:18080/v1/healthz', timeout=2)"]

ENTRYPOINT ["meter-driver-emulator"]
CMD ["--bind", "0.0.0.0:18080"]
