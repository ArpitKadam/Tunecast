"""FastAPI surface: bearer auth, async job submission, status, audio download, readiness, UI."""

from __future__ import annotations

import hmac
import secrets
from dataclasses import dataclass
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from .config import Settings
from .gpu import query_gpus
from .jobs import JobRunner, JobStore, job_to_dict
from .sidecar import Sidecar

MAX_DURATION_S = 360
MAX_LYRICS_CHARS = 20_000
MAX_DESCRIPTION_CHARS = 10_000
MAX_SEED = 2**31 - 1


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


def create_app(settings: Settings, store: JobStore, runner: JobRunner, sidecar: Sidecar, state: ReadyState) -> FastAPI:
    app = FastAPI(title="Tunecast", docs_url=None, redoc_url=None)
    key_bytes = settings.api_key.encode("utf-8")

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
    def submit(req: JobRequest):
        params = req.model_dump()
        if params["seed"] is None:
            params["seed"] = secrets.randbelow(MAX_SEED + 1)
        job = store.create(params)
        runner.submit(job.id)
        return JSONResponse(status_code=202, content=job_to_dict(job, store, runner.estimator))

    @app.get("/jobs", dependencies=auth)
    def list_jobs(limit: int = Query(default=50, ge=1, le=200)):
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

    @app.delete("/jobs/{job_id}", dependencies=auth, status_code=204)
    def delete_job(job_id: str):
        load_job(job_id)
        if not store.delete(job_id):
            raise HTTPException(status_code=409, detail="job is running")
        return Response(status_code=204)

    @app.get("/", response_class=HTMLResponse)
    def ui():
        return HTMLResponse("<!doctype html><title>Tunecast</title><h1>Tunecast</h1><p>UI arrives in Task 4.</p>")

    return app
