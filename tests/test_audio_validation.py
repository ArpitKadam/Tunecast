import json
import logging
import sqlite3
import struct
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path

import pytest

from tunecast.jobs import Estimator, JobRunner, JobStore, job_to_dict
from tunecast.sidecar import GenerateParams, HttpSidecar, SidecarError, StubSidecar, inspect_wav

PARAMS = GenerateParams(lyrics="[Verse]\nhi", description="lofi", duration_s=2, seed=1)
JOB_PARAMS = {"lyrics": "[Verse]\nhi", "description": "lofi", "duration_s": 2, "seed": 1, "format": "wav"}


def chunk(chunk_id: bytes, body: bytes) -> bytes:
    return chunk_id + struct.pack("<I", len(body)) + body + (b"\0" if len(body) % 2 else b"")


def wav(tag=1, channels=2, rate=32000, bits=16, frames=100, pre=b"", extensible_tag=None) -> bytes:
    align = channels * bits // 8
    fmt = struct.pack("<HHIIHH", tag, channels, rate, rate * align, align, bits)
    if extensible_tag is not None:
        guid = struct.pack("<H", extensible_tag) + b"\x00\x00\x00\x00\x10\x00\x80\x00\x00\xaa\x00\x38\x9b\x71"
        fmt = struct.pack("<HHIIHH", 0xFFFE, channels, rate, rate * align, align, bits) + struct.pack("<HHI", 22, bits, 3) + guid
    body = b"WAVE" + chunk(b"fmt ", fmt) + pre + chunk(b"data", b"\x00" * (frames * align))
    return b"RIFF" + struct.pack("<I", len(body)) + body


def test_pcm16_accepted_with_duration():
    info = inspect_wav(wav(frames=64000))
    assert (info.duration_s, info.sample_rate, info.channels) == (2.0, 32000, 2)
    assert info.bytes == len(wav(frames=64000))


def test_float32_accepted():
    info = inspect_wav(wav(tag=3, bits=32, channels=1, rate=48000, frames=24000))
    assert (info.duration_s, info.sample_rate, info.channels) == (0.5, 48000, 1)


def test_extensible_float_and_pcm_accepted():
    assert inspect_wav(wav(bits=32, extensible_tag=3, frames=32000)).duration_s == 1.0
    assert inspect_wav(wav(bits=16, extensible_tag=1, frames=16000)).duration_s == 0.5


def test_skips_odd_sized_chunk_before_data():
    assert inspect_wav(wav(pre=chunk(b"LIST", b"abc"), frames=32000)).duration_s == 1.0


@pytest.mark.parametrize("data, match", [
    (b"", "not a WAV"),
    (json.dumps({"error": "model not loaded"}).encode(), "not a WAV.*model not loaded"),
    (b"\x00\x01\x02\xff" * 50, "not a WAV.*binary"),
    (wav(frames=0), "no audio frames"),
    (wav(frames=100)[:-40], "truncated.*data chunk declares 400 bytes, 360 received"),
    (wav(frames=100)[:30], "fmt chunk is truncated"),
    (wav(tag=2), "not PCM or IEEE float"),
    (wav(rate=0), "inconsistent"),
    (b"RIFF\x04\x00\x00\x00WAVE", "no fmt chunk"),
])
def test_rejects_bad_wav(data, match):
    with pytest.raises(SidecarError, match=match):
        inspect_wav(data)


def test_rejects_partial_frame():
    bad = bytearray(wav(frames=10))
    struct.pack_into("<I", bad, len(bad) - 40 - 4, 39)   # data size field: not a multiple of 4
    with pytest.raises(SidecarError, match="whole number"):
        inspect_wav(bytes(bad))


def test_stub_output_validates_and_is_returned(tmp_path):
    out = tmp_path / "o" / "a.wav"
    info = StubSidecar(delay_s=0.0).generate(PARAMS, out)
    assert (info.duration_s, info.sample_rate, info.channels) == (2.0, 32000, 2)
    assert info.bytes == out.stat().st_size
    assert inspect_wav(out.read_bytes()) == info


class Handler(BaseHTTPRequestHandler):
    mode = "ok"
    good = wav(frames=64000)

    def log_message(self, *args):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        mode = Handler.mode
        if mode == "timeout":
            time.sleep(1.5)
            return
        body = {
            "ok": Handler.good,
            "json": json.dumps({"status": "queued"}).encode(),
            "empty": b"",
            "truncated": Handler.good[: len(Handler.good) // 2],
            "short_wav": Handler.good[:-4000],       # internally truncated, consistent Content-Length
        }[mode]
        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        declared = len(Handler.good) if mode == "truncated" else len(body)
        self.send_header("Content-Length", str(declared))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def server():
    Handler.mode = "ok"
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def run(server, tmp_path):
    out = tmp_path / "out" / "song.wav"
    return out, lambda: HttpSidecar(server, "m").generate(PARAMS, out)


def assert_clean(tmp_path):
    assert [p for p in tmp_path.rglob("*") if p.is_file()] == []


def test_http_valid_response_saved_and_described(server, tmp_path):
    out, go = run(server, tmp_path)
    info = go()
    assert info.duration_s == 2.0
    assert out.read_bytes() == Handler.good
    assert not out.with_suffix(".wav.part").exists()


@pytest.mark.parametrize("mode, match", [
    ("json", "not a WAV.*queued"),
    ("empty", "empty body"),
    ("truncated", "truncated: got \\d+ of \\d+ bytes"),
    ("short_wav", "WAV truncated"),
])
def test_http_bad_200_fails_and_leaves_nothing(server, tmp_path, mode, match):
    Handler.mode = mode
    _, go = run(server, tmp_path)
    with pytest.raises(SidecarError, match=match):
        go()
    assert_clean(tmp_path)


def test_http_timeout_fails_and_leaves_nothing(server, tmp_path, monkeypatch):
    monkeypatch.setattr(GenerateParams, "timeout_s", property(lambda self: 0.3))
    Handler.mode = "timeout"
    _, go = run(server, tmp_path)
    with pytest.raises(SidecarError, match="timed out after 0.3s"):
        go()
    assert_clean(tmp_path)


def test_disk_write_failure_fails_and_leaves_nothing(server, tmp_path, monkeypatch):
    def half_write(self, data):
        with open(self, "wb") as f:
            f.write(data[:10])
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(Path, "write_bytes", half_write)
    _, go = run(server, tmp_path)
    with pytest.raises(SidecarError, match="could not write audio file.*No space"):
        go()
    assert_clean(tmp_path)


class HttpFixture:
    """Runner wired to a real HttpSidecar; polls the store for the terminal state."""

    def __init__(self, server, tmp_path):
        self.store = JobStore(tmp_path / "jobs.db", tmp_path / "outputs")
        self.estimator = Estimator()
        self.runner = JobRunner(self.store, HttpSidecar(server, "m"), 1, self.estimator, 50, logging.getLogger("test"))
        self.runner.start()

    def run_job(self):
        job = self.store.create(JOB_PARAMS)
        self.runner.submit(job.id)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            got = self.store.get(job.id)
            if got.status in ("succeeded", "failed"):
                return got
            time.sleep(0.02)
        raise AssertionError("job never finished")


def test_job_with_json_200_is_failed_not_succeeded(server, tmp_path):
    Handler.mode = "json"
    fx = HttpFixture(server, tmp_path)
    job = fx.run_job()
    assert job.status == "failed"
    assert "not a WAV" in job.error
    assert job.audio_duration_s is None
    assert not fx.store.audio_path(job.id).exists()
    assert not list((tmp_path / "outputs").glob("*"))


def test_succeeded_job_stores_and_returns_real_audio_fields(server, tmp_path):
    fx = HttpFixture(server, tmp_path)
    job = fx.run_job()
    assert job.status == "succeeded"
    assert (job.audio_duration_s, job.audio_sample_rate, job.audio_channels) == (2.0, 32000, 2)
    assert job.audio_bytes == fx.store.audio_path(job.id).stat().st_size
    d = job_to_dict(job, fx.store, fx.estimator)
    assert d["audio_duration_s"] == 2.0
    assert d["audio_sample_rate"] == 32000
    assert d["audio_channels"] == 2
    assert d["audio_bytes"] == job.audio_bytes


def test_failed_job_discards_file_the_sidecar_left_behind(tmp_path):
    class Leaky:
        def generate(self, params, out_path):
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(b"junk")
            raise SidecarError("boom")

    store = JobStore(tmp_path / "jobs.db", tmp_path / "outputs")
    runner = JobRunner(store, Leaky(), 1, Estimator(), 50, logging.getLogger("test"))
    job = store.create(JOB_PARAMS)
    runner._run(job.id)
    assert store.get(job.id).status == "failed"
    assert not store.audio_path(job.id).exists()


def test_old_schema_db_is_migrated_in_place(tmp_path):
    db = tmp_path / "jobs.db"
    old = sqlite3.connect(db)
    old.execute(
        "CREATE TABLE jobs (seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE, status TEXT NOT NULL, "
        "created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT, params TEXT NOT NULL, "
        "timings TEXT NOT NULL DEFAULT '{}', gpu TEXT NOT NULL DEFAULT '[]', error TEXT)"
    )
    old.execute("INSERT INTO jobs (id, status, created_at, params) VALUES ('old1', 'succeeded', '2026-01-01T00:00:00.000Z', ?)", (json.dumps(JOB_PARAMS),))
    old.commit()
    old.close()

    store = JobStore(db, tmp_path / "outputs")
    job = store.get("old1")
    assert job.status == "succeeded"
    assert job.audio_duration_s is None
    store.update("old1", audio_duration_s=12.5, audio_sample_rate=32000, audio_channels=2, audio_bytes=1600044)
    assert store.get("old1").audio_duration_s == 12.5
    JobStore(db, tmp_path / "outputs")   # reopening an already-migrated DB is a no-op
