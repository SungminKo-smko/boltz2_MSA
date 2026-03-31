FROM nvidia/cuda:12.2.2-cudnn8-devel-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive
ENV HF_HOME=/cache
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# System deps + Python 3.11
RUN apt-get update && apt-get install -y --no-install-recommends \
        software-properties-common && \
    add-apt-repository ppa:deadsnakes/ppa && \
    apt-get update && apt-get install -y --no-install-recommends \
        python3.11 python3.11-venv python3.11-dev \
        build-essential cmake git \
        libffi-dev libssl-dev libhdf5-dev \
        libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

RUN python3.11 -m ensurepip --upgrade && \
    python3.11 -m pip install --no-cache-dir --upgrade pip setuptools wheel && \
    ln -sf /usr/bin/python3.11 /usr/local/bin/python && \
    ln -sf /usr/bin/python3.11 /usr/local/bin/python3

# Install boltz2 with CUDA support
RUN pip install --no-cache-dir "boltz[cuda]==2.2.0"

# Pre-download model weights (cache buster: change date to force re-download)
RUN mkdir -p /cache && boltz predict --help || true

# Install the platform package
COPY pyproject.toml ./
COPY src/ ./src/
RUN pip install --no-cache-dir .

RUN useradd -m -u 1000 worker
USER worker

CMD ["python", "-m", "boltz2_service.worker.app"]
