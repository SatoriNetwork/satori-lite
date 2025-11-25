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

# Copy requirements and install
COPY requirements.txt /Satori/requirements.txt
RUN pip install --upgrade pip && \
    pip install --no-cache-dir -r /Satori/requirements.txt

# Set Python path
ENV PYTHONPATH="/Satori/Lib:/Satori/Neuron:/Satori/Engine"

# Working directory
WORKDIR /Satori/Neuron

# Default command
CMD ["python", "start.py"]
