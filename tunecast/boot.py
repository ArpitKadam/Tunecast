"""Pod entrypoint: validate env, ensure weights, start the sidecar, the tunnel, and the API; supervise all three."""

from __future__ import annotations

import logging
import signal
import subprocess
import sys
import threading
import time

import uvicorn

from .app import ReadyState, create_app
from .config import ConfigError, Settings, load_settings
from .gpu import query_gpus
from .jobs import Estimator, JobRunner, JobStore
from .log import log_event, setup_logging
from .sidecar import HttpSidecar, Sidecar, StubSidecar
from .tunnel import NgrokTunnel
from .weights import ensure_weights

EXIT_OK = 0
EXIT_CONFIG = 1
EXIT_WEIGHTS = 2
EXIT_SIDECAR = 3
EXIT_TUNNEL = 4
EXIT_SIDECAR_DIED = 5
EXIT_API_BIND = 6

SIDECAR_READY_TIMEOUT_S = 1800   # model load from disk; 57 GB
TUNNEL_BIND_TIMEOUT_S = 60
SIDECAR_POLL_S = 2.0


def build_sidecar_command(settings: Settings) -> list[str]:
    cmd = [
        "sgl-omni", "serve",
        "--model-path", str(settings.models_dir),
        "--host", "127.0.0.1",
        "--port", str(settings.sidecar_port),
    ]
    # sglang-omni CLI overrides follow <stage>.<section>.<field>; the acoustic stage is "dit_dav".
    if settings.dit_steps is not None:
        cmd += ["--dit_dav.factory.dit_steps", str(settings.dit_steps)]
    if settings.dit_cfg_scale is not None:
        cmd += ["--dit_dav.factory.dit_cfg_scale", str(settings.dit_cfg_scale)]
    return cmd


class SidecarProcess:
    def __init__(self, settings: Settings, logger: logging.Logger):
        self.command = build_sidecar_command(settings)
        self.logger = logger
        self.proc: subprocess.Popen | None = None

    def start(self) -> None:
        self.proc = subprocess.Popen(self.command)   # stdio inherited: sidecar logs land in the pod log

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def returncode(self) -> int | None:
        return self.proc.returncode if self.proc else None

    def wait_ready(self, client: HttpSidecar, timeout_s: float, stop_event: threading.Event | None = None) -> bool:
        """True once the sidecar answers. False on timeout, process death, or stop_event."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if stop_event is not None and stop_event.is_set():
                return False
            if not self.alive():
                return False
            if client.ready():
                return True
            if stop_event is not None:
                stop_event.wait(SIDECAR_POLL_S)
            else:
                time.sleep(SIDECAR_POLL_S)
        return False

    def stop(self) -> None:
        if not self.alive():
            return
        self.proc.terminate()
        try:
            self.proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()


class Supervisor:
    def __init__(self, settings: Settings, host: str = "0.0.0.0"):
        self.settings = settings
        self.host = host
        self.exit_code = EXIT_OK
        self._stop = threading.Event()
        self._server: uvicorn.Server | None = None
        self._sidecar_proc: SidecarProcess | None = None
        self._tunnel: NgrokTunnel | None = None

    def request_stop(self) -> None:
        self._stop.set()
        if self._server is not None:
            self._server.should_exit = True

    def _install_signal_handlers(self) -> dict:
        """As PID 1 a process with no handler ignores SIGTERM; catch it from the first second of boot.
        uvicorn replaces these handlers once it serves. Returns the previous handlers for restoration."""
        if threading.current_thread() is not threading.main_thread():
            return {}
        previous = {}
        for sig in (signal.SIGTERM, signal.SIGINT):
            previous[sig] = signal.signal(sig, lambda signum, frame: self.request_stop())
        return previous

    def run(self) -> int:
        s = self.settings
        boot_t0 = time.monotonic()
        last = boot_t0
        logger = setup_logging(s.logs_dir, "boot.jsonl")
        jobs_logger = setup_logging(s.logs_dir, "jobs.jsonl", name="tunecast.jobs")
        previous_handlers = self._install_signal_handlers()

        def stage(name: str, **fields) -> None:
            nonlocal last
            now = time.monotonic()
            log_event(logger, name, elapsed_s=round(now - last, 2), since_boot_s=round(now - boot_t0, 2), **fields)
            last = now

        stage(
            "env_ok",
            stub=s.stub, data_dir=str(s.data_dir), port=s.port, sidecar_port=s.sidecar_port,
            ngrok_enabled=s.ngrok_enabled, ngrok_domain=s.ngrok_domain,
            max_concurrent=s.max_concurrent, keep_last=s.keep_last, model_revision=s.model_revision,
        )

        try:
            sidecar: Sidecar
            if s.stub:
                sidecar = StubSidecar()
                stage("stub_mode")
            else:
                try:
                    download_s = ensure_weights(s, logger)
                except Exception as e:
                    log_event(logger, "weights_failed", level=logging.ERROR, error=str(e))
                    self.exit_code = EXIT_WEIGHTS
                    return self.exit_code
                stage("weights_ready", download_s=round(download_s, 1))

                self._sidecar_proc = SidecarProcess(s, logger)
                self._sidecar_proc.start()
                stage("sidecar_start", command=" ".join(self._sidecar_proc.command))
                sidecar = HttpSidecar(f"http://127.0.0.1:{s.sidecar_port}", s.model_id)
                if not self._sidecar_proc.wait_ready(sidecar, SIDECAR_READY_TIMEOUT_S, self._stop):
                    if self._stop.is_set():
                        log_event(logger, "shutdown_requested", during="sidecar_wait")
                        return EXIT_OK
                    log_event(
                        logger, "sidecar_not_ready", level=logging.ERROR,
                        alive=self._sidecar_proc.alive(), returncode=self._sidecar_proc.returncode(),
                        timeout_s=SIDECAR_READY_TIMEOUT_S,
                    )
                    self.exit_code = EXIT_SIDECAR
                    return self.exit_code
                stage("sidecar_ready", gpu=query_gpus())

            store = JobStore(s.db_path, s.outputs_dir)
            stale = store.mark_stale_failed()
            if stale:
                log_event(logger, "stale_jobs_failed", count=stale)
            runner = JobRunner(store, sidecar, s.max_concurrent, Estimator(), s.keep_last, jobs_logger)
            runner.start()
            state = ReadyState(model=True)
            app = create_app(s, store, runner, sidecar, state)

            config = uvicorn.Config(app, host=self.host, port=s.port, log_level="warning", access_log=False)
            self._server = uvicorn.Server(config)
            try:
                sock = config.bind_socket()   # bind before the tunnel exists; uvicorn exits on failure
            except SystemExit:
                log_event(logger, "api_bind_failed", level=logging.ERROR, host=self.host, port=s.port)
                self.exit_code = EXIT_API_BIND
                return self.exit_code
            stage("api_bound", url=f"http://{self.host}:{s.port}")

            if s.ngrok_enabled:
                self._tunnel = NgrokTunnel(s.ngrok_authtoken, s.ngrok_domain, s.port, logger)
                self._tunnel.start()
                if not self._tunnel.wait_bound(TUNNEL_BIND_TIMEOUT_S, self._stop):
                    if self._stop.is_set():
                        log_event(logger, "shutdown_requested", during="tunnel_wait")
                        return EXIT_OK
                    log_event(
                        logger, "tunnel_not_bound", level=logging.ERROR,
                        alive=self._tunnel.alive(), domain=s.ngrok_domain, timeout_s=TUNNEL_BIND_TIMEOUT_S,
                    )
                    self.exit_code = EXIT_TUNNEL
                    return self.exit_code
                state.tunnel = True
                stage("tunnel_ready", url=f"https://{s.ngrok_domain}")
                threading.Thread(
                    target=self._tunnel.supervise, args=(state, self._stop), name="ngrok-supervisor", daemon=True
                ).start()
            else:
                state.tunnel = True
                stage("tunnel_disabled")

            if self._sidecar_proc is not None:
                threading.Thread(target=self._watch_sidecar, args=(logger,), name="sidecar-watch", daemon=True).start()
            stage("api_ready", url=f"http://{self.host}:{s.port}", public_url=f"https://{s.ngrok_domain}" if s.ngrok_enabled else None)

            self._server.run(sockets=[sock])
            log_event(logger, "shutdown", exit_code=self.exit_code, since_boot_s=round(time.monotonic() - boot_t0, 2))
            return self.exit_code
        finally:
            self._stop.set()
            if self._tunnel is not None:
                self._tunnel.stop()
            if self._sidecar_proc is not None:
                self._sidecar_proc.stop()
            for sig, handler in previous_handlers.items():
                signal.signal(sig, handler)

    def _watch_sidecar(self, logger: logging.Logger) -> None:
        while not self._stop.is_set():
            if not self._sidecar_proc.alive():
                log_event(logger, "sidecar_died", level=logging.ERROR, returncode=self._sidecar_proc.returncode())
                self.exit_code = EXIT_SIDECAR_DIED
                self.request_stop()
                return
            self._stop.wait(SIDECAR_POLL_S)


def main(argv: list[str] | None = None) -> int:
    try:
        settings = load_settings()
    except ConfigError as e:
        print(f"tunecast: {e}", file=sys.stderr)
        return EXIT_CONFIG
    return Supervisor(settings).run()


if __name__ == "__main__":
    sys.exit(main())
