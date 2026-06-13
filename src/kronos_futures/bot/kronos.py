from __future__ import annotations

import os
import random
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

KRONOS_COMMIT = "67b630e67f6a18c9e9be918d9b4337c960db1e9a"
MODEL_ID = "NeoQuasar/Kronos-small"
MODEL_REVISION = "901c26c1332695a2a8f243eb2f37243a37bea320"
TOKENIZER_ID = "NeoQuasar/Kronos-Tokenizer-base"
TOKENIZER_REVISION = "0e0117387f39004a9016484a186a908917e22426"
FEATURES = ("open", "high", "low", "close", "volume", "amount")


@dataclass(frozen=True)
class ForecastConfig:
    context: int = 512
    horizon: int = 1
    samples: int = 16
    temperature: float = 1.0
    top_p: float = 0.9
    top_k: int = 0
    seed: int = 123
    clip: float = 5.0


def set_seed(seed: int) -> None:
    random.seed(seed)
    np.random.seed(seed)
    import torch

    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)


def _load_classes(vendor: Path):
    marker = vendor / ".kronos_commit"
    if marker.exists():
        head = marker.read_text(encoding="ascii").strip()
    else:
        head = subprocess.check_output(
            ["git", "-C", str(vendor), "rev-parse", "HEAD"], text=True
        ).strip()
    if head != KRONOS_COMMIT:
        raise RuntimeError(f"Kronos checkout must be {KRONOS_COMMIT}, found {head}")
    sys.path.insert(0, str(vendor))
    from model import Kronos, KronosTokenizer

    return Kronos, KronosTokenizer


class KronosPathForecaster:
    def __init__(self, config: ForecastConfig, vendor: Path | None = None):
        vendor = vendor or Path(os.getenv("KRONOS_VENDOR", "/app/vendor/Kronos"))
        if not vendor.exists():
            raise FileNotFoundError(f"Kronos checkout not found at {vendor}")
        Kronos, KronosTokenizer = _load_classes(vendor)
        self.config = config
        self.device = "cpu"
        self.tokenizer = KronosTokenizer.from_pretrained(
            TOKENIZER_ID, revision=TOKENIZER_REVISION
        ).to(self.device).eval()
        self.model = Kronos.from_pretrained(
            MODEL_ID, revision=MODEL_REVISION
        ).to(self.device).eval()

    @staticmethod
    def _stamps(ts: pd.DatetimeIndex) -> np.ndarray:
        return np.column_stack(
            [ts.minute, ts.hour, ts.weekday, ts.day, ts.month]
        ).astype(np.float32)

    def forecast_batch(
        self, frames: list[pd.DataFrame], future: list[pd.DatetimeIndex]
    ) -> np.ndarray:
        import torch
        from model.kronos import sample_from_logits

        config = self.config
        set_seed(config.seed)
        arrays, x_stamps, y_stamps, means, stds = [], [], [], [], []
        for frame, future_ts in zip(frames, future, strict=True):
            values = frame.loc[:, FEATURES].to_numpy(np.float32)
            mean, std = values.mean(0), values.std(0)
            arrays.append(
                np.clip((values - mean) / (std + 1e-5), -config.clip, config.clip)
            )
            x_stamps.append(self._stamps(frame.index))
            y_stamps.append(self._stamps(future_ts))
            means.append(mean)
            stds.append(std)

        values = torch.tensor(np.stack(arrays), device=self.device)
        x_stamp = torch.tensor(np.stack(x_stamps), device=self.device)
        y_stamp = torch.tensor(np.stack(y_stamps), device=self.device)
        batch, context, _ = values.shape
        samples = config.samples
        with torch.inference_mode():
            values = values[:, None].repeat(1, samples, 1, 1).reshape(
                batch * samples, context, -1
            )
            x_stamp = x_stamp[:, None].repeat(1, samples, 1, 1).reshape(
                batch * samples, context, -1
            )
            y_stamp = y_stamp[:, None].repeat(1, samples, 1, 1).reshape(
                batch * samples, config.horizon, -1
            )
            coarse, fine = self.tokenizer.encode(values, half=True)
            stamps = torch.cat([x_stamp, y_stamp], dim=1)
            for _ in range(config.horizon):
                start = max(0, coarse.shape[1] - config.context)
                stamp = stamps[:, start : coarse.shape[1]]
                coarse_logits, hidden = self.model.decode_s1(
                    coarse[:, start:], fine[:, start:], stamp
                )
                next_coarse = sample_from_logits(
                    coarse_logits[:, -1],
                    config.temperature,
                    config.top_k,
                    config.top_p,
                    True,
                )
                fine_logits = self.model.decode_s2(hidden, next_coarse)
                next_fine = sample_from_logits(
                    fine_logits[:, -1],
                    config.temperature,
                    config.top_k,
                    config.top_p,
                    True,
                )
                coarse = torch.cat([coarse, next_coarse], dim=1)
                fine = torch.cat([fine, next_fine], dim=1)
            decoded = self.tokenizer.decode(
                [coarse[:, -config.context :], fine[:, -config.context :]],
                half=True,
            )
            decoded = decoded[:, -config.horizon :]
            decoded = decoded.reshape(
                batch, samples, config.horizon, len(FEATURES)
            ).cpu().numpy()
        mean_array = np.asarray(means)[:, None, None, :]
        std_array = np.asarray(stds)[:, None, None, :]
        return decoded * (std_array + 1e-5) + mean_array
