import json
import logging
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from tunecast.app import ReadyState
from tunecast.tunnel import NgrokTunnel, parse_tunnels

LOG = logging.getLogger("test")
DOMAIN = "example.ngrok-free.dev"
SLEEPER = [sys.executable, "-c", "import time; time.sleep(60)"]
QUITTER = [sys.executable, "-c", "pass"]


def test_parse_tunnels_matches_domain():
    body = json.dumps({"tunnels": [{"public_url": f"https://{DOMAIN}", "proto": "https"}]}).encode()
    assert parse_tunnels(body, DOMAIN) is True


def test_parse_tunnels_false_when_absent_or_garbage():
    assert parse_tunnels(json.dumps({"tunnels": []}).encode(), DOMAIN) is False
    assert parse_tunnels(json.dumps({"tunnels": [{"public_url": "https://other.ngrok-free.dev"}]}).encode(), DOMAIN) is False
    assert parse_tunnels(b"not json", DOMAIN) is False


class FakeNgrokApi(BaseHTTPRequestHandler):
    bound = True

    def log_message(self, *args):
        pass

    def do_GET(self):
        tunnels = [{"public_url": f"https://{DOMAIN}"}] if FakeNgrokApi.bound else []
        body = json.dumps({"tunnels": tunnels}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def api():
    FakeNgrokApi.bound = True
    httpd = HTTPServer(("127.0.0.1", 0), FakeNgrokApi)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def make(api_url, command):
    return NgrokTunnel("tok", DOMAIN, 8080, LOG, api_url=api_url, command=command, backoff_start_s=0.05, poll_s=0.05)


def test_start_alive_stop(api):
    t = make(api, SLEEPER)
    assert t.alive() is False
    t.start()
    assert t.alive() is True
    assert t.wait_bound(timeout_s=2) is True
    t.stop()
    assert t.alive() is False


def test_wait_bound_times_out_when_domain_missing(api):
    FakeNgrokApi.bound = False
    t = make(api, SLEEPER)
    t.start()
    try:
        assert t.wait_bound(timeout_s=0.3) is False
    finally:
        t.stop()


def test_wait_bound_false_when_api_unreachable():
    t = make("http://127.0.0.1:9", SLEEPER)
    t.start()
    try:
        assert t.wait_bound(timeout_s=0.3) is False
    finally:
        t.stop()


def test_supervise_restarts_dead_process_and_flags_state(api):
    t = make(api, QUITTER)
    state = ReadyState(model=True)
    stop = threading.Event()
    thread = threading.Thread(target=t.supervise, args=(state, stop), daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while t.starts < 3 and time.monotonic() < deadline:
        time.sleep(0.05)
    stop.set()
    thread.join(timeout=5)
    assert t.starts >= 3
    assert state.tunnel is False


def test_supervise_marks_tunnel_up_while_bound(api):
    t = make(api, SLEEPER)
    state = ReadyState(model=True)
    stop = threading.Event()
    thread = threading.Thread(target=t.supervise, args=(state, stop), daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not state.tunnel and time.monotonic() < deadline:
        time.sleep(0.05)
    assert state.tunnel is True
    stop.set()
    thread.join(timeout=5)
    assert t.alive() is False


def test_wait_bound_returns_early_on_stop(api):
    FakeNgrokApi.bound = False
    t = make(api, SLEEPER)
    t.start()
    stop = threading.Event()
    threading.Timer(0.2, stop.set).start()
    started = time.monotonic()
    try:
        assert t.wait_bound(timeout_s=30, stop_event=stop) is False
    finally:
        t.stop()
    assert time.monotonic() - started < 5
