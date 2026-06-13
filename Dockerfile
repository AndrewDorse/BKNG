FROM python:3.12.8-slim-bookworm AS base

ARG BKNG_IMAGE_TAG=2026-06-13-kronos-btc
LABEL org.opencontainers.image.title="BKNG Kronos BTC futures bot" \
      org.opencontainers.image.description="Kronos BTCUSDT Binance USD-M futures trader" \
      org.opencontainers.image.version="${BKNG_IMAGE_TAG}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl git gosu \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --shell /usr/sbin/nologin appuser

COPY requirements.txt /app/requirements.txt
RUN pip install --upgrade pip \
    && pip install --index-url https://download.pytorch.org/whl/cpu torch==2.6.0 \
    && pip install -r /app/requirements.txt

COPY pyproject.toml README.md /app/
COPY src /app/src
RUN pip install --no-deps /app

COPY docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod +x /docker-entrypoint.sh \
    && mkdir -p /app/data /app/logs /models \
    && chown -R appuser:appuser /app /models

USER root
ENTRYPOINT ["/docker-entrypoint.sh"]

FROM base AS trader

COPY config /app/config
COPY migrations /app/migrations
RUN chown -R appuser:appuser /app/config /app/migrations

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=5 \
    CMD curl -fsS http://localhost:8080/health/live || exit 1

CMD ["kronos-bot", "run", "--config", "/app/config/bot.yaml"]

FROM base AS inference

ARG KRONOS_COMMIT=67b630e67f6a18c9e9be918d9b4337c960db1e9a
RUN git clone https://github.com/AndrewDorse/Kronos.git /app/vendor/Kronos \
    && git -C /app/vendor/Kronos checkout --detach "${KRONOS_COMMIT}" \
    && printf '%s\n' "${KRONOS_COMMIT}" > /app/vendor/Kronos/.kronos_commit \
    && rm -rf /app/vendor/Kronos/.git \
    && chown -R appuser:appuser /app/vendor

ENV HF_HOME=/models/huggingface \
    KRONOS_VENDOR=/app/vendor/Kronos

VOLUME ["/models"]
EXPOSE 8081
HEALTHCHECK --interval=30s --timeout=5s --start-period=600s --retries=5 \
    CMD curl -fsS http://localhost:8081/health/live || exit 1

CMD ["uvicorn", "kronos_futures.bot.inference:app", "--host", "0.0.0.0", "--port", "8081"]
