"""ngrok agent lifecycle: start it, confirm the static domain is bound, restart it when it dies."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
import urllib.error
import urllib.request

from .app import ReadyState
from .log import log_event

DEFAULT_API_URL = "http://127.0.0.1:4040"


def parse_tunnels(body: bytes, domain: str) -> bool:
    """True when ngrok's local API lists a tunnel whose public URL carries our domain."""
    try:
        data = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return False
    tunnels = data.get("tunnels", []) if isinstance(data, dict) else []
    return any(domain in str(t.get("public_url", "")) for t in tunnels if isinstance(t, dict))


class NgrokTunnel:
    def __init__(
        self,
        authtoken: str,
        domain: str,
        port: int,
        logger: logging.Logger,
        api_url: str = DEFAULT_API_URL,
        command: list[str] | None = None,
        backoff_start_s: float = 1.0,
        backoff_max_s: float = 60.0,
        bind_timeout_s: float = 60.0,
        poll_s: float = 0.5,
    ):
        self.domain = domain
        self.logger = logger
        self.api_url = api_url.rstrip("/")
        self.command = command or [
            "ngrok", "http", str(port), f"--url={domain}",
            "--log=stdout", "--log-format=json", "--log-level=warn",
        ]
        self.env = {**os.environ, "NGROK_AUTHTOKEN": authtoken}   # env, not argv: keeps the token out of `ps`
        self.backoff_start_s = backoff_start_s
        self.backoff_max_s = backoff_max_s
        self.bind_timeout_s = bind_timeout_s
        self.poll_s = poll_s
        self.proc: subprocess.Popen | None = None
        self.starts = 0

    def start(self) -> None:
        self.proc = subprocess.Popen(self.command, env=self.env)
        self.starts += 1
        log_event(self.logger, "tunnel_start", attempt=self.starts, domain=self.domain)

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def stop(self) -> None:
        if not self.alive():
            return
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()

    def bound(self) -> bool:
        try:
            with urllib.request.urlopen(self.api_url + "/api/tunnels", timeout=2) as resp:
                return parse_tunnels(resp.read(), self.domain)
        except (urllib.error.URLError, OSError, ValueError):
            return False

    def wait_bound(self, timeout_s: float, stop_event: threading.Event | None = None) -> bool:
        """True once the domain is listed. False on timeout, agent death, or stop_event."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if stop_event is not None and stop_event.is_set():
                return False
            if not self.alive():
                return False
            if self.bound():
                return True
            if stop_event is not None:
                stop_event.wait(self.poll_s)
            else:
                time.sleep(self.poll_s)
        return False

    def supervise(self, state: ReadyState, stop_event: threading.Event) -> None:
        """Keep the agent running until stop_event; state.tunnel mirrors whether the domain is bound."""
        backoff = self.backoff_start_s
        while not stop_event.is_set():
            if self.alive():
                stop_event.wait(self.poll_s)
                continue
            state.tunnel = False
            if self.starts > 0:
                code = self.proc.returncode if self.proc else None
                log_event(self.logger, "tunnel_down", level=logging.WARNING, returncode=code, retry_in_s=backoff)
                if stop_event.wait(backoff):
                    break
                backoff = min(backoff * 2, self.backoff_max_s)
            self.start()
            if self.wait_bound(self.bind_timeout_s, stop_event):
                state.tunnel = True
                backoff = self.backoff_start_s
                log_event(self.logger, "tunnel_ready", url=f"https://{self.domain}", attempt=self.starts)
            else:
                log_event(self.logger, "tunnel_bind_timeout", level=logging.WARNING, attempt=self.starts)
        self.stop()
        state.tunnel = False
