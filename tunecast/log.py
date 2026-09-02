"""One-line JSON logging to stdout and, when a directory is given, a .jsonl file."""

from __future__ import annotations

import json
import logging
import sys
import time
from collections.abc import Mapping
from pathlib import Path

LOGGER_NAME = "tunecast"
SECRET_MARKERS = ("KEY", "TOKEN", "SECRET")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        rec = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "event": record.getMessage(),
        }
        fields = getattr(record, "fields", None)
        if fields:
            rec.update(fields)
        if record.exc_info:
            rec["exc"] = self.formatException(record.exc_info)
        return json.dumps(rec, default=str)


def setup_logging(logs_dir: Path | None, filename: str = "boot.jsonl", name: str = LOGGER_NAME) -> logging.Logger:
    """Return the named logger with fresh handlers; safe to call more than once."""
    logger = logging.getLogger(name)
    for handler in list(logger.handlers):
        handler.close()
        logger.removeHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    formatter = JsonFormatter()

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(formatter)
    logger.addHandler(stream)

    if logs_dir is not None:
        logs_dir.mkdir(parents=True, exist_ok=True)
        file = logging.FileHandler(logs_dir / filename, encoding="utf-8")
        file.setFormatter(formatter)
        logger.addHandler(file)
    return logger


def log_event(logger: logging.Logger, event: str, level: int = logging.INFO, **fields) -> None:
    logger.log(level, event, extra={"fields": fields})


def redact(env: Mapping[str, str]) -> dict[str, str]:
    """Mask any variable whose name looks like a credential."""
    return {
        key: "***" if any(marker in key.upper() for marker in SECRET_MARKERS) else value
        for key, value in env.items()
    }
