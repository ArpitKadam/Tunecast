import logging
import wave
from pathlib import Path

import pytest

from tunecast.gpu import parse_nvidia_smi, query_gpus
from tunecast.jobs import Estimator, Job, JobStore, new_job_id
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
    assert est.seconds_for(100) == pytest.approx(60.0)
    est.observe(10, 5.0)   # ratio 0.5
    est.observe(10, 9.0)   # ratio 0.9
    est.observe(10, 7.0)   # ratio 0.7
    assert est.seconds_for(100) == pytest.approx(70.0)


def test_estimator_keeps_only_last_ten():
    est = Estimator()
    for _ in range(10):
        est.observe(10, 100.0)   # ratio 10
    for _ in range(10):
        est.observe(10, 1.0)     # ratio 0.1
    assert est.seconds_for(10) == pytest.approx(1.0)


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
