import asyncio
from decimal import Decimal

from kronos_futures.bot.binance import BinanceGateway


def test_symbol_is_isolated_configures_uninitialized_symbol():
    gateway = object.__new__(BinanceGateway)
    requests = []

    async def request(method, path, params=None, *, signed=False, retries=3):
        requests.append((method, path, params, signed))
        return {"code": 200, "msg": "success"}

    gateway._request = request

    assert asyncio.run(gateway.symbol_is_isolated("BTCUSDT")) is True
    assert requests == [
        (
            "POST",
            "/fapi/v1/marginType",
            {"symbol": "BTCUSDT", "marginType": "ISOLATED"},
            True,
        )
    ]


def test_symbol_is_isolated_accepts_no_change_needed():
    gateway = object.__new__(BinanceGateway)

    async def request(method, path, params=None, *, signed=False, retries=3):
        from kronos_futures.bot.binance import BinanceError

        raise BinanceError(400, -4046, "No need to change margin type.")

    gateway._request = request

    assert asyncio.run(gateway.symbol_is_isolated("BTCUSDT")) is True


def test_positions_accepts_v3_payload_without_optional_metadata():
    gateway = object.__new__(BinanceGateway)

    async def request(method, path, params=None, *, signed=False, retries=3):
        return [
            {
                "symbol": "BTCUSDT",
                "positionAmt": "0.001",
                "entryPrice": "60000",
            }
        ]

    gateway._request = request

    positions = asyncio.run(gateway.positions())

    assert len(positions) == 1
    assert positions[0].quantity == Decimal("0.001")
    assert positions[0].isolated is True
    assert positions[0].leverage == 0
