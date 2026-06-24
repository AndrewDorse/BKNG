import asyncio
import time
from decimal import Decimal

import httpx

from kronos_futures.bot.binance import BinanceGateway
from kronos_futures.bot.domain import OrderRequest


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


def test_conditional_order_uses_algo_api():
    gateway = object.__new__(BinanceGateway)
    requests = []

    async def request(method, path, params=None, *, signed=False, retries=3):
        requests.append((method, path, params, signed))
        return {
            "symbol": "BTCUSDT",
            "clientAlgoId": params["clientAlgoId"],
            "algoId": 123,
            "algoStatus": "NEW",
            "orderType": params["type"],
        }

    gateway._request = request
    order = OrderRequest(
        symbol="BTCUSDT",
        side="BUY",
        order_type="STOP_MARKET",
        quantity=Decimal("0.001"),
        client_order_id="kr_stop_test",
        stop_price=Decimal("65000"),
        working_type="MARK_PRICE",
        close_position=True,
    )

    result = asyncio.run(gateway.submit_order(order))

    assert requests[0][1] == "/fapi/v1/algoOrder"
    assert requests[0][2]["algoType"] == "CONDITIONAL"
    assert requests[0][2]["triggerPrice"] == Decimal("65000")
    assert requests[0][2]["clientAlgoId"] == "kr_stop_test"
    assert result.status == "NEW"
    assert result.order_type == "STOP_MARKET"


def test_open_orders_merges_regular_and_algo_orders():
    gateway = object.__new__(BinanceGateway)

    async def request(method, path, params=None, *, signed=False, retries=3):
        if path == "/fapi/v1/openOrders":
            return []
        return [
            {
                "symbol": "BTCUSDT",
                "clientAlgoId": "kr_take_test",
                "algoId": 456,
                "algoStatus": "NEW",
                "orderType": "TAKE_PROFIT_MARKET",
            }
        ]

    gateway._request = request

    orders = asyncio.run(gateway.open_orders("BTCUSDT"))

    assert len(orders) == 1
    assert orders[0].client_order_id == "kr_take_test"
    assert orders[0].order_type == "TAKE_PROFIT_MARKET"


def test_cancel_all_orders_cancels_regular_and_algo_orders():
    gateway = object.__new__(BinanceGateway)
    paths = []

    async def request(method, path, params=None, *, signed=False, retries=3):
        paths.append(path)
        return {"code": 200}

    gateway._request = request

    asyncio.run(gateway.cancel_all_orders("BTCUSDT"))

    assert set(paths) == {"/fapi/v1/allOpenOrders", "/fapi/v1/algoOpenOrders"}


def test_get_request_retries_transient_read_timeout():
    gateway = object.__new__(BinanceGateway)
    gateway.api_key = ""
    gateway.api_secret = b""
    gateway.rest_base = "https://example.test"
    gateway.time_offset_ms = 0

    class Client:
        def __init__(self):
            self.calls = 0

        async def request(self, method, url, params=None, headers=None):
            self.calls += 1
            if self.calls == 1:
                raise httpx.ReadTimeout("temporary")
            return httpx.Response(
                200,
                json={"ok": True},
                request=httpx.Request(method, url),
            )

    gateway.client = Client()

    result = asyncio.run(gateway._request("GET", "/fapi/v1/klines", retries=2))

    assert result == {"ok": True}
    assert gateway.client.calls == 2


def test_post_request_does_not_retry_transport_failure():
    gateway = object.__new__(BinanceGateway)
    gateway.api_key = ""
    gateway.api_secret = b""
    gateway.rest_base = "https://example.test"
    gateway.time_offset_ms = 0

    class Client:
        def __init__(self):
            self.calls = 0

        async def request(self, method, url, params=None, headers=None):
            self.calls += 1
            raise httpx.ReadTimeout("unknown submission state")

    gateway.client = Client()

    try:
        asyncio.run(gateway._request("POST", "/fapi/v1/order", retries=3))
    except httpx.ReadTimeout:
        pass
    else:
        raise AssertionError("POST timeout must propagate for client-ID reconciliation")

    assert gateway.client.calls == 1


def test_signed_post_resynchronizes_and_retries_timestamp_rejection():
    gateway = object.__new__(BinanceGateway)
    gateway.api_key = "key"
    gateway.api_secret = b"secret"
    gateway.rest_base = "https://example.test"
    gateway.time_offset_ms = 0
    gateway._time_sync_lock = asyncio.Lock()

    class Client:
        def __init__(self):
            self.calls = 0

        async def request(self, method, url, params=None, headers=None):
            self.calls += 1
            if self.calls == 1:
                return httpx.Response(
                    400,
                    json={"code": -1021, "msg": "Timestamp outside recvWindow"},
                    request=httpx.Request(method, url),
                )
            return httpx.Response(
                200,
                json={"ok": True},
                request=httpx.Request(method, url),
            )

    async def synchronize_time():
        gateway.time_offset_ms = int(time.time() * 1000)

    gateway.client = Client()
    gateway.synchronize_time = synchronize_time

    result = asyncio.run(
        gateway._request("POST", "/fapi/v1/order", signed=True, retries=1)
    )

    assert result == {"ok": True}
    assert gateway.client.calls == 2


def test_symbol_rules_accepts_tradifi_perpetual_contract():
    gateway = object.__new__(BinanceGateway)
    gateway.api_key = ""
    gateway._rules = {}
    gateway._exchange_info = {
        "TSLAUSDT": {
            "symbol": "TSLAUSDT",
            "status": "TRADING",
            "contractType": "TRADIFI_PERPETUAL",
            "filters": [
                {"filterType": "LOT_SIZE", "minQty": "0.01", "maxQty": "20000", "stepSize": "0.01"},
                {"filterType": "PRICE_FILTER", "tickSize": "0.01000"},
                {"filterType": "MIN_NOTIONAL", "notional": "5"},
            ],
        }
    }

    rules = asyncio.run(gateway.symbol_rules("TSLAUSDT"))

    assert rules.symbol == "TSLAUSDT"
    assert rules.price_tick == Decimal("0.01000")
    assert rules.quantity_step == Decimal("0.01")
    assert rules.minimum_notional == Decimal("5")
