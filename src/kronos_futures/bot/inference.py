from __future__ import annotations

import asyncio
import logging
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

LOG = logging.getLogger(__name__)


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
            samples=max(2, int(os.getenv("KRONOS_SAMPLES", "4"))),
            temperature=1.0,
            top_p=0.9,
            top_k=0,
            seed=123,
        )
        self.forecaster = KronosPathForecaster(self.config)
        self.ready = False
        self.p95_ms = 0
        self.available_memory_mb = 0
        self.readiness_error: str | None = "model_not_loaded"

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
        runs = max(1, int(os.getenv("INFERENCE_BENCHMARK_RUNS", "3")))
        latencies = [self.forecast(request)["latency_ms"] for _ in range(runs)]
        self.p95_ms = sorted(latencies)[max(0, int(len(latencies) * 0.95) - 1)]
        self.available_memory_mb = psutil.virtual_memory().available // (1024 * 1024)
        maximum_latency = int(os.getenv("MAX_INFERENCE_P95_MS", "10000"))
        minimum_memory_mb = int(os.getenv("MIN_AVAILABLE_MEMORY_MB", "512"))
        failures = []
        if self.p95_ms > maximum_latency:
            failures.append(f"p95_ms={self.p95_ms} exceeds {maximum_latency}")
        if self.available_memory_mb < minimum_memory_mb:
            failures.append(
                f"available_memory_mb={self.available_memory_mb} below {minimum_memory_mb}"
            )
        self.readiness_error = "; ".join(failures) or None
        self.ready = not failures
        LOG.info(
            "inference_benchmark_complete samples=%s runs=%s p95_ms=%s "
            "available_memory_mb=%s ready=%s",
            self.config.samples,
            runs,
            self.p95_ms,
            self.available_memory_mb,
            self.ready,
        )


class HttpInferenceClient:
    def __init__(self, base_url: str, timeout_seconds: float = 15.0):
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
initialization_error: str | None = None
initialization_task: asyncio.Task | None = None
forecast_lock = asyncio.Lock()
app = FastAPI(title="Kronos inference", docs_url=None, redoc_url=None)


async def initialize_runtime() -> None:
    global runtime, initialization_error
    try:
        LOG.info("loading_kronos_model")
        loaded = await asyncio.to_thread(InferenceRuntime)
        LOG.info("running_inference_benchmark")
        await asyncio.to_thread(loaded.benchmark)
        runtime = loaded
    except Exception as exc:
        initialization_error = f"{type(exc).__name__}: {exc}"
        LOG.exception("inference_initialization_failed")


@app.on_event("startup")
async def startup() -> None:
    global initialization_task
    initialization_task = asyncio.create_task(initialize_runtime())


@app.post("/forecast")
async def forecast(request: ForecastRequest):
    if runtime is None or not runtime.ready:
        raise HTTPException(503, "Inference runtime is not ready")
    if forecast_lock.locked():
        raise HTTPException(429, "Inference is already processing a forecast")
    try:
        async with forecast_lock:
            return await asyncio.to_thread(runtime.forecast, request)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/health/live")
async def live():
    return {
        "live": True,
        "model_loaded": runtime is not None,
        "initialization_error": initialization_error,
    }


@app.get("/health/ready")
async def ready():
    if runtime is None or not runtime.ready:
        detail = (
            initialization_error
            or ("model_loading" if runtime is None else runtime.readiness_error)
        )
        raise HTTPException(503, detail or "Model benchmark has not passed")
    return {
        "ready": True,
        "samples": runtime.config.samples,
        "p95_ms": runtime.p95_ms,
        "available_memory_mb": runtime.available_memory_mb,
    }
