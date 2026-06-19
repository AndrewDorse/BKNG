# Binance Futures Portfolio Bot

Dockerized Python 3.12 trader for a 15-symbol Binance USD-M futures
cross-sectional momentum portfolio. The default deployment mode is `live`.

## Strategy

- Completed 4h candles and fixed Unix-UTC rebalance every 72 hours.
- Rank four-day return over 24 candles.
- Long the strongest three pairs and short the weakest three.
- 10x isolated leverage.
- 1% account equity margin per position; 6% maximum basket margin.
- Exchange-native reduce-only mark-price stop 6% adverse from entry.
- 15% account drawdown kill switch.
- Persistent ownership state in the `trader_state` Docker volume.

Historical fixed-phase results are research, not a profit guarantee. The
available year returned +79.76% at 1% sizing with -11.77% drawdown, but other
rebalance phases were materially weaker.

## Immediate Live Deployment

```bash
cp .env.example .env
chmod 600 .env
nano .env                 # set BINANCE_API_KEY and BINANCE_API_SECRET
docker compose build --no-cache trader
docker compose up -d trader
docker compose logs -f trader
docker compose exec trader curl -f http://127.0.0.1:8080/health/ready
```

The container refuses startup unless credentials and the exact live risk
acknowledgement are present. Readiness additionally requires active symbols,
supported leverage, synchronized candles, one-way mode, single-asset margin,
isolated margin, no unmanaged positions/orders, and restored protective stops.

The health API is intentionally not published on a host port, avoiding
collisions with other VPS projects. Query it from inside the container.

Do not delete the `trader_state` volume while positions may exist. See
[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) for account setup and emergency
procedures. Nothing under the Git-ignored `research/` directory is required by
the deployed container.
