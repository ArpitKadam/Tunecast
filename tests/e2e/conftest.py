import json
import logging
import socket
import threading
import time
import urllib.request

import pytest
import uvicorn

from tunecast.app import ReadyState, create_app
from tunecast.config import load_settings
from tunecast.jobs import Estimator, JobRunner, JobStore
from tunecast.sidecar import StubSidecar

KEY = "e2e-key"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class HeldSidecar(StubSidecar):
    """Generations block until release() so a test can hold one job running and the rest queued."""

    def __init__(self):
        super().__init__(delay_s=0)
        self._gate = threading.Event()

    def generate(self, params, out_path):
        self._gate.wait(30)
        return super().generate(params, out_path)

    def release(self) -> None:
        self._gate.set()


class LiveServer:
    """The real app on a real socket; stop() and start() simulate the pod dropping off and coming back."""

    def __init__(self, tmp_path, env=None, sidecar=None):
        settings = load_settings({
            "TUNECAST_API_KEY": KEY,
            "NGROK_ENABLED": "0",
            "TUNECAST_STUB": "1",
            "TUNECAST_DATA_DIR": str(tmp_path),
            **(env or {}),
        })
        sidecar = sidecar or StubSidecar(delay_s=0.2)
        store = JobStore(settings.db_path, settings.outputs_dir)
        runner = JobRunner(store, sidecar, settings.max_concurrent, Estimator(), settings.keep_last, logging.getLogger("e2e"))
        runner.start()
        self.state = ReadyState(model=True)
        self.app = create_app(settings, store, runner, sidecar, self.state)
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self._server = None
        self._thread = None

    def start(self) -> None:
        config = uvicorn.Config(self.app, host="127.0.0.1", port=self.port, log_level="warning", timeout_graceful_shutdown=1)
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        deadline = time.monotonic() + 10
        while not self._server.started:
            assert time.monotonic() < deadline, "uvicorn did not start"
            time.sleep(0.02)

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=10)
        assert not self._thread.is_alive(), "uvicorn did not stop"

    def submit(self, **overrides) -> dict:
        body = {"lyrics": "[Verse]\nla la la", "description": "warm acoustic pop", "duration_s": 2, "seed": 7} | overrides
        req = urllib.request.Request(
            self.url + "/jobs",
            data=json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.load(resp)

    def status(self, job_id: str) -> str:
        req = urllib.request.Request(f"{self.url}/jobs/{job_id}", headers={"Authorization": f"Bearer {KEY}"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.load(resp)["status"]


@pytest.fixture
def server(tmp_path):
    srv = LiveServer(tmp_path)
    srv.start()
    yield srv
    if srv._thread.is_alive():
        srv.stop()


@pytest.fixture
def full_queue_server(tmp_path):
    """Cap of 1 queued take: one take running (held) and one queued, so the next submit is refused."""
    sidecar = HeldSidecar()
    srv = LiveServer(tmp_path, env={"TUNECAST_MAX_QUEUED": "1"}, sidecar=sidecar)
    srv.start()
    running = srv.submit()["id"]
    deadline = time.monotonic() + 10
    while srv.status(running) != "running":
        assert time.monotonic() < deadline, "first take never started"
        time.sleep(0.02)
    srv.submit()
    yield srv
    sidecar.release()
    if srv._thread.is_alive():
        srv.stop()
