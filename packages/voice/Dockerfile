# Open Persona — `persona-voice` production image (app: open-persona-voice).
#
# ONE process: uvicorn runs the FastAPI app AND, with
# PERSONA_VOICE_AGENT_INPROCESS=true, the LiveKit agent worker runs IN-PROCESS
# inside that same app (build_app → InProcessAgentLauncher). The service
# connects OUT to a LiveKit server (packages/livekit, or LiveKit Cloud as the
# fallback); there is no LiveKit server in this image.
#
# Builds for linux/amd64 and linux/arm64 from this one file (the only
# architecture-specific step is the patchelf fetch, docker/patchelf.sh).
#
# Single uvicorn worker: the in-process agent launcher owns per-session state
# (one RLS engine + state machine per WebRTC session); never autoscale.
#
# Mirrors packages/api/Dockerfile: uv monorepo frozen install of the workspace
# members (core + runtime + voice). Build context is the REPO ROOT — the COPY
# paths assume it, so invoke `flyctl deploy -c packages/voice/fly.toml` from the
# repo root.

# ---------- builder ----------
FROM python:3.12-slim AS builder
ENV DEBIAN_FRONTEND=noninteractive \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv
# uv's download ceiling. The 30s default is fine on a fast native builder and not
# fine on an emulated cross-build or a slow link: torch and pydantic-core are big
# enough that one transient stall fails the entire sync. Raises the ceiling only;
# a healthy download is unaffected.
ENV UV_HTTP_TIMEOUT=300
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates build-essential libpq-dev \
    && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.6 /uv /usr/local/bin/uv

WORKDIR /app
# Copy workspace pyproject + lock first to leverage the layer cache.
COPY pyproject.toml uv.lock ./
COPY packages/core/pyproject.toml packages/core/pyproject.toml
COPY packages/runtime/pyproject.toml packages/runtime/pyproject.toml
COPY packages/api/pyproject.toml packages/api/pyproject.toml
COPY packages/voice/pyproject.toml packages/voice/pyproject.toml
# Provision the venv with all workspace members (frozen install) so the lock
# stays authoritative; matches the api image so one uv.lock serves both.
COPY packages/ packages/
RUN uv sync --frozen --all-packages --no-dev

# Some prebuilt native wheels ship .so files whose PT_GNU_STACK segment is marked
# executable. glibc 2.41 and later (this image's Debian 13 python:3.12-slim base)
# refuse to dlopen a library that asks for an executable stack:
#   OSError: ... silero_vad_lite.so: cannot enable executable stack ...
# silero-vad-lite 0.2.x was one (the VAD now runs on onnxruntime instead). The
# libraries a call loads are checked by tests/unit/stt/test_native_libraries.py; this
# step stays as a guard for every other wheel in the venv: it clears the exec-stack
# flag on every shared object. --clear-execstack needs patchelf 0.18+ (the apt/pip
# builds are older), so docker/patchelf.sh fetches the static release binary for the
# build's architecture (amd64 or arm64) and verifies its pinned SHA256 first.
# TARGETARCH is set by BuildKit; the dpkg fallback covers the legacy builder.
ARG TARGETARCH
RUN sh /app/packages/voice/docker/patchelf.sh "${TARGETARCH:-$(dpkg --print-architecture)}" \
    && find /opt/venv \( -name '*.so' -o -name '*.so.*' \) -print0 \
        | xargs -0 -r -n1 sh -c 'patchelf --clear-execstack "$0" 2>/dev/null || true'

# Prove on the target architecture, before the image ships, that the native code a call
# needs loads: LiveKit's FFI library through dlopen, then ONNX Runtime with the bundled
# Silero model scoring one window. A wrong-architecture wheel or a library the loader
# refuses fails the build here instead of the first call.
RUN /opt/venv/bin/python -I -c "from livekit.rtc._ffi_client import get_ffi_lib; get_ffi_lib(); import numpy as np; from persona_voice.stt.silero_onnx import SileroVAD; score = SileroVAD(16000).process(np.zeros(512, dtype=np.float32)); assert 0.0 <= score <= 1.0, score; print('voice native libraries load, silence scores', score)"

# ---------- runtime ----------
FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:${PATH}"
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 ca-certificates curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 1000 --create-home --shell /bin/bash persona

COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /app /app

WORKDIR /app
USER persona

EXPOSE 8001

# Single uvicorn worker (in-process agent launcher owns per-session state).
# --proxy-headers because Fly terminates TLS upstream.
CMD ["uvicorn", "persona_voice.http.app:create_app", "--factory", \
     "--host", "0.0.0.0", "--port", "8001", \
     "--workers", "1", "--proxy-headers", "--forwarded-allow-ips=*"]
