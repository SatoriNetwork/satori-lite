# Satori Lite - Lightweight Neuron Container
FROM python:3.10-slim

# System dependencies
# Kept byte-identical to Dockerfile.slim's builder stage so buildx's layer
# cache is shared between the `latest` and `slim` builds instead of
# reinstalling packages and recompiling strfry twice.
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
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
    rm -rf /var/lib/apt/lists/*

# Build a glibc-compatible strfry binary for the embedded relay runtime.
# Kept byte-identical to Dockerfile.slim's builder stage (see note above).
# STRFRY_REF pins the strfry version (branch or tag) for reproducible builds.
# STRFRY_MAKE_JOBS defaults to 2 to keep peak RAM under the 8GB Docker
# Desktop default; build.sh raises it based on available cores.
ARG STRFRY_REF=master
ARG STRFRY_MAKE_JOBS=2
RUN git clone --depth 1 --branch ${STRFRY_REF} https://github.com/hoytech/strfry.git /tmp/strfry && \
    cd /tmp/strfry && \
    git submodule update --init && \
    make setup-golpe && \
    make -j${STRFRY_MAKE_JOBS} && \
    cp /tmp/strfry/strfry /usr/local/bin/strfry && \
    strip /usr/local/bin/strfry && \
    rm -rf /tmp/strfry

# Shared requirements install. Kept byte-identical (same COPY destination,
# same RUN command) to Dockerfile.slim's builder stage so this layer is
# also served from cache on the second build.
COPY requirements.txt /tmp/requirements.txt
RUN pip install --upgrade pip && \
    pip install --no-cache-dir --retries 10 --timeout 120 -r /tmp/requirements.txt

# Optional foundation-model adapter (TimesFmAdapter). Install the CPU-only torch
# wheel FIRST so the subsequent timesfm install sees torch>=2.0.0 satisfied and
# does not pull the ~2 GB CUDA build. If this layer is removed, the engine simply
# hides TimesFM from the adapter choices (optional-import guard).
# Kept byte-identical to Dockerfile.slim's builder stage (see note above).
RUN pip install --no-cache-dir --retries 10 --timeout 120 \
        torch --index-url https://download.pytorch.org/whl/cpu && \
    pip install --no-cache-dir --retries 10 --timeout 120 timesfm==2.0.1

# --- everything below is specific to the full (non-slim) image ---

# Test runner for in-image test runs. (coincurve, the ETH address derivation
# dependency, is already pinned in requirements.txt - no separate install.)
RUN pip install --no-cache-dir --retries 10 --timeout 120 pytest

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
COPY tests /Satori/tests

# Set Python path - satorilib package lives at /Satori/Lib/satorilib in the image.
ENV PYTHONPATH="/Satori/Lib:/Satori/Neuron:/Satori/Engine:/Satori"

# Cache HuggingFace weights (e.g. TimesFM, ~800 MB) under the persisted models
# volume so they survive container restarts. /Satori/Neuron/models is symlinked
# to /Satori/models below, which the prod compose mounts from the host.
ENV HF_HOME="/Satori/Neuron/models/huggingface"

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
