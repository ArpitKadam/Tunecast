import logging
import json
import socket
import threading
import time
import urllib.request

from tunecast.boot import EXIT_CONFIG, Supervisor, main
from tunecast.config import load_settings


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_main_exits_1_without_api_key(monkeypatch, capsys):
    monkeypatch.delenv("TUNECAST_API_KEY", raising=False)
    assert main() == EXIT_CONFIG
    assert "TUNECAST_API_KEY" in capsys.readouterr().err


def test_stub_boot_reaches_ready_and_serves_a_job(tmp_path):
    port = free_port()
    settings = load_settings({
        "TUNECAST_API_KEY": "k",
        "NGROK_ENABLED": "0",
        "TUNECAST_STUB": "1",
        "TUNECAST_DATA_DIR": str(tmp_path),
        "TUNECAST_PORT": str(port),
    })
    sup = Supervisor(settings, host="127.0.0.1")
    results = []
    thread = threading.Thread(target=lambda: results.append(sup.run()), daemon=True)
    thread.start()

    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 20
    ready = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(base + "/ready", timeout=1) as r:
                ready = json.loads(r.read())
                break
        except Exception:
            time.sleep(0.1)
    assert ready == {"model": True, "tunnel": True}

    req = urllib.request.Request(
        base + "/jobs",
        data=json.dumps({"lyrics": "la", "description": "d", "duration_s": 1}).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer k"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        assert r.status == 202

    sup.request_stop()
    thread.join(timeout=15)
    assert results == [0]

    boot_log = (tmp_path / "tunecast" / "logs" / "boot.jsonl").read_text(encoding="utf-8")
    events = [json.loads(line)["event"] for line in boot_log.splitlines()]
    assert "env_ok" in events
    assert "api_ready" in events
    assert "shutdown" in events
    assert (tmp_path / "tunecast" / "logs" / "jobs.jsonl").exists()


def test_sidecar_command_is_official_serve_with_optional_dit_flags(tmp_path):
    from tunecast.boot import build_sidecar_command

    base = load_settings({"TUNECAST_API_KEY": "k", "NGROK_ENABLED": "0", "TUNECAST_DATA_DIR": str(tmp_path)})
    cmd = build_sidecar_command(base)
    assert cmd[:2] == ["sgl-omni", "serve"]
    assert cmd[cmd.index("--model-path") + 1] == str(base.models_dir)
    assert cmd[cmd.index("--host") + 1] == "127.0.0.1"
    assert cmd[cmd.index("--port") + 1] == "8000"
    assert "--dit_dav.factory.dit_steps" not in cmd

    tuned = load_settings({
        "TUNECAST_API_KEY": "k", "NGROK_ENABLED": "0", "TUNECAST_DATA_DIR": str(tmp_path),
        "TUNECAST_DIT_STEPS": "20", "TUNECAST_DIT_CFG_SCALE": "1.5",
    })
    cmd = build_sidecar_command(tuned)
    assert cmd[cmd.index("--dit_dav.factory.dit_steps") + 1] == "20"
    assert cmd[cmd.index("--dit_dav.factory.dit_cfg_scale") + 1] == "1.5"


def test_boot_exits_6_when_api_port_is_taken(tmp_path):
    from tunecast.boot import EXIT_API_BIND

    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    port = blocker.getsockname()[1]
    try:
        settings = load_settings({
            "TUNECAST_API_KEY": "k", "NGROK_ENABLED": "0", "TUNECAST_STUB": "1",
            "TUNECAST_DATA_DIR": str(tmp_path), "TUNECAST_PORT": str(port),
        })
        assert Supervisor(settings, host="127.0.0.1").run() == EXIT_API_BIND
    finally:
        blocker.close()
    boot_log = (tmp_path / "tunecast" / "logs" / "boot.jsonl").read_text(encoding="utf-8")
    events = [json.loads(line)["event"] for line in boot_log.splitlines()]
    assert "api_bind_failed" in events
    assert "api_ready" not in events


def test_sidecar_wait_ready_returns_early_when_stop_requested():
    import sys

    from tunecast.boot import SidecarProcess
    from tunecast.sidecar import HttpSidecar

    import os

    proc = SidecarProcess.__new__(SidecarProcess)
    proc.command = [sys.executable, "-c", "import time; time.sleep(60)"]
    proc.env = dict(os.environ)
    proc.proc = None
    proc.start()
    stop = threading.Event()
    threading.Timer(0.3, stop.set).start()
    started = time.monotonic()
    try:
        assert proc.wait_ready(HttpSidecar("http://127.0.0.1:9", "m"), timeout_s=60, stop_event=stop) is False
    finally:
        proc.stop()
    assert time.monotonic() - started < 10


def test_supervisor_stops_cleanly_when_stop_requested_during_boot(tmp_path, monkeypatch):
    """A SIGTERM during the sidecar wait must end boot promptly with exit 0, not after 30 minutes."""
    import sys

    from tunecast import boot

    monkeypatch.setattr(boot, "ensure_weights", lambda settings, logger: 0.0)
    monkeypatch.setattr(boot, "build_sidecar_command", lambda settings: [sys.executable, "-c", "import time; time.sleep(60)"])
    settings = load_settings({
        "TUNECAST_API_KEY": "k", "NGROK_ENABLED": "0", "TUNECAST_DATA_DIR": str(tmp_path),
        "TUNECAST_SIDECAR_PORT": str(free_port()),
    })
    sup = Supervisor(settings, host="127.0.0.1")
    threading.Timer(0.5, sup.request_stop).start()
    started = time.monotonic()
    assert sup.run() == 0
    assert time.monotonic() - started < 15
    boot_log = (tmp_path / "tunecast" / "logs" / "boot.jsonl").read_text(encoding="utf-8")
    assert "shutdown_requested" in boot_log


def test_sidecar_env_pins_cuda_visible_devices_to_real_gpu_count(tmp_path, monkeypatch):
    """sglang-omni infers its GPU layout from CUDA_VISIBLE_DEVICES; a host value that disagrees
    with the devices actually present makes it place a stage on a nonexistent ordinal."""
    from tunecast import boot
    from tunecast.boot import SidecarProcess

    settings = load_settings({"TUNECAST_API_KEY": "k", "NGROK_ENABLED": "0", "TUNECAST_DATA_DIR": str(tmp_path)})

    monkeypatch.setattr(boot, "query_gpus", lambda: [{"index": 0}, {"index": 1}])

    # Observed on RunPod: set but empty. sglang-omni reads that as an explicit "zero GPUs"
    # configuration and the CUDA driver exposes no devices at all.
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "")
    assert SidecarProcess(settings, logging.getLogger("test")).env["CUDA_VISIBLE_DEVICES"] == "0,1"

    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "GPU-deadbeef")
    assert SidecarProcess(settings, logging.getLogger("test")).env["CUDA_VISIBLE_DEVICES"] == "0,1"

    monkeypatch.delenv("CUDA_VISIBLE_DEVICES", raising=False)
    assert SidecarProcess(settings, logging.getLogger("test")).env["CUDA_VISIBLE_DEVICES"] == "0,1"

    monkeypatch.setattr(boot, "query_gpus", lambda: [{"index": 0}])
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "0,1")
    assert SidecarProcess(settings, logging.getLogger("test")).env["CUDA_VISIBLE_DEVICES"] == "0"

    monkeypatch.setattr(boot, "query_gpus", lambda: [])
    proc = SidecarProcess(settings, logging.getLogger("test"))
    assert proc.env["CUDA_VISIBLE_DEVICES"] == "0,1"   # nvidia-smi unavailable: leave the host value alone
