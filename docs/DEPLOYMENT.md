# Immediate Live VPS Deployment

## Binance Account

Before deployment:

- Create a trade-only API key with USD-M Futures enabled.
- Disable withdrawals and universal transfers.
- Restrict the key to the VPS public IP.
- Select one-way position mode and single-asset USDT margin.
- Close every existing futures position and cancel every regular/algo order.

The bot configures all 15 symbols for isolated margin and 20x leverage. It
refuses startup if any unmanaged position or order exists.

## Deploy

Requirements: Ubuntu 24.04, Docker Engine 27+, Docker Compose v2, 1 vCPU and
2 GB RAM minimum.

```bash
git clone <repository> /opt/bkng
cd /opt/bkng
cp .env.example .env
chmod 600 .env
nano .env
```

Required `.env` values:

```env
TRADING_MODE=live
BINANCE_API_KEY=YOUR_TRADE_ONLY_KEY
BINANCE_API_SECRET=YOUR_SECRET
LIVE_RISK_ACKNOWLEDGEMENT=I_ACCEPT_EXTREME_FUTURES_RISK
POLL_SECONDS=30
LOG_LEVEL=INFO
```

Start:

```bash
docker compose build --no-cache trader
docker compose up -d trader
docker compose ps
docker compose logs --tail=300 trader
docker compose exec trader curl -f http://127.0.0.1:8080/health/ready
```

Do not consider deployment operational until `/health/ready` returns HTTP 200
and logs contain `portfolio_ready` with no following error.

No host port is published. This avoids collisions with other Hostinger
projects using port 8080; health remains available inside the container.

## What Happens At Startup

1. Synchronize Binance server time.
2. Verify one-way and single-asset account modes.
3. Refuse all unknown positions and manual/open orders.
4. Validate every configured perpetual and leverage limit.
5. Set isolated margin and 20x leverage.
6. Validate 31 contiguous completed 4h candles for every pair.
7. Restore a missing stop for every persisted owned position.
8. Wait for the next fixed UTC 72-hour rebalance boundary. The phase is
   anchored to `00:00 UTC` every third day and does not shift after restarts.

The bot does not enter immediately at container startup unless startup occurs
within five minutes of a scheduled rebalance candle close.

## Risk Controls

- Eight positions maximum: four long and four short.
- 1.5% equity margin each; 12% baseline total isolated margin.
- Every order uses at least $2 margin. Quantity rounds upward and increases
  further when Binance requires a higher minimum quantity or notional.
- 20x isolated leverage with a 3% adverse stop per position.
- Every filled entry must receive a native mark-price stop before proceeding.
- Failed protection or incomplete basket execution flattens the basket and
  persists a halt.
- A 15% account drawdown flattens all owned positions and halts permanently.
- Partial exits retry against Binance's actual residual quantity.
- State and deterministic client IDs prevent duplicate rebalance orders.

## Monitoring

```bash
docker compose logs -f trader
docker compose exec trader curl -s http://127.0.0.1:8080/health/ready
docker compose exec trader curl -s http://127.0.0.1:8080/metrics
```

Readiness reports owned positions, latest candle, halt reason, closed trades,
worst drawdown, unprotected-position observations, and reconciliation faults.
The health API is container-internal and is not exposed publicly.

## Emergency Flatten

Stop the process first so it cannot submit another basket:

```bash
docker compose stop trader
```

For every open Binance symbol:

```bash
docker compose run --rm trader kronos-bot flatten --config /app/config/bot.yaml --symbol BTCUSDT
```

Repeat for all open symbols, then confirm positions and conditional orders are
zero in Binance. Do not manually edit `/state/portfolio_state.json` while an
exchange position exists.

## Updates And State

The `trader_state` volume is required for restart ownership. Never run
`docker compose down -v` while positions may exist. Back up the volume before
upgrades, and require readiness to pass after every restart.
