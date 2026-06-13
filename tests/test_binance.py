import asyncio

from kronos_futures.bot.binance import BinanceGateway


def test_symbol_is_isolated_uses_account_configuration_when_position_is_flat():
    gateway = object.__new__(BinanceGateway)
    requests = []

    async def request(method, path, params=None, *, signed=False, retries=3):
        requests.append((method, path, params, signed))
        return {
            "positions": [
                {
                    "symbol": "BTCUSDT",
                    "positionAmt": "0",
                    "marginType": "isolated",
                }
            ]
        }

    gateway._request = request

    assert asyncio.run(gateway.symbol_is_isolated("BTCUSDT")) is True
    assert requests == [("GET", "/fapi/v3/account", None, True)]


def test_symbol_is_isolated_falls_back_to_position_risk():
    gateway = object.__new__(BinanceGateway)

    async def request(method, path, params=None, *, signed=False, retries=3):
        if path == "/fapi/v3/account":
            return {"positions": []}
        return [{"symbol": "BTCUSDT", "marginType": "isolated"}]

    gateway._request = request

    assert asyncio.run(gateway.symbol_is_isolated("BTCUSDT")) is True
