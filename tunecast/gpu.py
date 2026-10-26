"""GPU memory snapshot via nvidia-smi. No CUDA bindings; empty list when the tool is absent."""

from __future__ import annotations

import subprocess
import threading

SAMPLE_INTERVAL_S = 1.0
QUERY = ["nvidia-smi", "--query-gpu=index,name,memory.used,memory.total", "--format=csv,noheader,nounits"]


def parse_nvidia_smi(text: str) -> list[dict]:
    gpus = []
    for line in text.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != 4:
            continue
        try:
            gpus.append({"index": int(parts[0]), "name": parts[1], "used_mb": int(parts[2]), "total_mb": int(parts[3])})
        except ValueError:
            continue
    return gpus


def query_gpus() -> list[dict]:
    try:
        result = subprocess.run(QUERY, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0:
        return []
    return parse_nvidia_smi(result.stdout)


class GpuPeakSampler:
    """Polls `query` on a daemon thread and keeps the highest used memory seen per GPU.

    It reads whole-GPU usage, so resident weights and any concurrent job are included.
    """

    def __init__(self, query=query_gpus, interval_s: float = SAMPLE_INTERVAL_S):
        self._query = query
        self._interval_s = interval_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._peaks: dict[int, dict] = {}

    def _sample(self) -> None:
        try:
            gpus = self._query()
        except Exception:
            return      # a failed poll must never fail the job being measured
        for g in gpus:
            seen = self._peaks.get(g["index"])
            if seen is None or g["used_mb"] > seen["peak_used_mb"]:
                self._peaks[g["index"]] = {"index": g["index"], "name": g["name"], "peak_used_mb": g["used_mb"], "total_mb": g["total_mb"]}

    def _run(self) -> None:
        while True:
            self._sample()
            if self._stop.wait(self._interval_s):
                return

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="tunecast-gpu-sampler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Idempotent. Takes one last sample so the end of the run is covered."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
            self._thread = None
            self._sample()

    @property
    def peak(self) -> list[dict]:
        return [self._peaks[i] for i in sorted(self._peaks)]
