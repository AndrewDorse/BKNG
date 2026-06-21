# Binance Futures Portfolio Bot

Dockerized Python 3.12 trader for a 15-symbol Binance USD-M futures
cross-sectional momentum portfolio. The default deployment mode is `live`.

## Strategy

- Completed 4h candles and fixed Unix-UTC rebalance every 72 hours.
- When starting flat more than 48 hours before the next fixed boundary, bootstrap
  once at the next completed 4h candle, then return to the fixed schedule.
- Rank five-day return over 30 completed candles.
- Long the strongest four pairs and short the weakest four.
- 20x isolated leverage.
- Automatically use 10x for a symbol when Binance does not allow 20x; reject
  the symbol only when even 10x is unavailable.
- Skip unavailable, delisted, invalid-history, and temporarily failing symbols;
  replace failed entries with the next pair in the momentum ranking.
- 1.5% account equity margin per position, with a $2 minimum margin per order.
- Round quantity upward and raise it when Binance minimum quantity/notional requires it.
- Exchange-native reduce-only mark-price stop 3% adverse from entry.
- 15% account drawdown kill switch.
- Persistent ownership state in the `trader_state` Docker volume.

Historical fixed-phase results are research, not a profit guarantee. The
selected fixed phase returned +336.15%, but other rebalance phases were
materially weaker and the result is not reliable evidence of live performance.

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
