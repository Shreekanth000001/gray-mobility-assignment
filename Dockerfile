# syntax=docker/dockerfile:1.7

###############################################################################
# Stage 1 — builder
#
# Compilers, headers and pip's build machinery live here and are thrown away.
# Only the resolved virtualenv crosses into the runtime stage, which keeps the
# final image small and removes the toolchain an attacker could otherwise use.
###############################################################################
FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# build-essential is needed only if a dependency has no manylinux wheel.
# It never reaches the runtime image.
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential \
 && rm -rf /var/lib/apt/lists/*

# A venv (rather than --user or system site-packages) gives us one
# self-contained directory to COPY across, with no path guessing.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /build

# Copy the manifest alone first: this layer is cached and only invalidated
# when dependencies change, not on every source edit.
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt


###############################################################################
# Stage 2 — runtime
###############################################################################
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    APP_PORT=8000 \
    # SQLite file lives on a mounted volume, not in the image layer.
    DATABASE_URL="sqlite:////data/contacts.db"

# Run as an unprivileged user. Root in a container is still root on the host
# kernel if anything escapes the namespace.
RUN groupadd --system --gid 10001 appuser \
 && useradd  --system --uid 10001 --gid appuser --no-create-home appuser

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY --chown=appuser:appuser main.py models.py ./

# Writable location for the SQLite file. Declared as a volume so the data
# survives container replacement; see SYSTEM_DESIGN.md for why this is the
# weak point of the current design.
RUN mkdir -p /data && chown appuser:appuser /data
VOLUME ["/data"]

USER appuser
EXPOSE 8000

# Docker-level health check. python -c beats curl/wget here: the slim image
# ships neither, and installing one just for a probe adds attack surface.
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; \
sys.exit(0) if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2).status == 200 else sys.exit(1)"

# Exec form: uvicorn becomes PID 1 and receives SIGTERM directly, so
# Kubernetes' graceful shutdown actually drains in-flight requests.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
