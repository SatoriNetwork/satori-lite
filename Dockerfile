# Satori Lite - Lightweight Neuron Container
FROM python:3.10-slim

# System dependencies
RUN apt-get update && \
    apt-get install -y \
        ca-certificates \
        build-essential \
        cmake \
        git \
        libflatbuffers-dev \
        libleveldb-dev \
        liblmdb-dev \
        libsecp256k1-dev \
        libssl-dev \
        libzstd-dev \
        zlib1g-dev && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Build a glibc-compatible strfry binary for the embedded relay runtime.
RUN git clone --depth 1 https://github.com/hoytech/strfry.git /tmp/strfry && \
    cd /tmp/strfry && \
    git submodule update --init && \
    make setup-golpe && \
    make -j2 && \
    cp /tmp/strfry/strfry /usr/local/bin/strfry && \
    rm -rf /tmp/strfry

# Create directory structure
RUN mkdir -p /Satori/Lib /Satori/Engine /Satori/Neuron /Satori/Neuron/satorineuron/web

# Copy only the Python package source into the image.
# The package lives under src/satorilib in the repo, and /Satori/Lib must contain
# the package root directly for `import satorilib...` to resolve in prod the same
# way it does in dev mounts.
COPY --from=satorilib src/satorilib /Satori/Lib/satorilib
# Copy neuron code
COPY neuron-lite /Satori/Neuron
COPY engine-lite /Satori/Engine
COPY web /Satori/web
# Skills served by the neuron (e.g. the self-improvement skill at /api/skill)
COPY skills /Satori/skills

# Pristine baseline in repo layout + the build commit. The neuron diffs an
# operator's live edits against this to produce repo-relative, base-pinned
# patches for the self-improvement flow. Purely additive — it does not affect
# the runtime layout above. CI should pass --build-arg SATORI_GIT_SHA=$(git rev-parse HEAD).
ARG SATORI_GIT_SHA=""
COPY neuron-lite /Satori/src/neuron-lite
COPY engine-lite /Satori/src/engine-lite
COPY web /Satori/src/web
COPY skills /Satori/src/skills
RUN printf '%s' "${SATORI_GIT_SHA}" > /Satori/BUILD_SHA

# MCP server source. Runs client-side (where the AI runs); shipped here for
# discovery / docker cp. Not installed into the neuron runtime.
COPY mcp-server /Satori/mcp-server

# Copy requirements and install
COPY requirements.txt /Satori/requirements.txt
RUN pip install --upgrade pip && \
    pip install --no-cache-dir --retries 10 --timeout 120 -r /Satori/requirements.txt && \
    # Ensure ETH address derivation dependency is available at runtime.
    pip install --no-cache-dir --retries 10 --timeout 120 coincurve && \
    pip install --retries 10 --timeout 120 pytest

COPY tests /Satori/tests

# Set Python path - satorilib package lives at /Satori/Lib/satorilib in the image.
ENV PYTHONPATH="/Satori/Lib:/Satori/Neuron:/Satori/Engine:/Satori"

# Create symbolic links for docker-compose.yaml compatibility
# Remove existing directories first, then create symlinks
RUN rm -rf /Satori/Neuron/data /Satori/Neuron/models && \
    ln -s /Satori/Engine/db /Satori/Neuron/data && \
    ln -s /Satori/models /Satori/Neuron/models

# Make start.sh executable (entrypoint for docker-compose compatibility)
RUN chmod +x /Satori/Neuron/satorineuron/web/start.sh

# Working directory
WORKDIR /Satori

# Expose web UI port
EXPOSE 24601

# Default command - starts neuron + web UI on port 24601
CMD ["python", "/Satori/Neuron/start.py"]
