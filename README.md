# Kronos Binance Futures Bot

Dockerized Python 3.12 bot for Binance USD-M Futures using the pinned
AndrewDorse/Kronos model. The enabled binding trades `BTCUSDT` one-minute
candles only.

## Services

- `trader`: market streams, strategy/risk engine, Binance execution,
  Binance position/order reconciliation, health, and Prometheus metrics.
- `inference`: persistent CPU-only Kronos-small inference process.

The trader checks Binance every 30 seconds. For each newly completed BTC
one-minute candle it runs Kronos once, opens at most one position, and places
exchange-native mark-price stop-loss and take-profit orders. Binance is the
source of truth after restarts; no database is required.

## BTC Profile

- 512 completed one-minute candles.
- 16 deterministic one-step Kronos paths.
- 30-minute mean reversion at `|z| >= 1`.
- Forecast edge at least `0.0004220834304313748`.
- At least 13 of 16 paths agree on direction.
- 50x isolated exchange leverage using 32% of available equity as margin.
- Maximum account notional: 16x equity.
- 0.7% mark-price stop and 60-minute maximum hold.
- 25% account drawdown halt while the trader process is running.

## Deploy

```bash
cp .env.example .env
chmod 600 .env
# Edit trading mode and Binance credentials before startup.
docker compose build --no-cache
docker compose up -d --force-recreate
docker compose ps
docker compose logs -f inference trader
curl http://127.0.0.1:8080/health/ready
```

The first inference startup downloads the pinned model and runs ten CPU
forecasts. On a one-vCPU VPS this can take several minutes. The health check
allows ten minutes for initialization.

If startup fails, run:

```bash
docker compose ps -a
docker compose logs --tail=300 inference trader
docker compose run --rm trader kronos-bot --help
docker compose exec inference curl -fsS http://localhost:8081/health/ready
```

The inference service must be `healthy` before the trader starts. A `503`
response includes the failed latency or memory gate.

Deployment and live-account requirements are documented in
[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).

Research datasets, forecast caches, reports, charts, sweeps, simulators, and
research tests are stored locally under `research/`. That directory is ignored
by Git and excluded from the Docker build context.

The reported backtest results are in-sample research, not expected live
performance or a profit guarantee.
