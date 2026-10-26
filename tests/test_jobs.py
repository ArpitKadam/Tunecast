import json
import logging
import sqlite3
import threading
import wave
from pathlib import Path

import pytest

from tunecast.gpu import parse_nvidia_smi, query_gpus
from tunecast.jobs import DuplicateRequest, Estimator, Job, JobRunner, JobStore, QueueFull, fingerprint, new_job_id
from tunecast.sidecar import GenerateParams, SidecarError, StubSidecar

LOG = logging.getLogger("test")
PARAMS = {"lyrics": "[Verse]\nla la", "description": "warm pop", "duration_s": 2, "seed": 7, "format": "wav"}


@pytest.fixture
def store(tmp_path):
    return JobStore(tmp_path / "jobs.db", tmp_path / "outputs")


def test_new_job_id_shape():
    a, b = new_job_id(), new_job_id()
    assert len(a) == 8 + 1 + 6 + 1 + 6
    assert a != b


def test_store_roundtrip(store):
    job = store.create(PARAMS)
    assert job.status == "queued"
    assert job.params == PARAMS
    assert job.created_at.endswith("Z")
    got = store.get(job.id)
    assert isinstance(got, Job)
    assert got == job
    assert store.get("missing") is None


def test_update_and_list_newest_first(store):
    first = store.create(PARAMS)
    second = store.create(PARAMS)
    store.update(first.id, status="failed", error="boom", timings={"inference_s": 1.5})
    listed = store.list(limit=10)
    assert [j.id for j in listed] == [second.id, first.id]
    assert listed[1].error == "boom"
    assert listed[1].timings == {"inference_s": 1.5}
    assert len(store.list(limit=1)) == 1


def test_queued_ids_in_creation_order(store):
    ids = [store.create(PARAMS).id for _ in range(3)]
    store.update(ids[0], status="running")
    assert store.queued_ids() == ids[1:]


def test_ahead_of_counts_running_and_earlier_queued(store):
    ids = [store.create(PARAMS).id for _ in range(4)]
    store.update(ids[0], status="succeeded")
    store.update(ids[1], status="running")
    assert store.ahead_of(ids[1]) == 0
    assert store.ahead_of(ids[2]) == 1
    assert store.ahead_of(ids[3]) == 2
    assert store.ahead_of("missing") == 0


def test_mark_stale_failed_survives_reopen(store, tmp_path):
    running = store.create(PARAMS)
    queued = store.create(PARAMS)
    done = store.create(PARAMS)
    store.update(running.id, status="running")
    store.update(done.id, status="succeeded")

    reopened = JobStore(tmp_path / "jobs.db", tmp_path / "outputs")
    assert reopened.mark_stale_failed() == 2
    assert reopened.get(running.id).status == "failed"
    assert reopened.get(running.id).error == "pod restarted"
    assert reopened.get(queued.id).status == "failed"
    assert reopened.get(done.id).status == "succeeded"


def test_delete_removes_row_and_file(store):
    job = store.create(PARAMS)
    path = store.audio_path(job.id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    assert store.delete(job.id) is True
    assert store.get(job.id) is None
    assert not path.exists()


def test_delete_refuses_running_and_unknown(store):
    job = store.create(PARAMS)
    store.update(job.id, status="running")
    assert store.delete(job.id) is False
    assert store.get(job.id) is not None
    assert store.delete("missing") is False


def test_update_reports_missing_row(store):
    job = store.create(PARAMS)
    assert store.update(job.id, status="failed") == 1
    assert store.update("missing", status="failed") == 0


def test_prune_keeps_last_n_succeeded_and_deletes_files(store):
    ids = []
    for _ in range(4):
        job = store.create(PARAMS)
        store.update(job.id, status="succeeded")
        p = store.audio_path(job.id)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x")
        ids.append(job.id)
    failed = store.create(PARAMS)
    store.update(failed.id, status="failed", error="e")

    deleted = store.prune(keep_last=2)

    assert sorted(deleted) == sorted(ids[:2])
    assert all(store.get(i) is None for i in ids[:2])
    assert all(not store.audio_path(i).exists() for i in ids[:2])
    assert all(store.get(i) is not None for i in ids[2:])
    assert store.get(failed.id) is not None


def test_estimator_seed_then_median():
    est = Estimator()
    assert est.estimate(100) == (pytest.approx(60.0), "low")
    est.observe(10, 5.0)   # ratio 0.5
    est.observe(10, 9.0)   # ratio 0.9
    est.observe(10, 7.0)   # ratio 0.7
    assert est.estimate(100) == (pytest.approx(70.0), "medium")


def test_estimator_keeps_only_last_ten():
    est = Estimator()
    for _ in range(10):
        est.observe(10, 100.0)   # ratio 10
    for _ in range(10):
        est.observe(10, 1.0)     # ratio 0.1
    assert est.estimate(10)[0] == pytest.approx(1.0)


def test_generate_params_derived_values():
    p = GenerateParams(lyrics="l", description="d", duration_s=180, seed=1)
    assert p.max_new_tokens == 4500
    assert p.timeout_s == 1920
    assert p.format == "wav"


def test_stub_sidecar_writes_wav_of_requested_length(tmp_path):
    out = tmp_path / "out.wav"
    StubSidecar(delay_s=0.0).generate(GenerateParams(lyrics="l", description="d", duration_s=2, seed=1), out)
    with wave.open(str(out), "rb") as w:
        assert w.getnchannels() == 2
        assert w.getsampwidth() == 2
        assert w.getframerate() == 32000
        assert w.getnframes() == 2 * 32000


def test_stub_sidecar_is_ready_and_describes_itself():
    s = StubSidecar(delay_s=0.0)
    assert s.ready() is True
    assert s.describe()["kind"] == "stub"


def test_sidecar_error_is_exception():
    assert issubclass(SidecarError, Exception)


def test_parse_nvidia_smi_lines():
    text = "0, NVIDIA L40S, 21850, 46068\n1, NVIDIA L40S, 15000, 46068\n"
    assert parse_nvidia_smi(text) == [
        {"index": 0, "name": "NVIDIA L40S", "used_mb": 21850, "total_mb": 46068},
        {"index": 1, "name": "NVIDIA L40S", "used_mb": 15000, "total_mb": 46068},
    ]
    assert parse_nvidia_smi("") == []
    assert parse_nvidia_smi("garbage line\n") == []


def test_query_gpus_returns_list_of_dicts():
    gpus = query_gpus()
    assert isinstance(gpus, list)
    for g in gpus:
        assert {"index", "name", "used_mb", "total_mb"} <= set(g)

OLD_SCHEMA = """
CREATE TABLE jobs (
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


def test_store_migrates_db_from_before_request_ids(tmp_path):
    db = tmp_path / "jobs.db"
    old = sqlite3.connect(db)
    old.execute(OLD_SCHEMA)
    old.execute("INSERT INTO jobs (id, status, created_at, params) VALUES ('old-1', 'succeeded', '2026-01-01T00:00:00.000Z', ?)", (json.dumps(PARAMS),))
    old.commit()
    old.close()

    store = JobStore(db, tmp_path / "outputs")
    assert store.get("old-1").request_id is None
    a = store.create(PARAMS)
    b = store.create(PARAMS)
    assert a.id != b.id
    keyed = store.create(PARAMS, "req-0123456789", "h")
    with pytest.raises(DuplicateRequest) as dup:
        store.create(PARAMS, "req-0123456789", "h")
    assert dup.value.job.id == keyed.id
    JobStore(db, tmp_path / "outputs")      # reopening an already migrated DB is a no-op


def test_create_with_request_id_roundtrip_and_lookup(store):
    job = store.create(PARAMS, "req-0123456789", fingerprint(PARAMS))
    assert store.get_by_request_id("req-0123456789") == job
    assert store.get(job.id) == job
    assert store.get_by_request_id("req-other-key") is None


def test_fingerprint_ignores_key_order_and_sees_changes():
    assert fingerprint({"a": 1, "b": 2}) == fingerprint({"b": 2, "a": 1})
    assert fingerprint(PARAMS) != fingerprint({**PARAMS, "seed": 8})


def test_create_refuses_past_max_queued_but_not_a_replay(store):
    store.create(PARAMS, "req-0123456789", "h", max_queued=2)
    store.create(PARAMS, max_queued=2)
    with pytest.raises(QueueFull):
        store.create(PARAMS, max_queued=2)
    with pytest.raises(DuplicateRequest):
        store.create(PARAMS, "req-0123456789", "h", max_queued=2)
    assert len(store.queued_ids()) == 2


def test_running_jobs_do_not_fill_the_queue(store):
    running = store.create(PARAMS, max_queued=1)
    store.update(running.id, status="running")
    store.create(PARAMS, max_queued=1)
    with pytest.raises(QueueFull):
        store.create(PARAMS, max_queued=1)


def test_concurrent_creates_at_the_boundary_admit_exactly_the_cap(store):
    outcomes = []
    barrier = threading.Barrier(16)

    def attempt():
        barrier.wait()
        try:
            store.create(PARAMS, max_queued=3)
            outcomes.append("ok")
        except QueueFull:
            outcomes.append("full")

    threads = [threading.Thread(target=attempt) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert outcomes.count("ok") == 3 and outcomes.count("full") == 13
    assert len(store.queued_ids()) == 3


class RecordingSidecar(StubSidecar):
    def __init__(self):
        super().__init__(delay_s=0)
        self.generated = []

    def generate(self, params, out_path):
        self.generated.append(params)
        return super().generate(params, out_path)


@pytest.fixture
def runner(store):
    sidecar = RecordingSidecar()
    return JobRunner(store, sidecar, 1, Estimator(), 200, LOG), sidecar


def test_worker_skips_an_id_that_was_cancelled_while_queued(store, runner):
    runner, sidecar = runner
    job = store.create(PARAMS)
    runner.submit(job.id)
    assert store.delete(job.id) is True
    runner.start()
    runner._queue.join()
    assert sidecar.generated == []


def test_worker_skips_a_job_cancelled_between_its_read_and_the_claim(store, runner):
    runner, sidecar = runner
    job = store.create(PARAMS)
    real_get = store.get

    def get_then_cancel(job_id):
        found = real_get(job_id)
        store.delete(job_id)
        return found

    store.get = get_then_cancel
    runner._run(job.id)
    assert sidecar.generated == []
    assert real_get(job.id) is None
