FROM nvidia/cuda:12.4.1-cudnn-devel-ubuntu22.04

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

# Install PyTorch for CUDA 12.4 driver compatibility, then boltz2
RUN pip install --no-cache-dir torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu124
RUN pip install --no-cache-dir "boltz==2.2.0"

# cuEquivariance kernels — accelerates triangular attention/multiplication on A100
RUN pip install --no-cache-dir \
    "cuequivariance_torch>=0.5.0" \
    "cuequivariance_ops_torch_cu12>=0.5.0" \
    "cuequivariance_ops_cu12>=0.5.0" \
    || echo "WARN: cuequivariance install failed, will use --no_kernels fallback"

# Pre-download model weights (cache buster: change date to force re-download)
RUN mkdir -p /cache && boltz predict --help || true

# Install the platform package
COPY pyproject.toml ./
COPY src/ ./src/
RUN pip install --no-cache-dir .

RUN useradd -m -u 1000 worker && \
    chown -R worker:worker /cache
USER worker

CMD ["python", "-m", "boltz2_service.worker.app"]
