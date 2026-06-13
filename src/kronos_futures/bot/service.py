from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
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
from .store import PostgresStore


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in ("symbol", "wallet_balance"):
            if hasattr(record, key):
                payload[key] = getattr(record, key)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, separators=(",", ":"))


def configure_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), handlers=[handler], force=True)


class BotService:
    def __init__(self, config_path: Path):
        self.settings = load_settings(config_path)
        self.store = PostgresStore(self.settings.database_url)
        self.inference = HttpInferenceClient(self.settings.inference_url)
        self.market = BinanceGateway(
            self.settings.binance_api_key,
            self.settings.binance_api_secret,
            self.settings.mode,
        )
        self.exchange = (
            PaperGateway(self.market)
            if self.settings.mode is TradingMode.PAPER
            else self.market
        )
        self.engines: list[TradingEngine] = []
        self.ready = False
        self.live = True

    async def start(self) -> None:
        await self.store.connect()
        migrations = Path(os.getenv("MIGRATIONS_DIR", "/app/migrations"))
        if not migrations.exists():
            migrations = Path(__file__).resolve().parents[3] / "migrations"
        await self.store.migrate(migrations)
        for binding in self.settings.bindings:
            if not binding.enabled:
                continue
            strategy = load_strategy(binding.strategy, binding.parameters)
            self.engines.append(
                TradingEngine(
                    binding,
                    strategy,
                    GuardedRiskEngine(binding.risk),
                    self.exchange,
                    self.inference,
                    self.store,
                )
            )
        for engine in self.engines:
            await engine.preflight()
        self.ready = True
        tasks = []
        for engine in self.engines:
            tasks.append(asyncio.create_task(engine.run_after_preflight()))
            if self.settings.mode is not TradingMode.PAPER:
                tasks.append(asyncio.create_task(engine.process_user_events()))
        await asyncio.gather(*tasks)

    async def stop(self) -> None:
        self.live = False
        self.ready = False
        for engine in self.engines:
            engine.running = False
        await self.inference.close()
        await self.market.close()
        await self.store.close()


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
        return {"ready": True, "bindings": [e.binding.name for e in service.engines]}

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
