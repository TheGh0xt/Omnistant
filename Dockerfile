# Personal Context Agent — Cloud Run image.
#
# Multi-stage so the runtime layer carries no build toolchain. The final image
# runs as a non-root user, which Cloud Run does not require but costs nothing.

FROM python:3.14-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build

# Dependencies first: this layer is cached until requirements.txt changes.
COPY requirements.txt .
RUN pip install --prefix=/install -r requirements.txt


FROM python:3.14-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/src \
    PORT=8080

RUN useradd --create-home --uid 1000 agent
WORKDIR /app

COPY --from=builder /install /usr/local
COPY src/ ./src/
COPY migrations/ ./migrations/

USER agent
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,os,sys; sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",8080)}/healthz', timeout=4).status==200 else 1)"

# Cloud Run injects $PORT and terminates TLS in front of us, so plain HTTP here.
# One worker: the agent holds an in-process ADK session service, and Cloud Run
# scales by adding instances rather than threads.
# JSON form (so no implicit shell wraps the process) with an explicit `sh -c`
# because $PORT must be expanded at runtime; `exec` hands PID 1 to uvicorn so
# Cloud Run's SIGTERM reaches it and shutdown hooks run.
CMD ["/bin/sh", "-c", "exec uvicorn main:app --host 0.0.0.0 --port ${PORT} --workers 1 --proxy-headers"]
