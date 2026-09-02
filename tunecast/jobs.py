"""SQLite-backed job store and the in-process worker queue that drives the sidecar."""

from __future__ import annotations

import json
import logging
import queue
import secrets
import sqlite3
import statistics
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .gpu import query_gpus
from .log import log_event
from .sidecar import GenerateParams, Sidecar

STATUSES = ("queued", "running", "succeeded", "failed")
STALE_ERROR = "pod restarted"
SEED_RATIO = 0.6            # seconds of inference per second of audio, until measured
RATIO_WINDOW = 10
PROGRESS_CAP = 0.95

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    params TEXT NOT NULL,
    timings TEXT NOT NULL DEFAULT '{}',
    gpu TEXT NOT NULL DEFAULT '[]',
    error TEXT
)
"""
_JSON_FIELDS = {"params", "timings", "gpu"}
_UPDATABLE = {"status", "started_at", "finished_at", "timings", "gpu", "error"}


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def new_job_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)


@dataclass
class Job:
    id: str
    status: str
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    params: dict = field(default_factory=dict)
    timings: dict = field(default_factory=dict)
    gpu: list = field(default_factory=list)
    error: str | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Job":
        return cls(
            id=row["id"],
            status=row["status"],
            created_at=row["created_at"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            params=json.loads(row["params"]),
            timings=json.loads(row["timings"]),
            gpu=json.loads(row["gpu"]),
            error=row["error"],
        )


class JobStore:
    def __init__(self, db_path: Path, outputs_dir: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.outputs_dir = outputs_dir
        self._lock = threading.Lock()
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute(_SCHEMA)
        self._db.commit()

    def audio_path(self, job_id: str) -> Path:
        return self.outputs_dir / f"{job_id}.wav"

    def create(self, params: dict) -> Job:
        job = Job(id=new_job_id(), status="queued", created_at=utcnow(), params=dict(params))
        with self._lock:
            self._db.execute(
                "INSERT INTO jobs (id, status, created_at, params) VALUES (?, ?, ?, ?)",
                (job.id, job.status, job.created_at, json.dumps(job.params)),
            )
            self._db.commit()
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return Job.from_row(row) if row else None

    def list(self, limit: int = 50) -> list[Job]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM jobs ORDER BY seq DESC LIMIT ?", (limit,)).fetchall()
        return [Job.from_row(r) for r in rows]

    def update(self, job_id: str, **fields) -> int:
        """Returns the number of rows changed: 0 means the job no longer exists."""
        unknown = set(fields) - _UPDATABLE
        if unknown:
            raise ValueError(f"cannot update {sorted(unknown)}")
        assignments = ", ".join(f"{k} = ?" for k in fields)
        values = [json.dumps(v) if k in _JSON_FIELDS else v for k, v in fields.items()]
        with self._lock:
            cur = self._db.execute(f"UPDATE jobs SET {assignments} WHERE id = ?", (*values, job_id))
            self._db.commit()
        return cur.rowcount

    def delete(self, job_id: str) -> bool:
        """Delete row and audio unless the job is running. False when running or unknown."""
        with self._lock:
            cur = self._db.execute("DELETE FROM jobs WHERE id = ? AND status != 'running'", (job_id,))
            self._db.commit()
        if cur.rowcount == 0:
            return False
        self.audio_path(job_id).unlink(missing_ok=True)
        return True

    def queued_ids(self) -> list[str]:
        with self._lock:
            rows = self._db.execute("SELECT id FROM jobs WHERE status = 'queued' ORDER BY seq").fetchall()
        return [r["id"] for r in rows]

    def ahead_of(self, job_id: str) -> int:
        """Jobs that will finish before this one: running, or queued earlier."""
        with self._lock:
            row = self._db.execute(
                "SELECT COUNT(*) AS n FROM jobs WHERE status IN ('queued', 'running') "
                "AND seq < (SELECT seq FROM jobs WHERE id = ?)",
                (job_id,),
            ).fetchone()
        return int(row["n"]) if row else 0

    def mark_stale_failed(self) -> int:
        with self._lock:
            cur = self._db.execute(
                "UPDATE jobs SET status = 'failed', error = ?, finished_at = ? WHERE status IN ('queued', 'running')",
                (STALE_ERROR, utcnow()),
            )
            self._db.commit()
        return cur.rowcount

    def prune(self, keep_last: int) -> list[str]:
        with self._lock:
            rows = self._db.execute(
                "SELECT id FROM jobs WHERE status = 'succeeded' ORDER BY seq DESC LIMIT -1 OFFSET ?",
                (keep_last,),
            ).fetchall()
        doomed = [r["id"] for r in rows]
        for job_id in doomed:
            self.delete(job_id)
        return doomed


class Estimator:
    """Predicts inference seconds from audio seconds using the median of recent jobs."""

    def __init__(self):
        self._ratios: deque[float] = deque(maxlen=RATIO_WINDOW)
        self._lock = threading.Lock()

    def observe(self, duration_s: int, inference_s: float) -> None:
        if duration_s > 0:
            with self._lock:
                self._ratios.append(inference_s / duration_s)

    def seconds_for(self, duration_s: int) -> float:
        with self._lock:
            ratio = statistics.median(self._ratios) if self._ratios else SEED_RATIO
        return ratio * duration_s


class JobRunner:
    def __init__(
        self,
        store: JobStore,
        sidecar: Sidecar,
        max_concurrent: int,
        estimator: Estimator,
        keep_last: int,
        logger: logging.Logger,
    ):
        self.store = store
        self.sidecar = sidecar
        self.max_concurrent = max_concurrent
        self.estimator = estimator
        self.keep_last = keep_last
        self.logger = logger
        self._queue: queue.Queue[str] = queue.Queue()

    def start(self) -> None:
        for n in range(self.max_concurrent):
            threading.Thread(target=self._loop, name=f"tunecast-worker-{n}", daemon=True).start()

    def submit(self, job_id: str) -> None:
        self._queue.put(job_id)

    def _loop(self) -> None:
        while True:
            job_id = self._queue.get()
            try:
                self._run(job_id)
            except Exception as e:  # never let a worker die
                log_event(self.logger, "job_worker_error", level=logging.ERROR, job_id=job_id, error=str(e))
                job = self.store.get(job_id)
                if job and job.status == "running":
                    self.store.update(job_id, status="failed", finished_at=utcnow(), error=str(e)[:2000])
            finally:
                self._queue.task_done()

    def _run(self, job_id: str) -> None:
        job = self.store.get(job_id)
        if job is None or job.status != "queued":
            return
        started_at = utcnow()
        queue_wait_s = (parse_ts(started_at) - parse_ts(job.created_at)).total_seconds()
        self.store.update(job_id, status="running", started_at=started_at)
        log_event(self.logger, "job_started", job_id=job_id, queue_wait_s=round(queue_wait_s, 3), duration_s=job.params.get("duration_s"))

        params = GenerateParams(**job.params)
        out_path = self.store.audio_path(job_id)
        clock = time.monotonic()
        try:
            self.sidecar.generate(params, out_path)
        except Exception as e:
            inference_s = time.monotonic() - clock
            timings = {"queue_wait_s": round(queue_wait_s, 3), "inference_s": round(inference_s, 3)}
            self.store.update(job_id, status="failed", finished_at=utcnow(), error=str(e)[:2000], timings=timings)
            log_event(self.logger, "job_failed", level=logging.ERROR, job_id=job_id, error=str(e)[:500], **timings)
            return
        inference_s = time.monotonic() - clock

        gpu = query_gpus()
        timings = {
            "queue_wait_s": round(queue_wait_s, 3),
            "inference_s": round(inference_s, 3),
            "total_s": round(queue_wait_s + inference_s, 3),
        }
        self.estimator.observe(params.duration_s, inference_s)
        if self.store.update(job_id, status="succeeded", finished_at=utcnow(), timings=timings, gpu=gpu) == 0:
            out_path.unlink(missing_ok=True)   # row was deleted while we were generating
            log_event(self.logger, "job_discarded", job_id=job_id, reason="deleted during run")
            return
        log_event(self.logger, "job_succeeded", job_id=job_id, bytes=out_path.stat().st_size, gpu=gpu, **timings)

        pruned = self.store.prune(self.keep_last)
        if pruned:
            log_event(self.logger, "jobs_pruned", count=len(pruned), keep_last=self.keep_last)


def job_to_dict(job: Job, store: JobStore, estimator: Estimator) -> dict:
    duration_s = int(job.params.get("duration_s", 0))
    estimate = estimator.seconds_for(duration_s)
    queue_position = 0
    elapsed = 0.0
    fraction = 0.0

    if job.status == "queued":
        queue_position = store.ahead_of(job.id)
    elif job.status == "running" and job.started_at:
        elapsed = (datetime.now(timezone.utc) - parse_ts(job.started_at)).total_seconds()
        fraction = min(elapsed / estimate, PROGRESS_CAP) if estimate > 0 else 0.0
    elif job.status == "succeeded":
        elapsed = float(job.timings.get("inference_s", 0.0))
        estimate = elapsed
        fraction = 1.0
    else:
        elapsed = float(job.timings.get("inference_s", 0.0))

    return {
        "id": job.id,
        "status": job.status,
        "queue_position": queue_position,
        "created_at": job.created_at,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
        "params": job.params,
        "progress": {
            "elapsed_s": round(elapsed, 1),
            "estimated_total_s": round(estimate, 1),
            "fraction": round(fraction, 3),
        },
        "timings": job.timings,
        "gpu": job.gpu,
        "audio_url": f"/jobs/{job.id}/audio" if job.status == "succeeded" else None,
        "error": job.error,
    }
