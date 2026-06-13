from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import asyncpg

from .domain import AccountContext, OrderRequest, OrderResult, SignalIntent


class PostgresStore:
    def __init__(self, database_url: str):
        self.database_url = database_url
        self.pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        self.pool = await asyncpg.create_pool(self.database_url, min_size=1, max_size=4)

    async def close(self) -> None:
        if self.pool:
            await self.pool.close()

    async def migrate(self, migrations_dir: Path) -> None:
        assert self.pool
        for migration in sorted(migrations_dir.glob("*.sql")):
            await self.pool.execute(migration.read_text(encoding="utf-8"))

    async def record_signal(self, binding: str, intent: SignalIntent) -> bool:
        assert self.pool
        result = await self.pool.execute(
            """
            INSERT INTO signals
                (binding, symbol, candle_close_time, side, reason, confidence, metadata)
            VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb)
            ON CONFLICT (binding, candle_close_time) DO NOTHING
            """,
            binding,
            intent.symbol,
            intent.candle_close_time,
            intent.side.value if intent.side else None,
            intent.reason,
            intent.confidence,
            json.dumps(dict(intent.metadata)),
        )
        return result.endswith("1")

    async def persist_order_intent(
        self, binding: str, request: OrderRequest, purpose: str
    ) -> None:
        assert self.pool
        await self.pool.execute(
            """
            INSERT INTO order_intents
                (binding, symbol, client_order_id, purpose, side, order_type, quantity,
                 reduce_only, stop_price, status)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,'PENDING')
            ON CONFLICT (client_order_id) DO NOTHING
            """,
            binding,
            request.symbol,
            request.client_order_id,
            purpose,
            request.side,
            request.order_type,
            request.quantity,
            request.reduce_only,
            request.stop_price,
        )

    async def record_order(self, result: OrderResult) -> None:
        assert self.pool
        await self.pool.execute(
            """
            UPDATE order_intents
            SET exchange_order_id=$2, status=$3, executed_quantity=$4,
                average_price=$5, updated_at=now()
            WHERE client_order_id=$1
            """,
            result.client_order_id,
            result.order_id,
            result.status,
            result.executed_quantity,
            result.average_price,
        )

    async def owns_symbol(self, symbol: str) -> bool:
        assert self.pool
        value = await self.pool.fetchval(
            """
            SELECT purpose='entry'
            FROM order_intents
            WHERE symbol=$1 AND status IN ('PARTIALLY_FILLED','FILLED')
            ORDER BY updated_at DESC, id DESC
            LIMIT 1
            """,
            symbol,
        )
        return bool(value)

    async def update_order_event(self, order: dict) -> None:
        assert self.pool
        await self.pool.execute(
            """
            UPDATE order_intents
            SET exchange_order_id=$2, status=$3, executed_quantity=$4,
                average_price=$5, updated_at=now()
            WHERE client_order_id=$1
            """,
            order["c"],
            int(order["i"]),
            order["X"],
            Decimal(order["z"]),
            Decimal(order.get("ap") or "0"),
        )

    async def set_runtime_state(self, key: str, value: dict) -> None:
        assert self.pool
        await self.pool.execute(
            """
            INSERT INTO runtime_state(key, value, updated_at)
            VALUES ($1, $2::jsonb, $3)
            ON CONFLICT (key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
            """,
            key,
            json.dumps(value),
            datetime.now(timezone.utc),
        )

    async def runtime_state(self, key: str) -> dict | None:
        assert self.pool
        value = await self.pool.fetchval("SELECT value FROM runtime_state WHERE key=$1", key)
        return dict(value) if value else None

    async def update_realized_pnl(self, pnl: Decimal) -> None:
        assert self.pool
        await self.pool.execute(
            """
            INSERT INTO risk_state(id, daily_realized_pnl, peak_equity, consecutive_losses)
            VALUES (1, $1, 0, CASE WHEN $1 < 0 THEN 1 ELSE 0 END)
            ON CONFLICT (id) DO UPDATE
            SET daily_realized_pnl = CASE
                    WHEN risk_state.realized_day = CURRENT_DATE
                    THEN risk_state.daily_realized_pnl + $1 ELSE $1 END,
                realized_day = CURRENT_DATE,
                consecutive_losses = CASE WHEN $1 < 0
                    THEN risk_state.consecutive_losses + 1 ELSE 0 END,
                updated_at = now()
            """,
            pnl,
        )

    async def risk_adjusted_account(self, account: AccountContext) -> AccountContext:
        assert self.pool
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                row = await connection.fetchrow(
                    "SELECT * FROM risk_state WHERE id=1 FOR UPDATE"
                )
                daily_pnl = row["daily_realized_pnl"]
                if row["realized_day"] != datetime.now(timezone.utc).date():
                    daily_pnl = Decimal(0)
                    await connection.execute(
                        """
                        UPDATE risk_state SET daily_realized_pnl=0,
                            realized_day=CURRENT_DATE, updated_at=now() WHERE id=1
                        """
                    )
                peak = max(Decimal(row["peak_equity"]), account.equity)
                if peak != row["peak_equity"]:
                    await connection.execute(
                        "UPDATE risk_state SET peak_equity=$1, updated_at=now() WHERE id=1",
                        peak,
                    )
        return replace(
            account,
            peak_equity=peak,
            daily_realized_pnl=Decimal(daily_pnl),
            consecutive_losses=int(row["consecutive_losses"]),
            halted_until=row["halted_until"],
        )

    async def record_trade_update(
        self,
        order: dict,
        event_time_ms: int,
        loss_limit: int,
        pause_hours: int,
    ) -> bool:
        assert self.pool
        trade_id = int(order.get("t", 0))
        if trade_id <= 0 or order.get("x") != "TRADE":
            return False
        realized = Decimal(order.get("rp", "0"))
        commission = Decimal(order.get("n") or "0")
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                inserted = await connection.fetchval(
                    """
                    INSERT INTO fills
                        (exchange_trade_id, client_order_id, symbol, side, quantity,
                         price, commission, realized_pnl, event_time)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,to_timestamp($9 / 1000.0))
                    ON CONFLICT (exchange_trade_id) DO NOTHING
                    RETURNING exchange_trade_id
                    """,
                    trade_id,
                    order["c"],
                    order["s"],
                    order["S"],
                    Decimal(order["l"]),
                    Decimal(order["L"]),
                    commission,
                    realized,
                    event_time_ms,
                )
                if inserted is None:
                    return False
                net = realized - commission
                closes_position = realized != 0
                await connection.execute(
                    """
                    UPDATE risk_state
                    SET daily_realized_pnl = CASE WHEN realized_day=CURRENT_DATE
                            THEN daily_realized_pnl + $1 ELSE $1 END,
                        realized_day=CURRENT_DATE,
                        consecutive_losses = CASE
                            WHEN $4 AND $1 < 0 THEN consecutive_losses + 1
                            WHEN $4 AND $1 > 0 THEN 0 ELSE consecutive_losses END,
                        halted_until = CASE
                            WHEN $4 AND $1 < 0 AND consecutive_losses + 1 >= $2
                            THEN now() + make_interval(hours => $3)
                            ELSE halted_until END,
                        updated_at=now()
                    WHERE id=1
                    """,
                    net,
                    loss_limit,
                    pause_hours,
                    closes_position,
                )
        return True

    async def record_account_event(self, event: dict) -> None:
        assert self.pool
        reason = event.get("a", {}).get("m", "UNKNOWN")
        for balance in event.get("a", {}).get("B", []):
            change = Decimal(balance.get("bc", "0"))
            if change == 0:
                continue
            key = f"{event.get('E')}:{reason}:{balance.get('a')}:{change}"
            await self.pool.execute(
                """
                INSERT INTO account_events
                    (event_key, event_type, asset, amount, event_time, payload)
                VALUES ($1,$2,$3,$4,to_timestamp($5 / 1000.0),$6::jsonb)
                ON CONFLICT (event_key) DO NOTHING
                """,
                key,
                reason,
                balance.get("a"),
                change,
                int(event["E"]),
                json.dumps(event),
            )
