import socket
import sys
import threading
import time
import urllib.request
import wave
from pathlib import Path

import pytest

from tunecast.boot import Supervisor
from tunecast.config import load_settings

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "client"))
import generate  # noqa: E402


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def server(tmp_path):
    port = free_port()
    settings = load_settings({
        "TUNECAST_API_KEY": "k",
        "NGROK_ENABLED": "0",
        "TUNECAST_STUB": "1",
        "TUNECAST_DATA_DIR": str(tmp_path / "data"),
        "TUNECAST_PORT": str(port),
    })
    sup = Supervisor(settings, host="127.0.0.1")
    threading.Thread(target=sup.run, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(base + "/ready", timeout=1)
            break
        except Exception:
            time.sleep(0.1)
    yield base
    sup.request_stop()


def test_client_submits_polls_and_saves_wav(server, tmp_path, capsys):
    lyrics = tmp_path / "lyrics.txt"
    lyrics.write_text("[Verse]\nla la\n", encoding="utf-8")
    out = tmp_path / "song.wav"
    code = generate.main([
        "--url", server, "--key", "k", "--lyrics-file", str(lyrics),
        "--description", "lofi", "--duration", "2", "--seed", "5", "--out", str(out), "--poll", "0.2",
    ])
    assert code == 0
    with wave.open(str(out), "rb") as w:
        assert w.getnframes() == 2 * 32000
    printed = capsys.readouterr().out
    assert "succeeded" in printed
    assert "seed 5" in printed


def test_client_wrong_key_exits_2(server, tmp_path):
    lyrics = tmp_path / "lyrics.txt"
    lyrics.write_text("la", encoding="utf-8")
    code = generate.main(["--url", server, "--key", "nope", "--lyrics-file", str(lyrics), "--description", "x", "--duration", "1"])
    assert code == 2


def test_client_unreachable_exits_2(tmp_path):
    lyrics = tmp_path / "lyrics.txt"
    lyrics.write_text("la", encoding="utf-8")
    code = generate.main(["--url", "http://127.0.0.1:9", "--key", "k", "--lyrics-file", str(lyrics), "--description", "x", "--duration", "1"])
    assert code == 2
