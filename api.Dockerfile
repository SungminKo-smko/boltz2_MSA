FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential libffi-dev libssl-dev \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY src/ ./src/

RUN pip install --no-cache-dir . && \
    pip install --no-cache-dir "mcp[cli]>=1.0"

RUN useradd -m -u 1000 appuser
USER appuser

EXPOSE 8001

CMD ["uvicorn", "boltz2_service.api.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8001"]
