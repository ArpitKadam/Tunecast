import json
import threading
import wave
from http.server import BaseHTTPRequestHandler, HTTPServer
from io import BytesIO

import pytest

from tunecast.sidecar import GenerateParams, HttpSidecar, SidecarError

PARAMS = GenerateParams(lyrics="[Verse]\nhi", description="lofi", duration_s=3, seed=42)


def tiny_wav(seconds: int) -> bytes:
    buf = BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(32000)
        w.writeframes(b"\x00\x00\x00\x00" * (seconds * 32000))
    return buf.getvalue()


class FakeSglOmni(BaseHTTPRequestHandler):
    received: list[dict] = []
    mode = "ok"

    def log_message(self, *args):  # keep pytest output clean
        pass

    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(length))
        FakeSglOmni.received.append(payload)
        if self.path != "/v1/audio/speech":
            self.send_response(404)
            self.end_headers()
            return
        if FakeSglOmni.mode == "error":
            body = json.dumps({"error": "CUDA out of memory on device 1"}).encode()
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
            return
        body = tiny_wav(payload["max_new_tokens"] // 25)
        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def server():
    FakeSglOmni.received = []
    FakeSglOmni.mode = "ok"
    httpd = HTTPServer(("127.0.0.1", 0), FakeSglOmni)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def test_ready_true_when_health_answers(server):
    assert HttpSidecar(server, "MiniMaxAI/MiniMax-Music3").ready() is True


def test_ready_false_when_nothing_listens():
    assert HttpSidecar("http://127.0.0.1:9", "m").ready() is False


def test_generate_posts_official_payload_and_writes_wav(server, tmp_path):
    out = tmp_path / "song.wav"
    HttpSidecar(server, "MiniMaxAI/MiniMax-Music3").generate(PARAMS, out)

    assert FakeSglOmni.received == [{
        "model": "MiniMaxAI/MiniMax-Music3",
        "input": "[Verse]\nhi",
        "instructions": "lofi",
        "seed": 42,
        "max_new_tokens": 75,
        "response_format": "wav",
        "stream": False,
    }]
    with wave.open(str(out), "rb") as w:
        assert w.getnframes() == 3 * 32000
    assert not out.with_suffix(".wav.part").exists()


def test_generate_surfaces_sidecar_error_body(server, tmp_path):
    FakeSglOmni.mode = "error"
    with pytest.raises(SidecarError, match="sidecar HTTP 500.*CUDA out of memory"):
        HttpSidecar(server, "m").generate(PARAMS, tmp_path / "song.wav")
    assert not (tmp_path / "song.wav").exists()


def test_generate_unreachable_is_sidecar_error(tmp_path):
    with pytest.raises(SidecarError, match="unreachable"):
        HttpSidecar("http://127.0.0.1:9", "m").generate(PARAMS, tmp_path / "song.wav")
