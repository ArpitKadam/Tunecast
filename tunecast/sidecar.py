"""Inference backends behind one interface: the official sgl-omni HTTP server, or a stub for local dev."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
import wave
from array import array
from dataclasses import dataclass
from math import pi, sin
from pathlib import Path
from typing import Protocol

FRAMES_PER_SECOND = 25          # model frame rate: max_new_tokens = seconds * 25
SAMPLE_RATE = 32000             # model output: 32 kHz, 16-bit, stereo
CHANNELS = 2
MAX_ERROR_CHARS = 2000


class SidecarError(Exception):
    """Inference failed; the message is safe to show in job status."""


@dataclass(frozen=True)
class GenerateParams:
    lyrics: str
    description: str
    duration_s: int
    seed: int
    format: str = "wav"

    @property
    def max_new_tokens(self) -> int:
        return self.duration_s * FRAMES_PER_SECOND

    @property
    def timeout_s(self) -> int:
        return self.duration_s * 10 + 120


class Sidecar(Protocol):
    def ready(self) -> bool: ...
    def generate(self, params: GenerateParams, out_path: Path) -> None: ...
    def describe(self) -> dict: ...


def _write_atomic(out_path: Path, data: bytes) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(out_path.suffix + ".part")
    tmp.write_bytes(data)
    os.replace(tmp, out_path)


class HttpSidecar:
    """Client for `sgl-omni serve` (OpenAI-compatible /v1/audio/speech)."""

    def __init__(self, base_url: str, model_id: str):
        self.base_url = base_url.rstrip("/")
        self.model_id = model_id

    def ready(self) -> bool:
        for path in ("/health", "/v1/models"):
            try:
                with urllib.request.urlopen(self.base_url + path, timeout=5) as resp:
                    if resp.status == 200:
                        return True
            except urllib.error.HTTPError:
                continue                      # server is up, path unsupported: try the next
            except (urllib.error.URLError, OSError, ValueError):
                return False                  # nothing listening: no point trying another path
        return False

    def generate(self, params: GenerateParams, out_path: Path) -> None:
        payload = {
            "model": self.model_id,
            "input": params.lyrics,
            "instructions": params.description,
            "seed": params.seed,
            "max_new_tokens": params.max_new_tokens,
            "response_format": params.format,
            "stream": False,
        }
        req = urllib.request.Request(
            self.base_url + "/v1/audio/speech",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=params.timeout_s) as resp:
                body = resp.read()
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:MAX_ERROR_CHARS]
            raise SidecarError(f"sidecar HTTP {e.code}: {detail}") from None
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise SidecarError(f"sidecar unreachable: {e}") from None
        if not body:
            raise SidecarError("sidecar returned an empty body")
        _write_atomic(out_path, body)

    def describe(self) -> dict:
        return {"kind": "sglang-omni", "base_url": self.base_url, "model": self.model_id}


class StubSidecar:
    """No CUDA, no model: sleeps briefly, then writes a 400 Hz sine of the requested length."""

    TONE_HZ = 400  # 32000 / 400 = 80 samples per cycle, so one block tiles exactly

    def __init__(self, delay_s: float | None = None):
        self.delay_s = delay_s

    def ready(self) -> bool:
        return True

    def generate(self, params: GenerateParams, out_path: Path) -> None:
        delay = min(params.duration_s * 0.05, 3.0) if self.delay_s is None else self.delay_s
        if delay > 0:
            time.sleep(delay)
        cycle = SAMPLE_RATE // self.TONE_HZ
        block = array("h")
        for i in range(cycle):
            sample = int(8000 * sin(2 * pi * i / cycle))
            block.extend((sample, sample))
        repeats = params.duration_s * SAMPLE_RATE // cycle
        out_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = out_path.with_suffix(out_path.suffix + ".part")
        with wave.open(str(tmp), "wb") as w:
            w.setnchannels(CHANNELS)
            w.setsampwidth(2)
            w.setframerate(SAMPLE_RATE)
            w.writeframes(block.tobytes() * repeats)
        os.replace(tmp, out_path)

    def describe(self) -> dict:
        return {"kind": "stub", "tone_hz": self.TONE_HZ}
