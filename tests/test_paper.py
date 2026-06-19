import asyncio
from decimal import Decimal

from kronos_futures.bot.domain import OrderRequest, TradingMode
from kronos_futures.bot.paper import PaperGateway


class Market:
    mode = TradingMode.PAPER

    def __init__(self):
        self.bid = Decimal("99.9")
        self.ask = Decimal("100.1")

    async def book_ticker(self, symbol):
        return self.bid, self.ask


def test_paper_gateway_realizes_pnl_and_fees():
    market = Market()
    paper = PaperGateway(market, Decimal("100"))
    asyncio.run(paper.set_leverage("BTCUSDT", 10))
    asyncio.run(
        paper.submit_order(
            OrderRequest("BTCUSDT", "BUY", "MARKET", Decimal("1"), "entry")
        )
    )
    market.bid = Decimal("110")
    market.ask = Decimal("110.2")
    asyncio.run(
        paper.submit_order(
            OrderRequest(
                "BTCUSDT", "SELL", "MARKET", Decimal("1"), "exit", reduce_only=True
            )
        )
    )
    result = asyncio.run(paper.account())

    assert result.equity == Decimal("109.79495")
    assert result.daily_realized_pnl == Decimal("9.845")


def test_paper_gateway_recovers_position_and_stop(tmp_path):
    market = Market()
    state = tmp_path / "paper.json"
    paper = PaperGateway(market, Decimal("100"), str(state))
    asyncio.run(paper.set_leverage("BTCUSDT", 10))
    asyncio.run(
        paper.submit_order(
            OrderRequest("BTCUSDT", "BUY", "MARKET", Decimal("1"), "entry")
        )
    )
    asyncio.run(
        paper.submit_order(
            OrderRequest(
                "BTCUSDT", "SELL", "STOP_MARKET", Decimal("1"), "stop",
                stop_price=Decimal("94"), close_position=True
            )
        )
    )

    recovered = PaperGateway(market, Decimal("999"), str(state))

    assert asyncio.run(recovered.positions())[0].leverage == 10
    assert asyncio.run(recovered.open_orders("BTCUSDT"))[0].client_order_id == "stop"
