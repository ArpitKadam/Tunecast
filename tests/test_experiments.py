import csv
import io
import json
import logging
import sqlite3
import threading
import time

import pytest
from fastapi.testclient import TestClient

from tunecast import jobs as jobs_module
from tunecast.app import ReadyState, create_app
from tunecast.config import load_settings
from tunecast.gpu import GpuPeakSampler
from tunecast.jobs import (
    EXPORT_FIELDS, DuplicateRequest, Estimator, Job, JobRunner, JobStore, UnknownParent, export_row, fingerprint, real_time_factor,
)
from tunecast.sidecar import SidecarError, StubSidecar

KEY = "test-key-s3cr3t"
NGROK_TOKEN = "ngrok-token-xyz-123"
HF_TOKEN = "hf_tokenvalue_456"
AUTH = {"Authorization": f"Bearer {KEY}"}
BODY = {"lyrics": "[Verse]\nla la la", "description": "warm acoustic pop", "duration_s": 2, "seed": 7}
PARAMS = {**BODY, "format": "wav"}
REVISION = "abc123"


def build(tmp_path, sidecar=None):
    settings = load_settings({
        "TUNECAST_API_KEY": KEY,
        "NGROK_ENABLED": "1",
        "NGROK_AUTHTOKEN": NGROK_TOKEN,
        "NGROK_DOMAIN": "tunecast.example.ngrok.app",
        "HF_TOKEN": HF_TOKEN,
        "TUNECAST_STUB": "1",
        "TUNECAST_DATA_DIR": str(tmp_path),
        "TUNECAST_MODEL_REVISION": REVISION,
    })
    sidecar = sidecar or StubSidecar(delay_s=0.0)
    store = JobStore(settings.db_path, settings.outputs_dir)
    runner = JobRunner(store, sidecar, settings.max_concurrent, Estimator(), settings.keep_last, logging.getLogger("test"))
    runner.start()
    return TestClient(create_app(settings, store, runner, sidecar, ReadyState())), store


@pytest.fixture
def api(tmp_path):
    return build(tmp_path)


def wait_done(client, job_id, timeout_s=10.0):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        job = client.get(f"/jobs/{job_id}", headers=AUTH).json()
        if job["status"] in ("succeeded", "failed"):
            return job
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} never finished: {job}")


def post(client, **extra):
    r = client.post("/jobs", json={**BODY, **extra}, headers=AUTH)
    assert r.status_code == 202, r.text
    return r.json()


# --- store: lineage columns -------------------------------------------------------------------

def test_lineage_columns_roundtrip(tmp_path):
    store = JobStore(tmp_path / "jobs.db", tmp_path / "outputs")
    root = store.create(PARAMS, model_revision=REVISION, config_snapshot={"max_concurrent": 1, "gpu_names": ["L40S"]})
    child = store.create(PARAMS, parent_job_id=root.id, experiment_id="exp-1")
    got = JobStore(tmp_path / "jobs.db", tmp_path / "outputs").get(root.id)
    assert got.model_revision == REVISION
    assert got.config_snapshot == {"max_concurrent": 1, "gpu_names": ["L40S"]}
    assert got.parent_job_id is None
    assert store.get(child.id).parent_job_id == root.id
    assert store.get(child.id).experiment_id == "exp-1"


def test_root_job_starts_its_own_experiment_and_children_inherit_it(tmp_path):
    store = JobStore(tmp_path / "jobs.db", tmp_path / "outputs")
    root = store.create(PARAMS)
    child = store.create(PARAMS, parent_job_id=root.id)
    grandchild = store.create(PARAMS, parent_job_id=child.id)
    assert root.experiment_id == root.id
    assert child.experiment_id == root.id
    assert grandchild.experiment_id == root.id
    assert [j.id for j in store.by_experiment(root.id)] == [root.id, child.id, grandchild.id]


def test_explicit_experiment_wins_over_the_parents(tmp_path):
    store = JobStore(tmp_path / "jobs.db", tmp_path / "outputs")
    root = store.create(PARAMS, experiment_id="mine")
    other = store.create(PARAMS, parent_job_id=root.id, experiment_id="elsewhere")
    assert root.experiment_id == "mine"
    assert other.experiment_id == "elsewhere"


def test_unknown_parent_is_rejected_and_nothing_is_stored(tmp_path):
    store = JobStore(tmp_path / "jobs.db", tmp_path / "outputs")
    with pytest.raises(UnknownParent):
        store.create(PARAMS, parent_job_id="nope")
    assert store.list() == []


def test_replay_wins_over_a_parent_that_was_deleted_since(tmp_path):
    store = JobStore(tmp_path / "jobs.db", tmp_path / "outputs")
    parent = store.create(PARAMS)
    first = store.create(PARAMS, "req-0123456789", "h", parent_job_id=parent.id)
    store.delete(parent.id)
    with pytest.raises(DuplicateRequest) as dup:
        store.create(PARAMS, "req-0123456789", "h", parent_job_id=parent.id)
    assert dup.value.job.id == first.id


def write_old_schema_db(db):
    old = sqlite3.connect(db)
    old.execute(
        "CREATE TABLE jobs (seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE, status TEXT NOT NULL, "
        "created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT, params TEXT NOT NULL, "
        "timings TEXT NOT NULL DEFAULT '{}', gpu TEXT NOT NULL DEFAULT '[]', error TEXT, "
        "audio_duration_s REAL, audio_sample_rate INTEGER, audio_channels INTEGER, audio_bytes INTEGER, "
        "request_id TEXT, request_hash TEXT)"
    )
    old.execute("INSERT INTO jobs (id, status, created_at, params) VALUES ('old1', 'succeeded', '2026-01-01T00:00:00.000Z', ?)", (json.dumps(PARAMS),))
    old.commit()
    old.close()


def test_old_schema_db_gets_lineage_columns(tmp_path):
    db = tmp_path / "jobs.db"
    write_old_schema_db(db)

    store = JobStore(db, tmp_path / "outputs")
    got = store.get("old1")
    assert (got.parent_job_id, got.experiment_id, got.model_revision, got.config_snapshot) == (None, "old1", None, None)
    child = store.create(PARAMS, parent_job_id="old1")
    assert child.experiment_id == "old1"        # the backfilled row is the experiment root the child joins
    JobStore(db, tmp_path / "outputs")          # reopening a migrated DB is a no-op


def test_old_rows_are_backfilled_as_their_own_experiment_root(tmp_path):
    db = tmp_path / "jobs.db"
    write_old_schema_db(db)
    JobStore(db, tmp_path / "outputs")
    assert JobStore(db, tmp_path / "outputs").get("old1").experiment_id == "old1"


# --- API: lineage, snapshot, idempotency ------------------------------------------------------

def test_submit_records_lineage_revision_and_snapshot(api):
    client, _ = api
    root = post(client)
    child = post(client, parent_job_id=root["id"])
    assert root["experiment_id"] == root["id"]
    assert root["parent_job_id"] is None
    assert child["parent_job_id"] == root["id"]
    assert child["experiment_id"] == root["id"]
    assert child["model_revision"] == REVISION
    snap = child["config_snapshot"]
    assert snap["stub"] is True and snap["max_concurrent"] == 1 and snap["sidecar"]["kind"] == "stub"
    assert {"app_version", "keep_last", "gpu_count", "gpu_names", "model_id"} <= set(snap)
    assert client.get(f"/jobs/{child['id']}", headers=AUTH).json()["config_snapshot"] == snap
    assert "parent_job_id" not in child["params"] and "experiment_id" not in child["params"]


def test_snapshot_and_responses_never_contain_secrets(api):
    client, store = api
    job = post(client)
    wait_done(client, job["id"])
    everything = json.dumps(client.get("/jobs", headers=AUTH).json()) + client.get(f"/experiments/{job['id']}/export?format=csv", headers=AUTH).text
    stored = json.dumps(store.get(job["id"]).config_snapshot)
    for secret in (KEY, NGROK_TOKEN, HF_TOKEN):
        assert secret not in everything
        assert secret not in stored


def test_unknown_parent_is_422_with_a_clear_message(api):
    client, store = api
    r = client.post("/jobs", json={**BODY, "parent_job_id": "20260101-000000-abcdef"}, headers=AUTH)
    assert r.status_code == 422
    assert "parent_job_id 20260101-000000-abcdef does not exist" in r.json()["detail"]
    assert store.list() == []


def test_malformed_lineage_ids_are_422(api):
    client, _ = api
    assert client.post("/jobs", json={**BODY, "experiment_id": "has space"}, headers=AUTH).status_code == 422
    assert client.post("/jobs", json={**BODY, "parent_job_id": "x" * 65}, headers=AUTH).status_code == 422


def test_explicit_experiment_on_a_root_job(api):
    client, _ = api
    job = post(client, experiment_id="take-study")
    assert job["experiment_id"] == "take-study"
    assert post(client, parent_job_id=job["id"])["experiment_id"] == "take-study"


def test_idempotent_replay_with_lineage_returns_the_same_job(api):
    client, store = api
    root = post(client)
    headers = {**AUTH, "Idempotency-Key": "lineage-key-0001"}
    body = {**BODY, "parent_job_id": root["id"], "seed": 99}
    first = client.post("/jobs", json=body, headers=headers)
    again = client.post("/jobs", json=body, headers=headers)
    assert (first.status_code, again.status_code) == (202, 200)
    assert again.json()["id"] == first.json()["id"]
    assert len(store.list()) == 2


def test_same_key_with_a_different_parent_or_experiment_is_409(api):
    client, _ = api
    a, b = post(client), post(client)
    headers = {**AUTH, "Idempotency-Key": "lineage-key-0002"}
    assert client.post("/jobs", json={**BODY, "parent_job_id": a["id"]}, headers=headers).status_code == 202
    assert client.post("/jobs", json={**BODY, "parent_job_id": b["id"]}, headers=headers).status_code == 409
    assert client.post("/jobs", json={**BODY, "parent_job_id": a["id"], "experiment_id": "other"}, headers=headers).status_code == 409
    assert client.post("/jobs", json=BODY, headers=headers).status_code == 409


def test_replay_still_works_after_the_parent_is_deleted(api):
    client, _ = api
    root = post(client)
    wait_done(client, root["id"])
    headers = {**AUTH, "Idempotency-Key": "lineage-key-0003"}
    body = {**BODY, "parent_job_id": root["id"]}
    first = client.post("/jobs", json=body, headers=headers)
    assert client.delete(f"/jobs/{root['id']}", headers=AUTH).status_code == 204
    again = client.post("/jobs", json=body, headers=headers)
    assert again.status_code == 200 and again.json()["id"] == first.json()["id"]
    assert client.post("/jobs", json=body, headers=AUTH).status_code == 422      # without the key it is a new job with a dead parent


def test_requests_without_lineage_keep_their_old_fingerprint(api):
    client, store = api
    headers = {**AUTH, "Idempotency-Key": "lineage-key-0004"}
    job = client.post("/jobs", json=BODY, headers=headers).json()
    assert store.get(job["id"]).request_hash == fingerprint(PARAMS)


# --- timings and real-time factor ---------------------------------------------------------------

def make_job(timings, audio_duration_s):
    return Job(id="j", status="succeeded", created_at="2026-01-01T00:00:00.000Z", timings=timings, audio_duration_s=audio_duration_s)


def test_real_time_factor_math_and_nulls():
    assert real_time_factor(make_job({"inference_s": 30.0}, 60.0)) == 0.5
    assert real_time_factor(make_job({"inference_s": 90.0}, 60.0)) == 1.5
    assert real_time_factor(make_job({"inference_s": 1.0}, 3.0)) == 0.333
    assert real_time_factor(make_job({}, 60.0)) is None
    assert real_time_factor(make_job({"inference_s": 30.0}, None)) is None
    assert real_time_factor(make_job({"inference_s": 30.0}, 0.0)) is None


def test_succeeded_job_reports_queue_inference_and_rtf(api):
    client, _ = api
    job = wait_done(client, post(client)["id"])
    t = job["timings"]
    assert t["queue_wait_s"] >= 0 and t["inference_s"] >= 0
    assert job["real_time_factor"] == round(t["inference_s"] / job["audio_duration_s"], 3)


def test_failed_job_has_no_rtf(tmp_path):
    class Failing:
        def ready(self): return True
        def describe(self): return {"kind": "failing"}
        def generate(self, params, out_path): raise SidecarError("boom")

    client, _ = build(tmp_path, Failing())
    job = wait_done(client, post(client)["id"])
    assert job["status"] == "failed"
    assert job["real_time_factor"] is None
    assert job["timings"]["inference_s"] >= 0


# --- estimator --------------------------------------------------------------------------------

def test_estimator_learns_from_actual_audio_not_the_requested_ceiling():
    est = Estimator()
    for _ in range(3):
        est.observe(10.0, 5.0, ceiling_s=100, lyrics_chars=100)     # asked for 100 s, got 10 s: 0.5 s of inference per audio second
    seconds, confidence = est.estimate(100, lyrics_chars=200)
    assert seconds == pytest.approx(10.0)       # 200 chars at 10 chars/s = 20 s of audio * 0.5
    assert confidence == "medium"
    assert est.estimate(100, lyrics_chars=5000)[0] == pytest.approx(50.0)   # capped at the ceiling


def test_estimator_is_low_confidence_until_it_has_data():
    est = Estimator()
    assert est.estimate(100) == (pytest.approx(60.0), "low")
    est.observe(10.0, 5.0)
    est.observe(10.0, 5.0)
    assert est.estimate(100)[1] == "low"
    est.observe(10.0, 5.0)
    assert est.estimate(100)[1] == "medium"


def test_estimator_stays_low_when_length_is_unpredictable():
    est = Estimator()
    est.observe(100.0, 50.0, ceiling_s=100, lyrics_chars=500)      # filled the ceiling
    est.observe(100.0, 50.0, ceiling_s=100, lyrics_chars=500)
    est.observe(20.0, 10.0, ceiling_s=100, lyrics_chars=100)       # one early finish: too few to learn from
    seconds, confidence = est.estimate(100, lyrics_chars=100)
    assert confidence == "low"
    assert seconds == pytest.approx(50.0)       # falls back to the ceiling


def test_estimator_ignores_runs_without_audio():
    est = Estimator()
    est.observe(0.0, 5.0)
    assert est.estimate(10) == (pytest.approx(6.0), "low")


def test_api_estimate_has_seconds_and_confidence_while_live_and_none_after(tmp_path):
    client, store = build(tmp_path)
    queued = store.create(PARAMS)       # never submitted to the runner, so it stays queued
    est = client.get(f"/jobs/{queued.id}", headers=AUTH).json()
    assert est["estimate"] == {"seconds": pytest.approx(1.2), "confidence": "low"}
    assert est["progress"]["estimated_total_s"] == pytest.approx(1.2)
    done = wait_done(client, post(client)["id"])
    assert done["estimate"] is None
    assert done["progress"]["fraction"] == 1.0


# --- peak GPU sampler -------------------------------------------------------------------------

def gpu(index, used_mb):
    return {"index": index, "name": "L40S", "used_mb": used_mb, "total_mb": 46000}


def test_sampler_keeps_the_peak_per_gpu():
    readings = [[gpu(0, 1000), gpu(1, 500)], [gpu(0, 9000), gpu(1, 400)], [gpu(0, 3000), gpu(1, 700)]]
    calls = []

    def query():
        calls.append(1)
        return readings[min(len(calls), len(readings)) - 1]

    sampler = GpuPeakSampler(query, interval_s=0.005)
    sampler.start()
    deadline = time.monotonic() + 5
    while len(calls) < len(readings) + 1 and time.monotonic() < deadline:
        time.sleep(0.005)
    sampler.stop()
    peak = {g["index"]: g["peak_used_mb"] for g in sampler.peak}
    assert peak == {0: 9000, 1: 700}
    assert sampler.peak[0] == {"index": 0, "name": "L40S", "peak_used_mb": 9000, "total_mb": 46000}


def test_sampler_polls_repeatedly_then_stops_and_stays_stopped():
    calls = []
    sampler = GpuPeakSampler(lambda: calls.append(1) or [], interval_s=0.005)
    sampler.start()
    deadline = time.monotonic() + 5
    while len(calls) < 3 and time.monotonic() < deadline:
        time.sleep(0.005)
    sampler.stop()
    assert len(calls) >= 3
    settled = len(calls)
    time.sleep(0.05)
    assert len(calls) == settled
    assert not any(t.name == "tunecast-gpu-sampler" for t in threading.enumerate())
    sampler.stop()      # idempotent
    assert sampler.peak == []


def test_sampler_survives_a_failing_query_and_never_started_stop_is_safe():
    def boom():
        raise RuntimeError("nvidia-smi exploded")

    sampler = GpuPeakSampler(boom, interval_s=0.005)
    sampler.start()
    sampler.stop()
    assert sampler.peak == []
    GpuPeakSampler(lambda: []).stop()


class FakeGpu:
    """query_gpus stand-in: used memory climbs on every call, so the final sample is the peak."""

    def __init__(self):
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return [gpu(0, 1000 * self.calls)]


def test_runner_stores_peak_gpu_from_samples_taken_during_inference(tmp_path, monkeypatch):
    fake = FakeGpu()
    monkeypatch.setattr(jobs_module, "query_gpus", fake)
    store = JobStore(tmp_path / "jobs.db", tmp_path / "outputs")
    runner = JobRunner(store, StubSidecar(delay_s=0.05), 1, Estimator(), 50, logging.getLogger("test"))
    job = store.create(PARAMS)
    runner._run(job.id)
    got = store.get(job.id)
    assert got.status == "succeeded"
    assert fake.calls >= 2
    assert got.gpu == [{"index": 0, "name": "L40S", "peak_used_mb": 1000 * fake.calls, "total_mb": 46000}]
    assert export_row(got)["peak_gpu_mb"] == 1000 * fake.calls


def test_runner_stops_the_sampler_when_the_sidecar_fails_and_keeps_the_peak(tmp_path, monkeypatch):
    fake = FakeGpu()
    monkeypatch.setattr(jobs_module, "query_gpus", fake)

    class OutOfMemory:
        def generate(self, params, out_path):
            time.sleep(0.03)
            raise SidecarError("CUDA out of memory")

    store = JobStore(tmp_path / "jobs.db", tmp_path / "outputs")
    runner = JobRunner(store, OutOfMemory(), 1, Estimator(), 50, logging.getLogger("test"))
    job = store.create(PARAMS)
    runner._run(job.id)
    got = store.get(job.id)
    assert got.status == "failed"
    assert got.gpu and got.gpu[0]["peak_used_mb"] >= 1000
    settled = fake.calls
    time.sleep(0.05)
    assert fake.calls == settled
    assert not any(t.name == "tunecast-gpu-sampler" for t in threading.enumerate())


def test_no_gpu_means_empty_peak_and_null_in_export(api, monkeypatch):
    monkeypatch.setattr(jobs_module, "query_gpus", lambda: [])
    client, _ = api
    job = wait_done(client, post(client)["id"])
    assert job["gpu"] == []
    assert export_row(Job(id="j", status="queued", created_at="t", params=PARAMS))["peak_gpu_mb"] is None


# --- export -----------------------------------------------------------------------------------

def test_export_json_lists_the_experiment_with_lineage_and_measurements(api):
    client, _ = api
    root = post(client)
    child = post(client, parent_job_id=root["id"], seed=8)
    post(client, experiment_id="unrelated")
    for j in (root, child):
        wait_done(client, j["id"])
    r = client.get(f"/experiments/{root['id']}/export", headers=AUTH)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    assert r.headers["content-disposition"] == f'attachment; filename="experiment-{root["id"]}.json"'
    rows = r.json()
    assert [row["job_id"] for row in rows] == [root["id"], child["id"]]
    assert all(set(row) == set(EXPORT_FIELDS) for row in rows)
    c = rows[1]
    assert c["parent_job_id"] == root["id"] and c["experiment_id"] == root["id"]
    assert c["seed"] == 8 and c["duration_s_requested"] == 2 and c["audio_duration_s"] == 2.0
    assert c["model_revision"] == REVISION and c["status"] == "succeeded"
    assert c["real_time_factor"] is not None and c["queue_s"] is not None and c["inference_s"] is not None
    assert c["config_snapshot"]["sidecar"]["kind"] == "stub"


def test_export_csv_escapes_commas_quotes_and_newlines(api):
    client, _ = api
    lyrics = 'line one, with comma\n"quoted" line\r\n[Chorus]\nlast, "line"'
    description = 'pop, "bright"\nsecond line'
    job = post(client, lyrics=lyrics, description=description, experiment_id="csv-check")
    wait_done(client, job["id"])
    r = client.get("/experiments/csv-check/export?format=csv", headers=AUTH)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert r.headers["content-disposition"] == 'attachment; filename="experiment-csv-check.csv"'
    rows = list(csv.DictReader(io.StringIO(r.text, newline="")))
    assert len(rows) == 1
    assert tuple(rows[0]) == EXPORT_FIELDS
    assert rows[0]["lyrics"] == lyrics
    assert rows[0]["description"] == description
    assert rows[0]["job_id"] == job["id"]
    assert rows[0]["parent_job_id"] == ""
    assert json.loads(rows[0]["config_snapshot"])["stub"] is True


def test_export_unknown_experiment_404_and_bad_format_422(api):
    client, _ = api
    job = post(client)
    assert client.get("/experiments/nope/export", headers=AUTH).status_code == 404
    assert client.get(f"/experiments/{job['id']}/export?format=xml", headers=AUTH).status_code == 422
    assert client.get("/experiments/bad%20id/export", headers=AUTH).status_code == 422


def test_export_requires_the_api_key(api):
    client, _ = api
    job = post(client)
    assert client.get(f"/experiments/{job['id']}/export").status_code == 401
    assert client.get(f"/experiments/{job['id']}/export?format=xml").status_code == 401


def test_export_row_has_exactly_the_documented_fields():
    assert tuple(export_row(Job(id="j", status="queued", created_at="t", params=PARAMS))) == EXPORT_FIELDS
