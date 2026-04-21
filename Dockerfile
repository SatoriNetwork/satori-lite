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
    make -j"$(nproc)" && \
    cp /tmp/strfry/strfry /usr/local/bin/strfry && \
    rm -rf /tmp/strfry

# Create directory structure
RUN mkdir -p /Satori/Lib /Satori/Engine /Satori/Neuron /Satori/Neuron/satorineuron/web

# Copy satorilib from external build context (passed via --build-context satorilib=...)
COPY --from=satorilib satorilib /Satori/Lib/satorilib
# Copy neuron code
COPY neuron-lite /Satori/Neuron
COPY engine-lite /Satori/Engine
COPY web /Satori/web

# Copy requirements and install
COPY requirements.txt /Satori/requirements.txt
RUN pip install --upgrade pip && \
    pip install --no-cache-dir -r /Satori/requirements.txt && \
    # Ensure ETH address derivation dependency is available at runtime.
    pip install --no-cache-dir coincurve && \
    pip install pytest

COPY tests /Satori/tests

# Set Python path - satorilib lives under /Satori/Lib/satorilib/src in the image.
ENV PYTHONPATH="/Satori/Lib/satorilib/src:/Satori/Neuron:/Satori/Engine:/Satori"

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
