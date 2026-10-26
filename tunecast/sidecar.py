"""Inference backends behind one interface: the official sgl-omni HTTP server, or a stub for local dev."""

from __future__ import annotations

import http.client
import json
import os
import struct
import time
import urllib.error
import urllib.request
import wave
from array import array
from dataclasses import dataclass
from io import BytesIO
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


@dataclass(frozen=True)
class AudioInfo:
    duration_s: float
    sample_rate: int
    channels: int
    bytes: int


class Sidecar(Protocol):
    def ready(self) -> bool: ...
    def generate(self, params: GenerateParams, out_path: Path) -> AudioInfo: ...
    def describe(self) -> dict: ...


_WAVE_PCM, _WAVE_FLOAT, _WAVE_EXTENSIBLE = 1, 3, 0xFFFE


def inspect_wav(data: bytes) -> AudioInfo:
    """Parse a RIFF/WAVE header (PCM, IEEE float, or EXTENSIBLE wrapping either); raise SidecarError unless it is whole, playable audio."""
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        head = data[:200]
        hint = repr(head.decode("ascii")) if head.isascii() else f"{len(data)} binary bytes"
        raise SidecarError(f"sidecar returned a body that is not a WAV file: {hint}")
    fmt = None
    pos = 12
    while pos + 8 <= len(data):
        chunk_id, size = struct.unpack_from("<4sI", data, pos)
        body = pos + 8
        if chunk_id == b"fmt ":
            if size < 16 or body + size > len(data):
                raise SidecarError("WAV fmt chunk is truncated")
            tag, channels, rate, _, align, bits = struct.unpack_from("<HHIIHH", data, body)
            if tag == _WAVE_EXTENSIBLE:
                if size < 40:
                    raise SidecarError("WAV extensible fmt chunk is truncated")
                tag = struct.unpack_from("<H", data, body + 24)[0]
            fmt = (tag, channels, rate, align, bits)
        elif chunk_id == b"data":
            if fmt is None:
                raise SidecarError("WAV data chunk comes before fmt chunk")
            tag, channels, rate, align, bits = fmt
            if tag not in (_WAVE_PCM, _WAVE_FLOAT):
                raise SidecarError(f"WAV format {tag} is not PCM or IEEE float")
            if not (channels and rate and bits and bits % 8 == 0 and align == channels * bits // 8):
                raise SidecarError(f"WAV fmt chunk is inconsistent (channels={channels}, rate={rate}, bits={bits}, block_align={align})")
            if body + size > len(data):
                raise SidecarError(f"WAV truncated: data chunk declares {size} bytes, {len(data) - body} received")
            if size % align:
                raise SidecarError(f"WAV data length {size} is not a whole number of {align}-byte frames")
            if size == 0:
                raise SidecarError("WAV contains no audio frames")
            return AudioInfo(duration_s=round(size / align / rate, 3), sample_rate=rate, channels=channels, bytes=len(data))
        pos = body + size + (size & 1)
    raise SidecarError("WAV has no data chunk" if fmt else "WAV has no fmt chunk")


def _write_atomic(out_path: Path, data: bytes) -> None:
    tmp = out_path.with_suffix(out_path.suffix + ".part")
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_bytes(data)
        os.replace(tmp, out_path)
    except OSError as e:
        tmp.unlink(missing_ok=True)
        out_path.unlink(missing_ok=True)
        raise SidecarError(f"could not write audio file: {e}") from None


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

    def generate(self, params: GenerateParams, out_path: Path) -> AudioInfo:
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
        except http.client.IncompleteRead as e:  # read() enforces Content-Length: short body raises here
            raise SidecarError(f"sidecar response truncated: got {len(e.partial)} of {len(e.partial) + e.expected} bytes") from None
        except (TimeoutError, urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as e:
            if isinstance(e, TimeoutError) or isinstance(getattr(e, "reason", None), TimeoutError):
                raise SidecarError(f"sidecar timed out after {params.timeout_s}s") from None
            raise SidecarError(f"sidecar unreachable: {e}") from None
        if not body:
            raise SidecarError("sidecar returned an empty body")
        info = inspect_wav(body)
        _write_atomic(out_path, body)
        return info

    def describe(self) -> dict:
        return {"kind": "sglang-omni", "base_url": self.base_url, "model": self.model_id}


class StubSidecar:
    """No CUDA, no model: sleeps briefly, then writes a 400 Hz sine of the requested length."""

    TONE_HZ = 400  # 32000 / 400 = 80 samples per cycle, so one block tiles exactly

    def __init__(self, delay_s: float | None = None):
        self.delay_s = delay_s

    def ready(self) -> bool:
        return True

    def generate(self, params: GenerateParams, out_path: Path) -> AudioInfo:
        delay = min(params.duration_s * 0.05, 3.0) if self.delay_s is None else self.delay_s
        if delay > 0:
            time.sleep(delay)
        cycle = SAMPLE_RATE // self.TONE_HZ
        block = array("h")
        for i in range(cycle):
            sample = int(8000 * sin(2 * pi * i / cycle))
            block.extend((sample, sample))
        repeats = params.duration_s * SAMPLE_RATE // cycle
        buf = BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(CHANNELS)
            w.setsampwidth(2)
            w.setframerate(SAMPLE_RATE)
            w.writeframes(block.tobytes() * repeats)
        info = inspect_wav(buf.getvalue())
        _write_atomic(out_path, buf.getvalue())
        return info

    def describe(self) -> dict:
        return {"kind": "stub", "tone_hz": self.TONE_HZ}
