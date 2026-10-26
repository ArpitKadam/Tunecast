"""SQLite-backed job store and the in-process worker queue that drives the sidecar."""

from __future__ import annotations

import hashlib
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

from .gpu import GpuPeakSampler, query_gpus
from .log import log_event
from .sidecar import GenerateParams, Sidecar

STATUSES = ("queued", "running", "succeeded", "failed")
STALE_ERROR = "pod restarted"
SEED_RATIO = 0.6            # seconds of inference per second of audio, until measured
RATIO_WINDOW = 10
PROGRESS_CAP = 0.95
MIN_OBSERVATIONS = 3
ENDED_EARLY = 0.95          # audio shorter than this share of the requested ceiling means the lyrics ran out first

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
    error TEXT,
    audio_duration_s REAL,
    audio_sample_rate INTEGER,
    audio_channels INTEGER,
    audio_bytes INTEGER,
    request_id TEXT,
    request_hash TEXT,
    parent_job_id TEXT,
    experiment_id TEXT,
    model_revision TEXT,
    config_snapshot TEXT
)
"""
_AUDIO_COLUMNS = {"audio_duration_s": "REAL", "audio_sample_rate": "INTEGER", "audio_channels": "INTEGER", "audio_bytes": "INTEGER"}
_LINEAGE_COLUMNS = {"parent_job_id": "TEXT", "experiment_id": "TEXT", "model_revision": "TEXT", "config_snapshot": "TEXT"}
_ADDED_COLUMNS = {**_AUDIO_COLUMNS, "request_id": "TEXT", "request_hash": "TEXT", **_LINEAGE_COLUMNS}
# NULLs never collide, so submissions without a request ID keep working
_REQUEST_ID_INDEX = "CREATE UNIQUE INDEX IF NOT EXISTS jobs_request_id ON jobs (request_id)"
_EXPERIMENT_INDEX = "CREATE INDEX IF NOT EXISTS jobs_experiment_id ON jobs (experiment_id)"
_JSON_FIELDS = {"params", "timings", "gpu"}
_UPDATABLE = {"status", "started_at", "finished_at", "timings", "gpu", "error", *_AUDIO_COLUMNS}


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def fingerprint(params: dict) -> str:
    """Stable hash of a validated request body, so a reused request ID with different content is detectable."""
    return hashlib.sha256(json.dumps(params, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


class DuplicateRequest(Exception):
    """The request ID is already taken; `job` is the job that took it."""

    def __init__(self, job: "Job"):
        super().__init__(job.id)
        self.job = job


class UnknownParent(Exception):
    """parent_job_id does not name an existing job."""


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
    audio_duration_s: float | None = None
    audio_sample_rate: int | None = None
    audio_channels: int | None = None
    audio_bytes: int | None = None
    request_id: str | None = None
    request_hash: str | None = None
    parent_job_id: str | None = None
    experiment_id: str | None = None
    model_revision: str | None = None
    config_snapshot: dict | None = None

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
            **{col: row[col] for col in _ADDED_COLUMNS if col != "config_snapshot"},
            config_snapshot=json.loads(row["config_snapshot"]) if row["config_snapshot"] else None,
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
        have = {r["name"] for r in self._db.execute("PRAGMA table_info(jobs)")}
        for col, sql_type in _ADDED_COLUMNS.items():
            if col not in have:     # DB created before audio validation, request IDs or lineage
                self._db.execute(f"ALTER TABLE jobs ADD COLUMN {col} {sql_type}")
        self._db.execute(_REQUEST_ID_INDEX)
        self._db.execute(_EXPERIMENT_INDEX)
        self._db.execute("UPDATE jobs SET experiment_id = id WHERE experiment_id IS NULL")    # pre-lineage rows root their own experiment
        self._db.commit()

    def audio_path(self, job_id: str) -> Path:
        return self.outputs_dir / f"{job_id}.wav"

    def create(
        self, params: dict, request_id: str | None = None, request_hash: str | None = None, *,
        parent_job_id: str | None = None, experiment_id: str | None = None,
        model_revision: str | None = None, config_snapshot: dict | None = None,
    ) -> Job:
        """Raises DuplicateRequest when request_id already belongs to a job; the UNIQUE index decides races.

        Raises UnknownParent when parent_job_id names no job. The experiment is the one given, else the parent's,
        else the job's own id, so a branch always groups with the take it came from.
        """
        job = Job(id=new_job_id(), status="queued", created_at=utcnow(), params=dict(params),
                  request_id=request_id, request_hash=request_hash, parent_job_id=parent_job_id,
                  model_revision=model_revision, config_snapshot=config_snapshot)
        with self._lock:
            if request_id:      # checked before the parent: a replay must survive its parent being deleted
                row = self._db.execute("SELECT * FROM jobs WHERE request_id = ?", (request_id,)).fetchone()
                if row:
                    raise DuplicateRequest(Job.from_row(row))
            inherited = None
            if parent_job_id:
                parent = self._db.execute("SELECT experiment_id FROM jobs WHERE id = ?", (parent_job_id,)).fetchone()
                if parent is None:
                    raise UnknownParent(parent_job_id)
                inherited = parent["experiment_id"]
            job.experiment_id = experiment_id or inherited or job.id
            try:
                self._db.execute(
                    "INSERT INTO jobs (id, status, created_at, params, request_id, request_hash, parent_job_id, experiment_id, model_revision, config_snapshot) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (job.id, job.status, job.created_at, json.dumps(job.params), request_id, request_hash,
                     parent_job_id, job.experiment_id, model_revision, json.dumps(config_snapshot, sort_keys=True) if config_snapshot else None),
                )
                self._db.commit()
            except sqlite3.IntegrityError:
                self._db.rollback()
                row = self._db.execute("SELECT * FROM jobs WHERE request_id = ?", (request_id,)).fetchone() if request_id else None
                if row is None:
                    raise
                raise DuplicateRequest(Job.from_row(row)) from None
        return job

    def get_by_request_id(self, request_id: str) -> Job | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM jobs WHERE request_id = ?", (request_id,)).fetchone()
        return Job.from_row(row) if row else None

    def by_experiment(self, experiment_id: str) -> list[Job]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM jobs WHERE experiment_id = ? ORDER BY seq", (experiment_id,)).fetchall()
        return [Job.from_row(r) for r in rows]

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
    """Predicts inference seconds for a job from the last RATIO_WINDOW runs of this process.

    The rate is the median inference seconds per second of audio actually produced. The requested duration is
    only a ceiling (the model stops when the lyrics run out), so output length is predicted from lyrics length
    once MIN_OBSERVATIONS recent jobs ended early; until then the ceiling stands in for it.
    """

    def __init__(self):
        self._runs: deque[tuple[float, float, float | None, int]] = deque(maxlen=RATIO_WINDOW)   # audio_s, inference_s, ceiling_s, lyrics_chars
        self._lock = threading.Lock()

    def observe(self, audio_s: float, inference_s: float, ceiling_s: float | None = None, lyrics_chars: int = 0) -> None:
        if audio_s > 0:
            with self._lock:
                self._runs.append((audio_s, inference_s, ceiling_s, lyrics_chars))

    def estimate(self, ceiling_s: float, lyrics_chars: int = 0) -> tuple[float, str]:
        """(seconds, confidence). Confidence is "low" or "medium", never "high": there is no variance model."""
        with self._lock:
            runs = list(self._runs)
        if not runs:
            return SEED_RATIO * ceiling_s, "low"
        rate = statistics.median(inference_s / audio_s for audio_s, inference_s, _, _ in runs)
        early = [(audio_s, chars) for audio_s, _, ceiling, chars in runs if ceiling and audio_s < ENDED_EARLY * ceiling]
        per_second = [chars / audio_s for audio_s, chars in early if chars]
        informed = len(per_second) >= MIN_OBSERVATIONS and lyrics_chars > 0
        audio_s = min(ceiling_s, lyrics_chars / statistics.median(per_second)) if informed else ceiling_s
        # medium: enough runs, and the audio length is either predicted from lyrics or reliably the full ceiling
        medium = len(runs) >= MIN_OBSERVATIONS and (informed or not early)
        return rate * audio_s, "medium" if medium else "low"


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
        sampler = GpuPeakSampler(query_gpus)
        failure = None
        clock = time.monotonic()
        try:
            sampler.start()
            audio = self.sidecar.generate(params, out_path)
        except Exception as e:
            failure = e
        finally:
            inference_s = time.monotonic() - clock      # read before stop(), which waits for the last poll
            sampler.stop()
        gpu = sampler.peak
        if failure is not None:
            out_path.unlink(missing_ok=True)
            timings = {"queue_wait_s": round(queue_wait_s, 3), "inference_s": round(inference_s, 3)}
            self.store.update(job_id, status="failed", finished_at=utcnow(), error=str(failure)[:2000], timings=timings, gpu=gpu)
            log_event(self.logger, "job_failed", level=logging.ERROR, job_id=job_id, error=str(failure)[:500], gpu=gpu, **timings)
            return

        timings = {
            "queue_wait_s": round(queue_wait_s, 3),
            "inference_s": round(inference_s, 3),
            "total_s": round(queue_wait_s + inference_s, 3),
        }
        self.estimator.observe(audio.duration_s, inference_s, params.duration_s, len(params.lyrics))
        done = self.store.update(
            job_id, status="succeeded", finished_at=utcnow(), timings=timings, gpu=gpu,
            audio_duration_s=audio.duration_s, audio_sample_rate=audio.sample_rate,
            audio_channels=audio.channels, audio_bytes=audio.bytes,
        )
        if done == 0:
            out_path.unlink(missing_ok=True)   # row was deleted while we were generating
            log_event(self.logger, "job_discarded", job_id=job_id, reason="deleted during run")
            return
        log_event(self.logger, "job_succeeded", job_id=job_id, bytes=audio.bytes, audio_duration_s=audio.duration_s, gpu=gpu, **timings)

        pruned = self.store.prune(self.keep_last)
        if pruned:
            log_event(self.logger, "jobs_pruned", count=len(pruned), keep_last=self.keep_last)


def real_time_factor(job: Job) -> float | None:
    """Inference seconds per second of audio produced; None until both are known."""
    inference_s = job.timings.get("inference_s")
    if inference_s is None or not job.audio_duration_s:
        return None
    return round(inference_s / job.audio_duration_s, 3)


EXPORT_FIELDS = (
    "job_id", "experiment_id", "parent_job_id", "status", "created_at", "started_at", "finished_at",
    "seed", "duration_s_requested", "lyrics", "description", "model_revision",
    "queue_s", "inference_s", "audio_duration_s", "real_time_factor", "peak_gpu_mb", "error", "config_snapshot",
)


def export_row(job: Job) -> dict:
    """One flat record per job. peak_gpu_mb is the highest per-GPU peak; config_snapshot is the only nested value."""
    peaks = [g["peak_used_mb"] for g in job.gpu if "peak_used_mb" in g]
    return {
        "job_id": job.id,
        "experiment_id": job.experiment_id,
        "parent_job_id": job.parent_job_id,
        "status": job.status,
        "created_at": job.created_at,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
        "seed": job.params.get("seed"),
        "duration_s_requested": job.params.get("duration_s"),
        "lyrics": job.params.get("lyrics"),
        "description": job.params.get("description"),
        "model_revision": job.model_revision,
        "queue_s": job.timings.get("queue_wait_s"),
        "inference_s": job.timings.get("inference_s"),
        "audio_duration_s": job.audio_duration_s,
        "real_time_factor": real_time_factor(job),
        "peak_gpu_mb": max(peaks) if peaks else None,
        "error": job.error,
        "config_snapshot": job.config_snapshot,
    }


def job_to_dict(job: Job, store: JobStore, estimator: Estimator) -> dict:
    estimate, confidence = estimator.estimate(int(job.params.get("duration_s", 0)), len(job.params.get("lyrics", "")))
    queue_position = 0
    elapsed = 0.0
    fraction = 0.0
    live = job.status in ("queued", "running")

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
        "estimate": {"seconds": round(estimate, 1), "confidence": confidence} if live else None,
        "timings": job.timings,
        "real_time_factor": real_time_factor(job),
        "gpu": job.gpu,
        "audio_url": f"/jobs/{job.id}/audio" if job.status == "succeeded" else None,
        "audio_duration_s": job.audio_duration_s,
        "audio_sample_rate": job.audio_sample_rate,
        "audio_channels": job.audio_channels,
        "audio_bytes": job.audio_bytes,
        "error": job.error,
        "request_id": job.request_id,
        "parent_job_id": job.parent_job_id,
        "experiment_id": job.experiment_id,
        "model_revision": job.model_revision,
        "config_snapshot": job.config_snapshot,
    }
