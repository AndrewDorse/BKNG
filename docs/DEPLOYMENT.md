# VPS Deployment

## Risk boundary

The historical `$20 -> $58.455672` result is a two-day discovery backtest using
45x leverage and all account equity as isolated margin. It had an 81.28%
drawdown and is not an expected live return. The default live profile commits
32% of available equity as margin at 50x and places a 0.7% mark-price emergency
stop. Its same-period replay reached `$32.815419` with `-24.12%` drawdown, but
that profile was tuned on the reported period and remains unvalidated.

Start in `paper`, then use Binance Futures testnet. Live mode is available only
after all startup checks pass and the acknowledgement string matches exactly.

## VPS

Ubuntu 24.04, Docker Engine 27+, and Docker Compose v2 are expected. One vCPU
and 4 GB RAM is the minimum target. Four vCPUs and 8 GB RAM are recommended so
model inference and trading reconciliation do not compete for memory.

```bash
git clone <your-repository> /opt/kronos-bot
cd /opt/kronos-bot
cp .env.example .env
chmod 600 .env
# Choose paper/testnet/live and set Binance credentials when required.
docker compose build --no-cache
docker compose up -d --force-recreate
docker compose ps
docker compose logs -f inference trader
curl http://127.0.0.1:8080/health/ready
```

The inference container downloads the pinned model and tokenizer on first
startup. Its readiness check runs ten forecasts and rejects trading when the
worst observed latency exceeds 10 seconds or less than 512 MB is available.
The initial model download and benchmark can take several minutes on one CPU.

## Deployment Diagnostics

Use these commands before changing configuration:

```bash
docker compose ps -a
docker compose logs --tail=300 inference
docker compose logs --tail=300 trader
docker compose run --rm trader kronos-bot --help
docker compose exec inference curl -i http://localhost:8081/health/ready
```

Common failures:

- `ModuleNotFoundError: tqdm`: stale image; rebuild with `--no-cache`.
- Missing `.env`: no longer fatal; Compose defaults to paper mode.
- Inference `503`: response text identifies latency or available-memory failure.
- Trader repeatedly restarts: inspect the named preflight gate in trader logs.
- Binance HTTP `451`/`403`: the VPS network location cannot access Binance
  Futures; deployment must use a permitted region and IP.

For a clean rebuild:

```bash
docker compose down
docker compose build --no-cache
docker compose up -d --force-recreate
```

## Credentials

Create a Binance API key with USD-M Futures trading permission only:

- Disable withdrawals.
- Restrict the key to the VPS public IP.
- Do not enable universal transfer permissions.
- Store secrets in `.env` with mode `0600`, or replace the environment values
  with Docker secrets in your deployment platform.

Paper mode uses public market data and does not submit orders. Testnet and live
mode require `BINANCE_API_KEY` and `BINANCE_API_SECRET`.

## Live gates

The account must already use:

- one-way position mode;
- single-asset USDT margin mode;
- isolated margin for an existing owned BTC position;
- no unknown BTC position or open order.

The bot does not silently change account-wide position mode. For live mode:

```env
TRADING_MODE=live
LIVE_RISK_ACKNOWLEDGEMENT=I_ACCEPT_EXTREME_FUTURES_RISK
```

Then restart and inspect readiness and logs:

```bash
docker compose up -d
docker compose logs -f trader
curl -f http://127.0.0.1:8080/health/ready
```

## Operations

Run commands inside the trader container:

```bash
docker compose exec trader kronos-bot status
docker compose exec trader kronos-bot reconcile --symbol BTCUSDT
docker compose exec trader kronos-bot flatten --symbol BTCUSDT
```

Stop new entries with `docker compose stop trader`. Use `flatten` first when an
open position must be closed. The health endpoint is bound to localhost by
default; expose it through an authenticated monitoring tunnel rather than
directly to the internet.

## Strategy and pair changes

Each enabled YAML binding owns one symbol. The process refuses to start if two
strategies own the same symbol. Add a strategy by implementing the `Strategy`
protocol and referencing `module:ClassName` in `config/bot.yaml`.

Changing symbols automatically loads Binance price, lot-size, notional, and
leverage filters. New pairs are blocked unless the binding is marked
`validated: true` or `ALLOW_UNVALIDATED_PAIRS=true` is deliberately set.
The unchanged BTC 45x strategy liquidated ETH in transfer testing, so parameters
must be independently validated per pair.

## Backups and updates

The model cache is disposable. Open positions and protective orders are read
directly from Binance after restart. Stop cleanly with:

```bash
docker compose down
```

Do not use `docker compose down -v` unless intentionally deleting the model
cache volume.
