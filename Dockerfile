# syntax=docker/dockerfile:1.7
# Tunecast: MiniMax Music 3 song-generation server for RunPod.
#
# Base: the sglang-omni dev image (lmsysorg/sglang:v0.5.18 underneath, Ubuntu 24.04, CUDA 13.0.3),
# which already carries sglang, sgl-kernel, flashinfer with cubins, torch 2.13 and UCX in its
# system Python. Pinned by digest because the `dev` tag is mutable. See DECISIONS.md.
FROM hongccc/sglang-omni:dev@sha256:02a85f00438c901c72a2eb2ef738974a807f63af3d13084445604f3344067b19

ARG NGROK_VERSION=3.39.11
ARG NGROK_SHA256=cec0b4997fcc5f529dfc74bac89050354d11a915f968720600039738fdf330cf
ARG UV_VERSION=0.11.16

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    UV_LINK_MODE=copy \
    SGLANG_OMNI_AUTO_CLONE=0 \
    TUNECAST_DATA_DIR=/workspace

# tini reaps orphaned sgl-omni worker processes when the supervisor is PID 1.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tini \
 && rm -rf /var/lib/apt/lists/*

# ngrok agent, pinned by version and checksum. Credentials arrive at runtime via env.
RUN curl -fsSL -o /tmp/ngrok.tgz "https://bin.equinox.io/c/bNyj1mQVY4c/ngrok-v3-${NGROK_VERSION}-linux-amd64.tgz" \
 && echo "${NGROK_SHA256}  /tmp/ngrok.tgz" | sha256sum -c - \
 && tar -xzf /tmp/ngrok.tgz -C /usr/local/bin ngrok \
 && rm /tmp/ngrok.tgz \
 && ngrok version

# Pinned uv, then the inference package and our app deps on top of the base's system Python.
COPY --from=ghcr.io/astral-sh/uv:0.11.16 /uv /bin/uv
COPY docker/requirements.txt /app/docker/requirements.txt
RUN uv pip install --system --break-system-packages --prerelease=allow -r /app/docker/requirements.txt \
 && uv pip freeze --system > /app/docker/installed.txt \
 && rm -rf /root/.cache/uv /root/.cache/pip

# Application last, so code changes rebuild only this layer.
COPY pyproject.toml README.md /app/
COPY tunecast/ /app/tunecast/
RUN uv pip install --system --break-system-packages --no-deps /app \
 && python3 -c "import importlib.util as u, tunecast.boot; assert u.find_spec('sglang_omni'), 'sglang_omni missing'; print('tunecast import ok')"

WORKDIR /app
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=30m --retries=3 \
  CMD curl -fsS http://127.0.0.1:8080/health || exit 1
ENTRYPOINT ["tini", "--", "python3", "-m", "tunecast.boot"]
