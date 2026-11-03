# Agent-DAW as a service.
#
# The image runs the canonical service-plugin surface: /health and
# /invoke. A host -- Codexify -- reaches it over HTTP, so the engine
# stays out of the host's process and out of the host's crash domain.
#
# The output root is a bind-mount, not a container-local path. When the
# DAW runs on the host and this runs in a container, the stems and the
# REAPER project have to land somewhere the host can open them. Set
# AGENTDAW_OUT_ROOT to the mounted directory and bind the same host
# directory here; everything else follows.

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    AGENTDAW_OUT_ROOT=/app/agentdaw_output

WORKDIR /app

# Dependencies first, so a code change does not invalidate the layer.
COPY pyproject.toml requirements.txt ./
RUN python -m pip install --upgrade pip setuptools wheel \
    && python -m pip install -r requirements.txt \
    && python -m pip install "fastapi>=0.115" "uvicorn>=0.30"

COPY agentdaw ./agentdaw

# The bind-mount target. Created here so the directory exists even when
# the operator forgets to mount it -- in which case the artifacts stay
# inside the container and the host simply will not find them.
RUN mkdir -p /app/agentdaw_output

EXPOSE 8000

# The health check is the same endpoint the plugin facade probes.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"

CMD ["uvicorn", "agentdaw.serve:create_app", "--factory", \
     "--host", "0.0.0.0", "--port", "8000"]
