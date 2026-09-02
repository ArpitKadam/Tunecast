import logging
import time
import wave
from io import BytesIO
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tunecast.app import ReadyState, create_app
from tunecast.config import load_settings
from tunecast.jobs import Estimator, JobRunner, JobStore
from tunecast.sidecar import GenerateParams, SidecarError, StubSidecar

KEY = "test-key"
AUTH = {"Authorization": f"Bearer {KEY}"}
BODY = {"lyrics": "[Verse]\nla la la", "description": "warm acoustic pop", "duration_s": 2, "seed": 7}


class FailingSidecar:
    def ready(self) -> bool:
        return True

    def generate(self, params: GenerateParams, out_path: Path) -> None:
        raise SidecarError("sidecar HTTP 500: CUDA out of memory")

    def describe(self) -> dict:
        return {"kind": "failing"}


def build(tmp_path, sidecar=None, delay_s=0.3):
    settings = load_settings({
        "TUNECAST_API_KEY": KEY,
        "NGROK_ENABLED": "0",
        "TUNECAST_STUB": "1",
        "TUNECAST_DATA_DIR": str(tmp_path),
    })
    sidecar = sidecar or StubSidecar(delay_s=delay_s)
    store = JobStore(settings.db_path, settings.outputs_dir)
    estimator = Estimator()
    runner = JobRunner(store, sidecar, settings.max_concurrent, estimator, settings.keep_last, logging.getLogger("test"))
    runner.start()
    state = ReadyState()
    app = create_app(settings, store, runner, sidecar, state)
    return TestClient(app), state, store


@pytest.fixture
def client(tmp_path):
    c, _, _ = build(tmp_path)
    return c


def wait_for(client, job_id, status, timeout_s=10.0):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        job = client.get(f"/jobs/{job_id}", headers=AUTH).json()
        if job["status"] == status:
            return job
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} never reached {status}: {job}")


def test_health_open(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_ready_503_until_both_flags(tmp_path):
    client, state, _ = build(tmp_path)
    assert client.get("/ready").status_code == 503
    assert client.get("/ready").json() == {"model": False, "tunnel": False}
    state.model = True
    assert client.get("/ready").status_code == 503
    state.tunnel = True
    r = client.get("/ready")
    assert r.status_code == 200
    assert r.json() == {"model": True, "tunnel": True}


def test_jobs_requires_bearer(client):
    r = client.get("/jobs")
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == "Bearer"
    assert client.post("/jobs", json=BODY).status_code == 401
    assert client.get("/info").status_code == 401


def test_wrong_key_401(client):
    assert client.get("/jobs", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.get("/jobs", headers={"Authorization": "Basic dGVzdA=="}).status_code == 401


def test_submit_returns_202_and_shape(client):
    r = client.post("/jobs", json=BODY, headers=AUTH)
    assert r.status_code == 202
    job = r.json()
    assert set(job) == {
        "id", "status", "queue_position", "created_at", "started_at", "finished_at",
        "params", "progress", "timings", "gpu", "audio_url", "error",
    }
    assert job["status"] in ("queued", "running")
    assert job["params"] == {**BODY, "format": "wav"}
    assert job["audio_url"] is None
    assert job["error"] is None
    assert set(job["progress"]) == {"elapsed_s", "estimated_total_s", "fraction"}


def test_duration_out_of_range_422(client):
    assert client.post("/jobs", json={**BODY, "duration_s": 0}, headers=AUTH).status_code == 422
    assert client.post("/jobs", json={**BODY, "duration_s": 361}, headers=AUTH).status_code == 422
    assert client.post("/jobs", json={**BODY, "lyrics": ""}, headers=AUTH).status_code == 422
    assert client.post("/jobs", json={**BODY, "format": "mp3"}, headers=AUTH).status_code == 422


def test_seed_random_when_omitted(client):
    body = {k: v for k, v in BODY.items() if k != "seed"}
    a = client.post("/jobs", json=body, headers=AUTH).json()["params"]["seed"]
    b = client.post("/jobs", json=body, headers=AUTH).json()["params"]["seed"]
    assert isinstance(a, int) and 0 <= a < 2**31
    assert isinstance(b, int)
    assert a != b


def test_job_completes_and_audio_is_wav(client):
    job_id = client.post("/jobs", json=BODY, headers=AUTH).json()["id"]
    job = wait_for(client, job_id, "succeeded")
    assert job["audio_url"] == f"/jobs/{job_id}/audio"
    assert job["progress"]["fraction"] == 1.0
    assert job["timings"]["inference_s"] >= 0
    assert job["started_at"] and job["finished_at"]

    r = client.get(job["audio_url"], headers=AUTH)
    assert r.status_code == 200
    assert r.headers["content-type"] == "audio/wav"
    assert "attachment" in r.headers["content-disposition"]
    with wave.open(BytesIO(r.content), "rb") as w:
        assert w.getnframes() == BODY["duration_s"] * 32000


def test_audio_404_unknown_and_409_before_done(client):
    assert client.get("/jobs/nope/audio", headers=AUTH).status_code == 404
    job_id = client.post("/jobs", json={**BODY, "duration_s": 30}, headers=AUTH).json()["id"]
    assert client.get(f"/jobs/{job_id}/audio", headers=AUTH).status_code == 409


def test_second_job_has_queue_position_1(tmp_path):
    client, _, _ = build(tmp_path, delay_s=1.0)
    first = client.post("/jobs", json=BODY, headers=AUTH).json()["id"]
    second = client.post("/jobs", json=BODY, headers=AUTH).json()
    assert second["status"] == "queued"
    assert second["queue_position"] == 1
    running = wait_for(client, first, "running", timeout_s=3)
    assert running["queue_position"] == 0
    assert 0 <= running["progress"]["fraction"] <= 0.95


def test_list_newest_first_and_limit(client):
    ids = [client.post("/jobs", json=BODY, headers=AUTH).json()["id"] for _ in range(3)]
    listed = client.get("/jobs", headers=AUTH).json()
    assert [j["id"] for j in listed] == ids[::-1]
    assert len(client.get("/jobs?limit=2", headers=AUTH).json()) == 2
    assert client.get("/jobs?limit=0", headers=AUTH).status_code == 422


def test_failed_job_has_error(tmp_path):
    client, _, _ = build(tmp_path, sidecar=FailingSidecar())
    job_id = client.post("/jobs", json=BODY, headers=AUTH).json()["id"]
    job = wait_for(client, job_id, "failed")
    assert "CUDA out of memory" in job["error"]
    assert job["audio_url"] is None
    assert client.get(f"/jobs/{job_id}/audio", headers=AUTH).status_code == 409


def test_delete_running_409_and_delete_done_204(tmp_path):
    client, _, store = build(tmp_path, delay_s=1.0)
    job_id = client.post("/jobs", json=BODY, headers=AUTH).json()["id"]
    wait_for(client, job_id, "running", timeout_s=3)
    assert client.delete(f"/jobs/{job_id}", headers=AUTH).status_code == 409
    wait_for(client, job_id, "succeeded")
    assert store.audio_path(job_id).exists()
    assert client.delete(f"/jobs/{job_id}", headers=AUTH).status_code == 204
    assert client.get(f"/jobs/{job_id}", headers=AUTH).status_code == 404
    assert not store.audio_path(job_id).exists()
    assert client.delete("/jobs/nope", headers=AUTH).status_code == 404


def test_info_has_model_and_gpu_list(client):
    info = client.get("/info", headers=AUTH).json()
    assert info["model_id"] == "MiniMaxAI/MiniMax-Music3"
    assert info["model_revision"].startswith("fbdf52fb")
    assert info["stub"] is True
    assert info["sidecar"]["kind"] == "stub"
    assert isinstance(info["gpus"], list)
    assert info["limits"]["max_duration_s"] == 360
    assert info["max_concurrent"] == 1


def test_root_serves_ui_with_attribution_and_warning(client):
    r = client.get("/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    body = r.text
    assert "MiniMax-Music3" in body
    assert "Save WAV" in body
    assert "deleted when this pod terminates" in body
    assert "ngrok-skip-browser-warning" in body
    assert "<script src=" not in body and "<link" not in body   # no external assets over the tunnel
