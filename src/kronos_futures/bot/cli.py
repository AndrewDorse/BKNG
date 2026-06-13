from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

from .binance import BinanceGateway
from .domain import OrderRequest, Side
from .engine import client_order_id
from .service import run_service
from .settings import load_settings
from .store import PostgresStore


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Kronos Binance futures bot")
    result.add_argument(
        "command",
        choices=["run", "status", "pause", "resume", "flatten", "reconcile", "kill"],
    )
    result.add_argument("--config", type=Path, default=Path("config/bot.yaml"))
    result.add_argument("--symbol", default="BTCUSDT")
    result.add_argument("--reason", default="operator_request")
    return result


async def _control(command: str, config: Path, symbol: str, reason: str) -> None:
    settings = load_settings(config)
    store = PostgresStore(settings.database_url)
    await store.connect()
    try:
        if command == "status":
            state = await store.runtime_state("control") or {"paused": False}
            print(json.dumps({"mode": settings.mode.value, "control": state}, indent=2))
            return
        if command in {"pause", "kill"}:
            await store.set_runtime_state(
                "control", {"paused": True, "reason": reason, "kill": command == "kill"}
            )
        elif command == "resume":
            await store.set_runtime_state("control", {"paused": False, "reason": reason})
        elif command in {"flatten", "reconcile"}:
            gateway = BinanceGateway(
                settings.binance_api_key, settings.binance_api_secret, settings.mode
            )
            try:
                positions = [position for position in await gateway.positions() if position.symbol == symbol]
                orders = await gateway.open_orders(symbol)
                print(
                    json.dumps(
                        {
                            "positions": [
                                {
                                    "symbol": p.symbol,
                                    "quantity": str(p.quantity),
                                    "entry_price": str(p.entry_price),
                                }
                                for p in positions
                            ],
                            "open_orders": [o.client_order_id for o in orders],
                        },
                        indent=2,
                    )
                )
                if command == "flatten" and positions:
                    await gateway.cancel_all_orders(symbol)
                    position = positions[0]
                    request = OrderRequest(
                        symbol=symbol,
                        side=(Side.LONG if position.quantity > 0 else Side.SHORT).exit_order_side,
                        order_type="MARKET",
                        quantity=abs(position.quantity),
                        client_order_id=client_order_id(
                            "operator", datetime.now(timezone.utc), "exit"
                        ),
                        reduce_only=True,
                    )
                    await store.persist_order_intent("operator", request, reason)
                    result = await gateway.submit_order(request)
                    await store.record_order(result)
            finally:
                await gateway.close()
        print(f"{command}: ok")
    finally:
        await store.close()


def main(argv: list[str] | None = None) -> None:
    args = parser().parse_args(argv)
    if args.command == "run":
        asyncio.run(run_service(args.config))
    else:
        asyncio.run(_control(args.command, args.config, args.symbol.upper(), args.reason))


if __name__ == "__main__":
    main()
