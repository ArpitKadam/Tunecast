"""FastAPI surface: bearer auth, async job submission, status, audio download, readiness, UI."""

from __future__ import annotations

import csv
import hmac
import io
import json
import secrets
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from importlib.resources import files
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Path, Query, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from .config import Settings
from .gpu import query_gpus
from .jobs import EXPORT_FIELDS, DuplicateRequest, JobRunner, JobStore, QueueFull, UnknownParent, export_row, fingerprint, job_to_dict
from .sidecar import Sidecar

MAX_DURATION_S = 360
MAX_LYRICS_CHARS = 20_000
MAX_DESCRIPTION_CHARS = 10_000
MAX_SEED = 2**31 - 1
# ponytail: fixed hint, not derived from the estimator; a slot opens when the running take ends (minutes, estimate is a guess)
RETRY_AFTER_S = 30
REQUEST_ID_PATTERN = r"^[A-Za-z0-9_-]{8,128}$"
EXPERIMENT_ID_PATTERN = r"^[A-Za-z0-9_.-]{1,64}$"     # also matches job IDs, which name an experiment by default


@dataclass
class ReadyState:
    model: bool = False
    tunnel: bool = False


class JobRequest(BaseModel):
    lyrics: str = Field(min_length=1, max_length=MAX_LYRICS_CHARS)
    description: str = Field(min_length=1, max_length=MAX_DESCRIPTION_CHARS)
    duration_s: int = Field(default=180, ge=1, le=MAX_DURATION_S)
    seed: int | None = Field(default=None, ge=0, le=MAX_SEED)
    format: Literal["wav"] = "wav"
    parent_job_id: str | None = Field(default=None, pattern=EXPERIMENT_ID_PATTERN)
    experiment_id: str | None = Field(default=None, pattern=EXPERIMENT_ID_PATTERN)


def _app_version() -> str:
    try:
        return version("tunecast")
    except PackageNotFoundError:
        return "unknown"


def create_app(settings: Settings, store: JobStore, runner: JobRunner, sidecar: Sidecar, state: ReadyState) -> FastAPI:
    app = FastAPI(title="Tunecast", docs_url=None, redoc_url=None)
    key_bytes = settings.api_key.encode("utf-8")
    gpus = query_gpus()
    # Recorded with every job. Settings only by name, never the API key, ngrok token or HF token.
    config_snapshot = {
        "app_version": _app_version(),
        "model_id": settings.model_id,
        "stub": settings.stub,
        "sidecar": sidecar.describe(),
        "max_concurrent": settings.max_concurrent,
        "keep_last": settings.keep_last,
        "dit_steps": settings.dit_steps,
        "dit_cfg_scale": settings.dit_cfg_scale,
        "gpu_count": len(gpus),
        "gpu_names": [g["name"] for g in gpus],
    }

    def require_key(authorization: str | None = Header(default=None)) -> None:
        scheme, _, token = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(token.strip().encode("utf-8"), key_bytes):
            raise HTTPException(status_code=401, detail="invalid or missing API key", headers={"WWW-Authenticate": "Bearer"})

    auth = [Depends(require_key)]

    def load_job(job_id: str):
        job = store.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        return job

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/ready")
    def ready():
        ok = state.model and state.tunnel
        return JSONResponse(status_code=200 if ok else 503, content={"model": state.model, "tunnel": state.tunnel})

    @app.get("/info", dependencies=auth)
    def info():
        return {
            "model_id": settings.model_id,
            "model_revision": settings.model_revision,
            "stub": settings.stub,
            "sidecar": sidecar.describe(),
            "gpus": query_gpus(),
            "max_concurrent": settings.max_concurrent,
            "keep_last": settings.keep_last,
            "limits": {
                "max_duration_s": MAX_DURATION_S,
                "max_lyrics_chars": MAX_LYRICS_CHARS,
                "max_description_chars": MAX_DESCRIPTION_CHARS,
                "formats": ["wav"],
            },
        }

    @app.post("/jobs", dependencies=auth, status_code=202)
    def submit(req: JobRequest, idempotency_key: str | None = Header(default=None, pattern=REQUEST_ID_PATTERN)):
        retry = {"Retry-After": str(RETRY_AFTER_S)}     # its presence marks a definitive admission refusal, nothing was created
        if not state.model and not (idempotency_key and store.get_by_request_id(idempotency_key)):
            raise HTTPException(status_code=503, detail="the model server is not ready", headers=retry)
        params = req.model_dump()
        lineage = {k: params.pop(k) for k in ("parent_job_id", "experiment_id")}
        # lineage joins the fingerprint only when set, so bodies without it keep their pre-lineage hashes
        # fingerprint is taken before the seed is drawn, so a retry without a seed still matches
        digest = fingerprint({**params, **{k: v for k, v in lineage.items() if v is not None}})
        if params["seed"] is None:
            params["seed"] = secrets.randbelow(MAX_SEED + 1)
        try:
            job = store.create(params, idempotency_key, digest, settings.max_queued, **lineage, model_revision=settings.model_revision, config_snapshot=config_snapshot)
        except QueueFull as full:
            raise HTTPException(
                status_code=429,
                detail=f"queue is full ({full.queued} of {settings.max_queued} takes waiting)",
                headers=retry,
            ) from None
        except DuplicateRequest as dup:
            if dup.job.request_hash != digest:
                raise HTTPException(status_code=409, detail="Idempotency-Key was already used for a different request") from None
            # replay: 200, not 202, because nothing new was accepted
            return JSONResponse(status_code=200, content=job_to_dict(dup.job, store, runner.estimator))
        except UnknownParent as missing:
            raise HTTPException(status_code=422, detail=f"parent_job_id {missing} does not exist") from None
        runner.submit(job.id)
        return JSONResponse(status_code=202, content=job_to_dict(job, store, runner.estimator))

    @app.get("/jobs", dependencies=auth)
    def list_jobs(limit: int = Query(default=50, ge=1, le=200), request_id: str | None = Query(default=None, pattern=REQUEST_ID_PATTERN)):
        if request_id is not None:
            job = store.get_by_request_id(request_id)
            return [job_to_dict(job, store, runner.estimator)] if job else []
        return [job_to_dict(j, store, runner.estimator) for j in store.list(limit)]

    @app.get("/jobs/{job_id}", dependencies=auth)
    def get_job(job_id: str):
        return job_to_dict(load_job(job_id), store, runner.estimator)

    @app.get("/jobs/{job_id}/audio", dependencies=auth)
    def audio(job_id: str):
        job = load_job(job_id)
        path = store.audio_path(job_id)
        if job.status != "succeeded" or not path.exists():
            raise HTTPException(status_code=409, detail=f"audio not available, job is {job.status}")
        return FileResponse(path, media_type="audio/wav", filename=f"{job_id}.wav")

    @app.get("/experiments/{experiment_id}/export", dependencies=auth)
    def export_experiment(
        experiment_id: str = Path(pattern=EXPERIMENT_ID_PATTERN),
        fmt: Literal["json", "csv"] = Query(default="json", alias="format"),
    ):
        jobs = store.by_experiment(experiment_id)
        if not jobs:
            raise HTTPException(status_code=404, detail="experiment not found")
        rows = [export_row(j) for j in jobs]
        headers = {"Content-Disposition": f'attachment; filename="experiment-{experiment_id}.{fmt}"'}
        if fmt == "json":
            return JSONResponse(rows, headers=headers)
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=EXPORT_FIELDS)
        writer.writeheader()
        writer.writerows({**r, "config_snapshot": json.dumps(r["config_snapshot"], sort_keys=True) if r["config_snapshot"] else None} for r in rows)
        return Response(buf.getvalue(), media_type="text/csv; charset=utf-8", headers=headers)

    @app.delete("/jobs/{job_id}", dependencies=auth, status_code=204)
    def delete_job(job_id: str):
        load_job(job_id)
        if not store.delete(job_id):
            raise HTTPException(status_code=409, detail="a running job cannot be cancelled; delete it once it finishes")
        return Response(status_code=204)

    @app.get("/", response_class=HTMLResponse)
    def ui():
        return HTMLResponse(files("tunecast").joinpath("static/index.html").read_text(encoding="utf-8"))

    return app
