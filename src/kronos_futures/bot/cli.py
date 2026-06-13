from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

from .binance import BinanceGateway
from .domain import OrderRequest, Side, TradingMode
from .engine import client_order_id
from .service import run_service
from .settings import load_settings


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Kronos Binance futures bot")
    result.add_argument("command", choices=["run", "status", "flatten", "reconcile"])
    result.add_argument("--config", type=Path, default=Path("config/bot.yaml"))
    result.add_argument("--symbol", default="BTCUSDT")
    return result


async def _control(command: str, config: Path, symbol: str) -> None:
    settings = load_settings(config)
    if settings.mode is TradingMode.PAPER:
        raise RuntimeError("Operator exchange commands require testnet or live mode")
    gateway = BinanceGateway(
        settings.binance_api_key, settings.binance_api_secret, settings.mode
    )
    try:
        positions = [
            position for position in await gateway.positions()
            if position.symbol == symbol
        ]
        orders = await gateway.open_orders(symbol)
        payload = {
            "mode": settings.mode.value,
            "positions": [
                {
                    "symbol": item.symbol,
                    "quantity": str(item.quantity),
                    "entry_price": str(item.entry_price),
                    "isolated": item.isolated,
                    "leverage": item.leverage,
                }
                for item in positions
            ],
            "open_orders": [
                {
                    "client_order_id": order.client_order_id,
                    "status": order.status,
                    "type": order.order_type,
                }
                for order in orders
            ],
        }
        print(json.dumps(payload, indent=2))
        if command == "flatten" and positions:
            await gateway.cancel_all_orders(symbol)
            position = positions[0]
            side = Side.LONG if position.quantity > 0 else Side.SHORT
            await gateway.submit_order(
                OrderRequest(
                    symbol=symbol,
                    side=side.exit_order_side,
                    order_type="MARKET",
                    quantity=abs(position.quantity),
                    client_order_id=client_order_id(
                        "operator", datetime.now(timezone.utc), "exit"
                    ),
                    reduce_only=True,
                )
            )
            print("flatten: submitted")
    finally:
        await gateway.close()


def main(argv: list[str] | None = None) -> None:
    args = parser().parse_args(argv)
    if args.command == "run":
        asyncio.run(run_service(args.config))
    else:
        asyncio.run(_control(args.command, args.config, args.symbol.upper()))


if __name__ == "__main__":
    main()
