import csv
import hashlib
import io
import logging
import re
import threading
import time
import wave
from io import BytesIO
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from tunecast.app import ReadyState, create_app
from tunecast.config import load_settings
from tunecast.jobs import Estimator, JobRunner, JobStore
from tunecast.sidecar import AudioInfo, GenerateParams, SidecarError, StubSidecar

KEY = "test-key"
AUTH = {"Authorization": f"Bearer {KEY}"}
BODY = {"lyrics": "[Verse]\nla la la", "description": "warm acoustic pop", "duration_s": 2, "seed": 7}


class FailingSidecar:
    def ready(self) -> bool:
        return True

    def generate(self, params: GenerateParams, out_path: Path) -> AudioInfo:
        raise SidecarError("sidecar HTTP 500: CUDA out of memory")

    def describe(self) -> dict:
        return {"kind": "failing"}


def build(tmp_path, sidecar=None, delay_s=0.3, env=None):
    settings = load_settings({
        "TUNECAST_API_KEY": KEY,
        "NGROK_ENABLED": "0",
        "TUNECAST_STUB": "1",
        "TUNECAST_DATA_DIR": str(tmp_path),
        **(env or {}),
    })
    sidecar = sidecar or StubSidecar(delay_s=delay_s)
    store = JobStore(settings.db_path, settings.outputs_dir)
    estimator = Estimator()
    runner = JobRunner(store, sidecar, settings.max_concurrent, estimator, settings.keep_last, logging.getLogger("test"))
    runner.start()
    state = ReadyState(model=True)
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
    state.model = False
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
        "audio_duration_s", "audio_sample_rate", "audio_channels", "audio_bytes", "request_id",
        "estimate", "real_time_factor", "parent_job_id", "experiment_id", "model_revision", "config_snapshot",
        "title", "favorite",
    }
    assert job["status"] in ("queued", "running")
    assert job["params"] == {**BODY, "format": "wav"}
    assert job["audio_url"] is None
    assert job["error"] is None
    assert job["title"] is None and job["favorite"] is False
    assert job["audio_duration_s"] is None
    assert set(job["progress"]) == {"elapsed_s", "estimated_total_s", "fraction"}


def test_succeeded_job_reports_real_audio_fields(client):
    job_id = client.post("/jobs", json=BODY, headers=AUTH).json()["id"]
    job = wait_for(client, job_id, "succeeded")
    audio = client.get(job["audio_url"], headers=AUTH).content
    assert job["audio_duration_s"] == 2.0
    assert job["audio_sample_rate"] == 32000
    assert job["audio_channels"] == 2
    assert job["audio_bytes"] == len(audio)
    assert client.get("/jobs", headers=AUTH).json()[0]["audio_duration_s"] == 2.0


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


ASSET_LINK = re.compile(r'(?:src|href)="([^"]*)"')
LOCAL_ASSET = re.compile(r"/static/app\.(?:js|css)\?v=[0-9a-f]{12}")


def asset_version(client) -> str:
    return re.search(r"/static/app\.js\?v=([0-9a-f]{12})", client.get("/").text).group(1)


def test_root_serves_ui_with_attribution_and_warning(client):
    r = client.get("/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert r.headers["cache-control"] == "no-cache"
    body = r.text
    assert "MiniMax-Music3" in body
    assert "Save WAV" in body
    assert "deleted when this pod terminates" in body
    assert "{{" not in body
    script = client.get("/static/app.js").text
    assert "ngrok-skip-browser-warning" in script
    # only same-origin assets over the tunnel: every link is a fragment or a versioned /static file, no CDN or font host
    links = ASSET_LINK.findall(body)
    assert {"/static/app.js", "/static/app.css"} <= {link.split("?")[0] for link in links}
    assert all(link == "#" or LOCAL_ASSET.fullmatch(link) for link in links), links
    for text in (body, script, client.get("/static/app.css").text):
        assert "://" not in text and "@import" not in text and "url(" not in text


def test_static_assets_are_public_typed_and_cached_by_content_version(client):
    v = asset_version(client)
    js = client.get(f"/static/app.js?v={v}")
    css = client.get(f"/static/app.css?v={v}")
    assert js.status_code == css.status_code == 200
    assert js.headers["content-type"] == "text/javascript; charset=utf-8"
    assert css.headers["content-type"] == "text/css; charset=utf-8"
    assert js.headers["cache-control"] == css.headers["cache-control"] == "public, max-age=31536000, immutable"
    for stale in ("/static/app.js", "/static/app.js?v=000000000000"):
        assert client.get(stale).headers["cache-control"] == "no-cache"
    for text in (js.text, css.text):
        assert KEY not in text and "Bearer test" not in text
    for missing in ("/static/index.html", "/static/nope.js", "/static/..%2Fapp.py", "/static/"):
        assert client.get(missing).status_code == 404, missing


def test_asset_version_is_the_hash_of_the_served_assets(client):
    served = client.get("/static/app.js").content + client.get("/static/app.css").content
    assert asset_version(client) == hashlib.sha256(served).hexdigest()[:12]


def test_labels_work_on_queued_running_and_finished_jobs(gated):
    client, sidecar, _, running = gated
    queued = client.post("/jobs", json=BODY, headers=AUTH).json()["id"]
    r = client.patch(f"/jobs/{running}", json={"title": "  Night drive  "}, headers=AUTH)
    assert r.status_code == 200
    assert r.json()["title"] == "Night drive" and r.json()["favorite"] is False and r.json()["status"] == "running"
    assert client.patch(f"/jobs/{queued}", json={"favorite": True}, headers=AUTH).json()["favorite"] is True
    sidecar.gate.set()
    done = wait_for(client, running, "succeeded")
    assert done["title"] == "Night drive"      # the runner's status updates do not touch labels
    both = client.patch(f"/jobs/{running}", json={"title": "Final", "favorite": True}, headers=AUTH).json()
    assert (both["title"], both["favorite"], both["status"]) == ("Final", True, "succeeded")
    listed = {j["id"]: j for j in client.get("/jobs", headers=AUTH).json()}
    assert (listed[running]["title"], listed[running]["favorite"]) == ("Final", True)
    assert listed[queued]["favorite"] is True and listed[queued]["title"] is None


def test_labels_clear_and_partial_updates(client):
    job_id = client.post("/jobs", json=BODY, headers=AUTH).json()["id"]
    client.patch(f"/jobs/{job_id}", json={"title": "Take one", "favorite": True}, headers=AUTH)
    assert client.patch(f"/jobs/{job_id}", json={"favorite": False}, headers=AUTH).json()["title"] == "Take one"
    assert client.patch(f"/jobs/{job_id}", json={"title": "   "}, headers=AUTH).json()["title"] is None
    client.patch(f"/jobs/{job_id}", json={"title": "again"}, headers=AUTH)
    assert client.patch(f"/jobs/{job_id}", json={"title": None}, headers=AUTH).json()["title"] is None
    unchanged = client.patch(f"/jobs/{job_id}", json={}, headers=AUTH)
    assert unchanged.status_code == 200 and unchanged.json()["favorite"] is False


@pytest.mark.parametrize("bad", [
    {"title": "x" * 121},
    {"title": 5},
    {"favorite": "yes"},
    {"favorite": 1},
    {"favorite": None},
    {"status": "succeeded"},
    {"params": {"seed": 1}},
])
def test_labels_are_validated(client, bad):
    job_id = client.post("/jobs", json=BODY, headers=AUTH).json()["id"]
    assert client.patch(f"/jobs/{job_id}", json=bad, headers=AUTH).status_code == 422
    job = client.get(f"/jobs/{job_id}", headers=AUTH).json()
    assert (job["title"], job["favorite"]) == (None, False)


def test_title_is_trimmed_before_the_length_limit(client):
    job_id = client.post("/jobs", json=BODY, headers=AUTH).json()["id"]
    r = client.patch(f"/jobs/{job_id}", json={"title": "  " + "x" * 120 + "  "}, headers=AUTH)
    assert r.status_code == 200 and r.json()["title"] == "x" * 120
    assert client.get(f"/jobs/{job_id}", headers=AUTH).json()["title"] == "x" * 120
    assert client.patch(f"/jobs/{job_id}", json={"title": "x" * 121}, headers=AUTH).status_code == 422


def test_labels_need_the_key_and_an_existing_job(client):
    job_id = client.post("/jobs", json=BODY, headers=AUTH).json()["id"]
    assert client.patch(f"/jobs/{job_id}", json={"favorite": True}).status_code == 401
    assert client.patch(f"/jobs/{job_id}", json={"favorite": True}, headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.patch(f"/jobs/{job_id}", json={"favorite": "bad"}).status_code == 401
    assert client.patch("/jobs/nope", json={"favorite": True}, headers=AUTH).status_code == 404
    assert client.get(f"/jobs/{job_id}", headers=AUTH).json()["favorite"] is False


def test_labels_survive_an_idempotent_replay(client):
    first = client.post("/jobs", json=BODY, headers=keyed()).json()
    client.patch(f"/jobs/{first['id']}", json={"title": "Keeper", "favorite": True}, headers=AUTH)
    replay = client.post("/jobs", json=BODY, headers=keyed())
    assert replay.status_code == 200
    assert replay.json()["id"] == first["id"]
    assert (replay.json()["title"], replay.json()["favorite"]) == ("Keeper", True)
    assert client.post("/jobs", json={**BODY, "seed": 8}, headers=keyed()).status_code == 409
    assert len(client.get("/jobs", headers=AUTH).json()) == 1


def test_labels_are_in_the_experiment_export(client):
    job_id = client.post("/jobs", json=BODY, headers=AUTH).json()["id"]
    client.patch(f"/jobs/{job_id}", json={"title": "Exported, with comma", "favorite": True}, headers=AUTH)
    rows = client.get(f"/experiments/{job_id}/export", headers=AUTH).json()
    assert (rows[0]["title"], rows[0]["favorite"]) == ("Exported, with comma", True)
    csv_rows = list(csv.DictReader(io.StringIO(client.get(f"/experiments/{job_id}/export?format=csv", headers=AUTH).text)))
    assert (csv_rows[0]["title"], csv_rows[0]["favorite"]) == ("Exported, with comma", "True")


class CountingSidecar(StubSidecar):
    def __init__(self, delay_s=0.0):
        super().__init__(delay_s=delay_s)
        self.calls = 0

    def generate(self, params, out_path):
        self.calls += 1
        return super().generate(params, out_path)


def keyed(key="req-0123456789"):
    return {**AUTH, "Idempotency-Key": key}


def test_same_key_same_body_returns_same_job_and_runs_once(tmp_path):
    sidecar = CountingSidecar(delay_s=0.1)
    client, _, store = build(tmp_path, sidecar=sidecar)
    first = client.post("/jobs", json=BODY, headers=keyed())
    again = client.post("/jobs", json=BODY, headers=keyed())
    assert first.status_code == 202
    assert again.status_code == 200
    assert again.json()["id"] == first.json()["id"]
    assert again.json()["request_id"] == "req-0123456789"
    wait_for(client, first.json()["id"], "succeeded")
    assert len(store.list()) == 1
    assert sidecar.calls == 1


def test_same_key_without_seed_replays_the_same_job_and_seed(client):
    body = {k: v for k, v in BODY.items() if k != "seed"}
    first = client.post("/jobs", json=body, headers=keyed()).json()
    again = client.post("/jobs", json=body, headers=keyed())
    assert again.status_code == 200
    assert again.json()["id"] == first["id"]
    assert again.json()["params"]["seed"] == first["params"]["seed"]


def test_same_key_changed_body_is_409_and_creates_nothing(client):
    first = client.post("/jobs", json=BODY, headers=keyed())
    changed = client.post("/jobs", json={**BODY, "duration_s": 3}, headers=keyed())
    assert changed.status_code == 409
    assert "Idempotency-Key" in changed.json()["detail"]
    assert len(client.get("/jobs", headers=AUTH).json()) == 1
    assert client.get("/jobs", headers=AUTH).json()[0]["id"] == first.json()["id"]


def test_without_key_every_submit_is_a_new_job(client):
    a = client.post("/jobs", json=BODY, headers=AUTH)
    b = client.post("/jobs", json=BODY, headers=AUTH)
    assert a.status_code == b.status_code == 202
    assert a.json()["id"] != b.json()["id"]
    assert a.json()["request_id"] is None


def test_different_keys_are_different_jobs(client):
    a = client.post("/jobs", json=BODY, headers=keyed("req-aaaaaaaaaa"))
    b = client.post("/jobs", json=BODY, headers=keyed("req-bbbbbbbbbb"))
    assert a.json()["id"] != b.json()["id"]


@pytest.mark.parametrize("bad", ["short", "has space in it", "x" * 129, "semi;colon-key"])
def test_malformed_key_is_rejected(client, bad):
    assert client.post("/jobs", json=BODY, headers=keyed(bad)).status_code == 422
    assert client.get("/jobs", params={"request_id": bad}, headers=AUTH).status_code == 422
    assert client.get("/jobs", headers=AUTH).json() == []


def test_concurrent_duplicate_submits_make_one_job(tmp_path):
    sidecar = CountingSidecar(delay_s=0.1)
    client, _, store = build(tmp_path, sidecar=sidecar)
    barrier = threading.Barrier(8)
    codes = []

    def post():
        barrier.wait()
        codes.append(client.post("/jobs", json=BODY, headers=keyed()).status_code)

    threads = [threading.Thread(target=post) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(codes) == [200] * 7 + [202]
    jobs = store.list()
    assert len(jobs) == 1
    wait_for(client, jobs[0].id, "succeeded")
    assert sidecar.calls == 1


def test_lookup_by_request_id(client):
    job = client.post("/jobs", json=BODY, headers=keyed()).json()
    client.post("/jobs", json=BODY, headers=AUTH)
    found = client.get("/jobs", params={"request_id": "req-0123456789"}, headers=AUTH)
    assert found.status_code == 200
    assert [j["id"] for j in found.json()] == [job["id"]]
    missing = client.get("/jobs", params={"request_id": "req-never-used"}, headers=AUTH)
    assert missing.status_code == 200
    assert missing.json() == []


def test_lookup_by_request_id_needs_auth(client):
    assert client.get("/jobs", params={"request_id": "req-0123456789"}).status_code == 401
    assert client.post("/jobs", json=BODY, headers={"Idempotency-Key": "req-0123456789"}).status_code == 401


def test_deleted_job_frees_its_key(client):
    first = client.post("/jobs", json=BODY, headers=keyed()).json()
    wait_for(client, first["id"], "succeeded")
    assert client.delete(f"/jobs/{first['id']}", headers=AUTH).status_code == 204
    again = client.post("/jobs", json=BODY, headers=keyed())
    assert again.status_code == 202
    assert again.json()["id"] != first["id"]


class GateSidecar(StubSidecar):
    """Holds every generation until released, so tests control exactly which jobs are running or queued."""

    def __init__(self):
        super().__init__(delay_s=0)
        self.gate = threading.Event()
        self.calls = 0

    def generate(self, params, out_path):
        self.calls += 1
        self.gate.wait(10)
        return super().generate(params, out_path)


@pytest.fixture
def gated(tmp_path):
    """Cap of 2 queued jobs; the first job is running and held, so each further submit sits in the queue."""
    sidecar = GateSidecar()
    client, _, store = build(tmp_path, sidecar=sidecar, env={"TUNECAST_MAX_QUEUED": "2"})
    running = client.post("/jobs", json=BODY, headers=AUTH).json()["id"]
    wait_for(client, running, "running")
    yield client, sidecar, store, running
    sidecar.gate.set()


def queued_count(client):
    return sum(j["status"] == "queued" for j in client.get("/jobs", headers=AUTH).json())


def test_running_job_does_not_count_toward_the_cap_and_cap_plus_one_is_429(gated):
    client, _, _, _ = gated
    assert [client.post("/jobs", json=BODY, headers=AUTH).status_code for _ in range(2)] == [202, 202]
    before = [j["id"] for j in client.get("/jobs", headers=AUTH).json()]
    rejected = client.post("/jobs", json=BODY, headers=AUTH)
    assert rejected.status_code == 429
    assert int(rejected.headers["retry-after"]) > 0
    assert "queue is full" in rejected.json()["detail"]
    assert [j["id"] for j in client.get("/jobs", headers=AUTH).json()] == before
    assert queued_count(client) == 2


def test_slot_frees_when_a_queued_job_is_cancelled(gated):
    client, _, _, _ = gated
    ids = [client.post("/jobs", json=BODY, headers=AUTH).json()["id"] for _ in range(2)]
    assert client.post("/jobs", json=BODY, headers=AUTH).status_code == 429
    assert client.delete(f"/jobs/{ids[0]}", headers=AUTH).status_code == 204
    assert client.post("/jobs", json=BODY, headers=AUTH).status_code == 202


def test_replay_of_an_existing_key_returns_200_when_the_queue_is_full(gated):
    client, _, _, _ = gated
    first = client.post("/jobs", json=BODY, headers=keyed("req-aaaaaaaaaa"))
    client.post("/jobs", json=BODY, headers=AUTH)
    assert queued_count(client) == 2
    replay = client.post("/jobs", json=BODY, headers=keyed("req-aaaaaaaaaa"))
    assert replay.status_code == 200
    assert replay.json()["id"] == first.json()["id"]
    assert client.post("/jobs", json={**BODY, "seed": 8}, headers=keyed("req-aaaaaaaaaa")).status_code == 409
    assert client.post("/jobs", json=BODY, headers=keyed("req-bbbbbbbbbb")).status_code == 429
    assert queued_count(client) == 2


def test_concurrent_submits_never_exceed_the_cap(gated):
    client, _, _, _ = gated
    barrier = threading.Barrier(12)
    codes = []

    def post():
        barrier.wait()
        codes.append(client.post("/jobs", json=BODY, headers=AUTH).status_code)

    threads = [threading.Thread(target=post) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert codes.count(202) == 2 and codes.count(429) == 10
    assert queued_count(client) == 2


def test_model_not_ready_is_503_with_retry_after_and_creates_nothing(tmp_path):
    client, state, store = build(tmp_path)
    state.model = False
    r = client.post("/jobs", json=BODY, headers=keyed("req-aaaaaaaaaa"))
    assert r.status_code == 503
    assert int(r.headers["retry-after"]) > 0
    assert store.get_by_request_id("req-aaaaaaaaaa") is None
    assert client.get("/jobs", headers=AUTH).json() == []
    state.model = True
    first = client.post("/jobs", json=BODY, headers=keyed("req-aaaaaaaaaa"))
    state.model = False
    replay = client.post("/jobs", json=BODY, headers=keyed("req-aaaaaaaaaa"))
    assert replay.status_code == 200 and replay.json()["id"] == first.json()["id"]


def test_cancelled_queued_job_never_runs(gated):
    client, sidecar, store, running = gated
    queued = client.post("/jobs", json=BODY, headers=AUTH).json()["id"]
    assert client.delete(f"/jobs/{queued}", headers=AUTH).status_code == 204
    assert client.get(f"/jobs/{queued}", headers=AUTH).status_code == 404
    sidecar.gate.set()
    wait_for(client, running, "succeeded")
    time.sleep(0.2)
    assert sidecar.calls == 1
    assert not store.audio_path(queued).exists()


def test_cancelling_a_running_job_is_409_and_explains_why(gated):
    client, sidecar, _, running = gated
    r = client.delete(f"/jobs/{running}", headers=AUTH)
    assert r.status_code == 409
    assert "cannot be cancelled" in r.json()["detail"]
    sidecar.gate.set()
    wait_for(client, running, "succeeded")


def test_only_the_documented_routes_are_public_and_the_schema_is_not_served(client):
    public = {"/health", "/ready", "/", "/static/{name}"}
    checked = 0
    for route in client.app.routes:
        assert isinstance(route, APIRoute), f"{route!r}: a mount or other route needs a deliberate auth decision"
        if route.path in public:
            continue
        url = re.sub(r"\{[^}]+\}", "x", route.path)
        for method in route.methods - {"HEAD", "OPTIONS"}:
            r = client.request(method, url)
            assert r.status_code == 401, (method, route.path, r.status_code)
            checked += 1
    assert checked >= 8
    assert client.get("/openapi.json").status_code == 404
    assert client.get("/docs").status_code == 404
    assert client.get("/redoc").status_code == 404
