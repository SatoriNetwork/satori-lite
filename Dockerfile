# Satori Lite - Lightweight Neuron Container
FROM python:3.10-slim

# System dependencies
RUN apt-get update && \
    apt-get install -y \
        build-essential \
        cmake \
        libleveldb-dev && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Create directory structure
RUN mkdir -p /Satori/Lib /Satori/Engine /Satori/Neuron

# Copy satori-lite code
COPY lib-lite /Satori/Lib
COPY neuron-lite /Satori/Neuron
COPY engine-lite /Satori/Engine
COPY web /Satori/web

# Copy requirements and install
COPY requirements.txt /Satori/requirements.txt
RUN pip install --upgrade pip && \
    pip install --no-cache-dir -r /Satori/requirements.txt

# Set Python path - include /Satori so 'from web.app' imports work
ENV PYTHONPATH="/Satori/Lib:/Satori/Neuron:/Satori/Engine:/Satori"

# Working directory
WORKDIR /Satori

# Expose web UI port
EXPOSE 5000

# Default command - starts neuron + web UI on port 5000
CMD ["python", "/Satori/Neuron/start.py"]
