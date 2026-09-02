"""GPU memory snapshot via nvidia-smi. No CUDA bindings; empty list when the tool is absent."""

from __future__ import annotations

import subprocess

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
