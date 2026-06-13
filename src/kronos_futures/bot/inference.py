from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime, timezone
from decimal import Decimal

import httpx
import pandas as pd
import psutil
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from .domain import ForecastContext, MarketContext
from .kronos import ForecastConfig, KronosPathForecaster


class CandlePayload(BaseModel):
    open_time: datetime
    open: str
    high: str
    low: str
    close: str
    volume: str
    amount: str


class ForecastRequest(BaseModel):
    candles: list[CandlePayload]
    interval_seconds: int = 60


class InferenceRuntime:
    def __init__(self):
        import torch

        torch.set_num_threads(int(os.getenv("TORCH_NUM_THREADS", "1")))
        self.config = ForecastConfig(
            context=512,
            horizon=1,
            samples=16,
            temperature=1.0,
            top_p=0.9,
            top_k=0,
            seed=123,
        )
        self.forecaster = KronosPathForecaster(self.config)
        self.ready = False
        self.p95_ms = 0

    def forecast(self, request: ForecastRequest) -> dict:
        if len(request.candles) != self.config.context:
            raise ValueError(f"Exactly {self.config.context} candles are required")
        frame = pd.DataFrame(
            [
                {
                    "open": float(item.open),
                    "high": float(item.high),
                    "low": float(item.low),
                    "close": float(item.close),
                    "volume": float(item.volume),
                    "amount": float(item.amount),
                }
                for item in request.candles
            ],
            index=pd.DatetimeIndex([item.open_time for item in request.candles]),
        )
        future = pd.date_range(
            frame.index[-1] + pd.Timedelta(seconds=request.interval_seconds),
            periods=1,
            freq=pd.Timedelta(seconds=request.interval_seconds),
        )
        started = time.perf_counter()
        paths = self.forecaster.forecast_batch([frame], [future])
        latency_ms = round((time.perf_counter() - started) * 1000)
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "close_paths": [str(value) for value in paths[0, :, 0, 3]],
            "seed": self.config.seed,
            "latency_ms": latency_ms,
        }

    def benchmark(self) -> None:
        now = pd.Timestamp.now(tz="UTC").floor("min")
        candles = [
            CandlePayload(
                open_time=(now - pd.Timedelta(minutes=511 - index)).to_pydatetime(),
                open="100",
                high="101",
                low="99",
                close=str(Decimal("100") + Decimal(index) / Decimal(10000)),
                volume="1",
                amount="100",
            )
            for index in range(512)
        ]
        request = ForecastRequest(candles=candles)
        latencies = [self.forecast(request)["latency_ms"] for _ in range(10)]
        self.p95_ms = sorted(latencies)[-1]
        minimum_memory = int(os.getenv("MIN_AVAILABLE_MEMORY_MB", "512")) * 1024 * 1024
        self.ready = (
            self.p95_ms <= int(os.getenv("MAX_INFERENCE_P95_MS", "10000"))
            and psutil.virtual_memory().available >= minimum_memory
        )


class HttpInferenceClient:
    def __init__(self, base_url: str, timeout_seconds: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.client = httpx.AsyncClient(timeout=timeout_seconds)

    async def close(self) -> None:
        await self.client.aclose()

    async def forecast(self, market: MarketContext) -> ForecastContext:
        payload = {
            "interval_seconds": 60,
            "candles": [
                {
                    "open_time": candle.open_time.isoformat(),
                    "open": str(candle.open),
                    "high": str(candle.high),
                    "low": str(candle.low),
                    "close": str(candle.close),
                    "volume": str(candle.volume),
                    "amount": str(candle.amount),
                }
                for candle in market.candles[-512:]
            ],
        }
        response = await self.client.post(f"{self.base_url}/forecast", json=payload)
        response.raise_for_status()
        result = response.json()
        return ForecastContext(
            generated_at=datetime.fromisoformat(result["generated_at"]),
            close_paths=tuple(Decimal(value) for value in result["close_paths"]),
            seed=int(result["seed"]),
            latency_ms=int(result["latency_ms"]),
        )

    async def ready(self) -> bool:
        try:
            response = await self.client.get(f"{self.base_url}/health/ready")
            return response.status_code == 200
        except httpx.HTTPError:
            return False


runtime: InferenceRuntime | None = None
app = FastAPI(title="Kronos inference", docs_url=None, redoc_url=None)


@app.on_event("startup")
async def startup() -> None:
    global runtime
    runtime = await asyncio.to_thread(InferenceRuntime)
    await asyncio.to_thread(runtime.benchmark)


@app.post("/forecast")
async def forecast(request: ForecastRequest):
    if runtime is None or not runtime.ready:
        raise HTTPException(503, "Inference runtime is not ready")
    try:
        return await asyncio.to_thread(runtime.forecast, request)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/health/live")
async def live():
    return {"live": True}


@app.get("/health/ready")
async def ready():
    if runtime is None or not runtime.ready:
        raise HTTPException(503, "Model benchmark has not passed")
    return {"ready": True, "p95_ms": runtime.p95_ms}
