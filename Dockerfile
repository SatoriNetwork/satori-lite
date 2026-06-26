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

# Copy requirements and install
COPY requirements.txt /Satori/requirements.txt
RUN pip install --upgrade pip && \
    pip install --no-cache-dir --retries 10 --timeout 120 -r /Satori/requirements.txt && \
    # Ensure ETH address derivation dependency is available at runtime.
    pip install --no-cache-dir --retries 10 --timeout 120 coincurve && \
    pip install --retries 10 --timeout 120 pytest

# Optional foundation-model adapter (TimesFmAdapter). Install the CPU-only torch
# wheel FIRST so the subsequent timesfm install sees torch>=2.0.0 satisfied and
# does not pull the ~2 GB CUDA build. If this layer is removed, the engine simply
# hides TimesFM from the adapter choices (optional-import guard).
RUN pip install --no-cache-dir --retries 10 --timeout 120 \
        torch --index-url https://download.pytorch.org/whl/cpu && \
    pip install --no-cache-dir --retries 10 --timeout 120 timesfm==2.0.1

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
