from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
from decimal import Decimal
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.responses import Response

from .binance import BinanceGateway
from .domain import TradingMode
from .engine import TradingEngine
from .inference import HttpInferenceClient
from .paper import PaperGateway
from .risk import GuardedRiskEngine
from .settings import load_settings, load_strategy


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in (
            "symbol",
            "mode",
            "bindings",
            "side",
            "quantity",
            "price",
            "reason",
            "leverage",
            "margin_fraction",
        ):
            if hasattr(record, key):
                payload[key] = getattr(record, key)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, separators=(",", ":"))


def configure_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), handlers=[handler], force=True)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


class BotService:
    def __init__(self, config_path: Path):
        self.settings = load_settings(config_path)
        self.inference = HttpInferenceClient(
            self.settings.inference_url,
            timeout_seconds=self.settings.inference_timeout_seconds,
        )
        self.market = BinanceGateway(
            self.settings.binance_api_key,
            self.settings.binance_api_secret,
            self.settings.mode,
        )
        self.exchange = (
            PaperGateway(
                self.market,
                Decimal(os.getenv("PAPER_STARTING_EQUITY", "20")),
            )
            if self.settings.mode is TradingMode.PAPER
            else self.market
        )
        self.engines: list[TradingEngine] = []
        self.live = True

    async def start(self) -> None:
        logging.getLogger(__name__).info(
            "trader_start",
            extra={
                "mode": self.settings.mode.value,
                "bindings": sum(binding.enabled for binding in self.settings.bindings),
            },
        )
        for binding in self.settings.bindings:
            if not binding.enabled:
                continue
            strategy = load_strategy(binding.strategy, binding.parameters)
            engine = TradingEngine(
                binding,
                strategy,
                GuardedRiskEngine(binding.risk),
                self.exchange,
                self.inference,
                poll_seconds=self.settings.poll_seconds,
            )
            self.engines.append(engine)
        await asyncio.gather(*(engine.run() for engine in self.engines))

    async def stop(self) -> None:
        logging.getLogger(__name__).info("trader_stop")
        self.live = False
        for engine in self.engines:
            engine.running = False
        await self.inference.close()
        await self.market.close()

    @property
    def ready(self) -> bool:
        return bool(self.engines) and all(engine.ready for engine in self.engines)


def health_app(service: BotService) -> FastAPI:
    app = FastAPI(title="Kronos trader health", docs_url=None, redoc_url=None)

    @app.get("/health/live")
    async def live():
        if not service.live:
            raise HTTPException(503, "shutting down")
        return {"live": True}

    @app.get("/health/ready")
    async def ready():
        if not service.ready:
            raise HTTPException(503, "startup gates have not passed")
        return {
            "ready": True,
            "bindings": [
                {
                    "name": engine.binding.name,
                    "symbol": engine.binding.symbol,
                    "last_analyzed_candle": (
                        engine.last_analyzed_candle.isoformat()
                        if engine.last_analyzed_candle
                        else None
                    ),
                    "last_successful_analysis": (
                        engine.last_successful_analysis.isoformat()
                        if engine.last_successful_analysis
                        else None
                    ),
                    "last_analysis_error": engine.last_analysis_error,
                    "position_open": bool(
                        engine.last_position and engine.last_position.is_open
                    ),
                    "halted_reason": engine.halted_reason,
                }
                for engine in service.engines
            ],
        }

    @app.get("/metrics")
    async def metrics():
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    return app


async def run_service(config_path: Path) -> None:
    configure_logging()
    service = BotService(config_path)
    server = uvicorn.Server(
        uvicorn.Config(
            health_app(service),
            host=service.settings.health_host,
            port=service.settings.health_port,
            log_config=None,
        )
    )
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass
    bot_task = asyncio.create_task(service.start())
    health_task = asyncio.create_task(server.serve())
    stop_task = asyncio.create_task(stop_event.wait())
    done, _ = await asyncio.wait(
        {bot_task, health_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
    )
    if bot_task in done and bot_task.exception():
        raise bot_task.exception()
    await service.stop()
    server.should_exit = True
    for task in (bot_task, health_task, stop_task):
        task.cancel()
