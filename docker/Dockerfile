FROM python:3.12.8-slim-bookworm AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends git curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --index-url https://download.pytorch.org/whl/cpu torch==2.6.0 \
    && pip install --no-deps . \
    && pip install asyncpg==0.30.0 einops==0.8.1 fastapi==0.115.6 \
       huggingface-hub==0.33.1 httpx==0.28.1 numpy==2.1.3 pandas==2.2.3 \
       prometheus-client==0.21.1 psutil==6.1.1 pydantic==2.10.4 PyYAML==6.0.2 \
       safetensors==0.6.2 uvicorn==0.34.0 websockets==14.1

FROM base AS trader
COPY config ./config
COPY migrations ./migrations
HEALTHCHECK --interval=30s --timeout=3s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8080/health/live || exit 1
CMD ["kronos-bot", "run", "--config", "/app/config/bot.yaml"]

FROM base AS inference
ARG KRONOS_COMMIT=67b630e67f6a18c9e9be918d9b4337c960db1e9a
RUN git clone https://github.com/AndrewDorse/Kronos.git /app/vendor/Kronos \
    && git -C /app/vendor/Kronos checkout ${KRONOS_COMMIT} \
    && echo "${KRONOS_COMMIT}" > /app/vendor/Kronos/.kronos_commit \
    && rm -rf /app/vendor/Kronos/.git
ENV HF_HOME=/models/huggingface
ENV KRONOS_VENDOR=/app/vendor/Kronos
VOLUME ["/models"]
EXPOSE 8081
HEALTHCHECK --interval=30s --timeout=3s --start-period=180s --retries=3 \
    CMD curl -fsS http://localhost:8081/health/live || exit 1
CMD ["uvicorn", "kronos_futures.bot.inference:app", "--host", "0.0.0.0", "--port", "8081"]
