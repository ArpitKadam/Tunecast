# Explanation log

One entry per completed task, newest first. Plain language for a teammate
reading cold. Why-questions belong in `DECISIONS.md`.

## 2026-09-03 — Task 3: Supervisor, ngrok tunnel, readiness

- **What changed:** `tunecast/boot.py` (the container entrypoint) and
  `tunecast/tunnel.py`, with `tests/test_boot.py` and
  `tests/test_tunnel.py`. `log.setup_logging` gained a `name` parameter so
  job events get their own `jobs.jsonl`; the test fixture now resets every
  `tunecast*` logger.
- **How it works:**
  - `python -m tunecast.boot` → `main()` loads settings (exit 1 with the
    variable name on stderr if unusable) and runs `Supervisor.run()`.
  - `run()` logs `env_ok`, then: stub mode uses `StubSidecar`; real mode
    calls `ensure_weights` (exit 2 on failure), spawns
    `sgl-omni serve --model-path <models_dir> --host 127.0.0.1 --port
    8000` (+ `--dit_dav.factory.dit_steps` / `dit_cfg_scale` when set) and
    polls `HttpSidecar.ready()` for up to 30 min (exit 3). Then it opens
    the job store, fails stale running/queued rows ("pod restarted"),
    starts the worker threads, builds the app, and **binds the API socket
    before anything is exposed** (`api_bound`; exit 6 if the port is
    taken). Only then does it spawn `ngrok http <port> --url=<domain>`
    with the authtoken in the child's environment (never argv), waits up
    to 60 s for the domain to appear in ngrok's local API (exit 4), flips
    `ReadyState.tunnel`, and hands the agent to a supervise thread that
    restarts it with 1→60 s exponential backoff and drops `/ready` to 503
    while it is down. Finally `api_ready` is logged and uvicorn runs in
    the main thread on the pre-bound socket. A watcher thread turns a dead
    sidecar into exit 5. SIGTERM reaches uvicorn's handler; the `finally`
    stops ngrok and the sidecar.
  - Every stage line carries `elapsed_s` (since the previous stage) and
    `since_boot_s`, which is the cold-start breakdown the README will
    report.
- **How it was verified:**
  ```
  uv run pytest -q
  76 passed in 20.57s
  ```
  Unit tests drive `NgrokTunnel` with `python -c` stand-ins for the agent
  and a stdlib fake of ngrok's local API (bind, timeout, unreachable API,
  restart-on-death, state flag). `test_stub_boot_reaches_ready_and_serves_a_job`
  runs the real supervisor in a thread on a free port and checks
  `/ready`, a job submit, a clean stop (exit 0), and both log files.

  Live run on the laptop with the real ngrok agent and the owner's
  `.env` sourced by the shell (values never printed), stub sidecar, port
  8090 because Apache (`httpd.exe`) owns 8080 on this machine:
  ```
  boot → api_bound 0.06 s → tunnel_ready 1.27 s → api_ready 1.27 s
  public /ready                      200 {"model":true,"tunnel":true}
  public /info without key           401
  public /info with key              200 (stub, RTX 3050 listed)
  public POST /jobs (5 s)            202 → succeeded, inference_s 0.25
  public GET /jobs/<id>/audio        200, 640 044 bytes, 0.94 s, 5.0 s WAV
  public GET /                       200
  ```
  The first live attempt exposed a real defect: readiness was logged
  before the bind, and a bind failure produced no JSON event and reused
  exit code 1. Fixed test-first (`test_boot_exits_6_when_api_port_is_taken`).
- **Limitations / follow-ups:** The sidecar CLI override names
  (`--dit_dav.factory.dit_steps`, `--dit_dav.factory.dit_cfg_scale`) follow
  sglang-omni's `<stage>.<section>.<field>` convention seen in its cookbook
  but are unverified until the pod run; they are only emitted when the env
  vars are set. Progress while the tunnel is down is not queued anywhere:
  clients see 503 on `/ready` and retry. On Windows, refusing a closed
  port takes ~2 s, so `test_wait_bound_false_when_api_unreachable` is slow.

## 2026-09-03 — Task 2: Sidecar clients, job queue, authenticated API

- **What changed:** Four new modules: `tunecast/sidecar.py`,
  `tunecast/jobs.py`, `tunecast/gpu.py`, `tunecast/app.py`. Three new test
  files. Dev dependency `httpx` replaced by `httpx2` (Starlette deprecated
  the old one). `.env.example` rewritten with every variable and its purpose.
- **How it works:**
  - `sidecar.py`: `GenerateParams` carries lyrics, description,
    duration, seed, format and derives `max_new_tokens` (duration × 25) and
    an HTTP timeout (duration × 10 + 120 s). `HttpSidecar` POSTs the
    official payload to `/v1/audio/speech` on `sgl-omni`, writes the WAV
    atomically (`.part` then rename), and turns any non-200, timeout, or
    connection failure into `SidecarError` carrying the server's message.
    `StubSidecar` sleeps briefly and writes a 400 Hz sine (32 kHz stereo
    16-bit) of the requested length, so the whole stack runs without CUDA.
  - `jobs.py`: `JobStore` is one SQLite connection behind a lock (WAL),
    one `jobs` table; `params`, `timings`, `gpu` are JSON columns. It
    creates, reads, lists newest-first, updates whitelisted columns,
    deletes row + WAV, counts jobs ahead of a queued job, marks stale
    running/queued rows failed with "pod restarted", and prunes succeeded
    jobs beyond `keep_last`. `Estimator` predicts inference time from the
    median ratio of the last 10 jobs (seed 0.6 s per audio second).
    `JobRunner` starts N daemon threads that pop job ids from a
    `queue.Queue`, mark running, call the sidecar, record timings + GPU
    memory, mark succeeded/failed, then prune. A worker never dies: any
    exception becomes a failed job with the message.
  - `app.py`: `create_app(settings, store, runner, sidecar, state)` builds
    the FastAPI app. `require_key` parses `Authorization: Bearer` and
    compares with `hmac.compare_digest`; failures are 401 with
    `WWW-Authenticate: Bearer`. Routes: `/health`, `/ready` (503 until both
    `ReadyState` flags), `/info`, `POST /jobs` (202, seed randomised when
    omitted), `GET /jobs`, `GET /jobs/{id}`, `GET /jobs/{id}/audio`
    (`FileResponse`, 409 until succeeded), `DELETE /jobs/{id}` (409 while
    running, 204 otherwise), `/` placeholder until Task 4.
  - `gpu.py`: parses `nvidia-smi --query-gpu=…` CSV into dicts; empty list
    when the tool is missing.
- **How it was verified:**
  ```
  uv run pytest -q
  62 passed in 9.91s
  ```
  Tests first (collection errors), then implementation. API tests drive
  the real app through `TestClient` with the stub sidecar and real worker
  threads. `tests/test_sidecar_http.py` runs a stdlib `http.server`
  imitating `sgl-omni` to check the exact payload, WAV write, 500-body
  surfacing, and unreachable handling. One design correction found by a
  test: `queue_position` now means "jobs ahead of you" (running + queued
  earlier), not index among queued; spec updated. Review pass found a
  delete-vs-worker race on queued jobs: `JobStore.delete` is now a
  conditional SQL delete (`status != 'running'`) returning a bool, and a
  worker whose row vanished mid-run discards its WAV instead of orphaning
  it. `HttpSidecar.ready()` stops after a connection-level failure instead
  of probing a second path.
- **Limitations / follow-ups:** `timings` has `queue_wait_s`,
  `inference_s`, `total_s`; the sidecar writes the file inside `generate`,
  so a separate encode/write time is not observable from outside. Progress
  while running is an estimate (capped at 0.95) because `sgl-omni` is
  non-streaming. Refusing a closed localhost port takes ~2 s on Windows,
  which is why the unreachable tests are the slowest.

## 2026-09-02 — Task 1: Foundation (settings, JSON logging, weights)

- **What changed:** New `tunecast` package with three modules and their
  tests. `pyproject.toml` pinned to Python 3.12 with `fastapi`, `uvicorn`,
  `huggingface_hub` as runtime deps and `pytest` + `httpx` for dev;
  `uv.lock` generated. `tunecast/weights_manifest.json` records all 88
  files (57 353 379 600 bytes) of the pinned MiniMax-Music3 snapshot.
  `.gitignore` ignores `.data/` (local stub data).
- **How it works:**
  - `config.load_settings(env)` reads the environment into a frozen
    `Settings`. Missing `TUNECAST_API_KEY` raises `ConfigError`; so does a
    missing `NGROK_AUTHTOKEN` or `NGROK_DOMAIN` unless `NGROK_ENABLED=0`.
    `TUNECAST_MAX_CONCURRENT` is clamped to 1–4. `data_dir` defaults to
    `/workspace`, or `./.data` in stub mode. Derived paths (`models_dir`,
    `outputs_dir`, `db_path`, `logs_dir`) are properties.
  - `log.setup_logging(dir, filename)` returns the shared `tunecast`
    logger with a stdout handler and, when a directory is given, a
    `.jsonl` file handler; calling it again replaces handlers instead of
    stacking them. `log_event(logger, "name", **fields)` writes one JSON
    object with `ts`, `level`, `event`, plus the fields. `redact(env)`
    masks any variable containing KEY, TOKEN, or SECRET.
  - `weights.verify(models_dir, revision)` returns the relative paths that
    are missing or wrong-sized, plus the `.tunecast_complete` marker when
    it is absent or holds a different revision. `ensure_weights(settings,
    logger)` returns `0.0` immediately when `verify` is clean, otherwise
    runs `snapshot_download` at the pinned revision into `models_dir`,
    re-verifies, raises `WeightsError` naming the bad files if still
    incomplete, and only then writes the marker. Fast downloads come from
    `hf_xet`, which huggingface_hub 1.x installs and uses by default; the
    old `hf_transfer` extra no longer exists in 1.x, so the design's
    `HF_HUB_ENABLE_HF_TRANSFER` mention is dropped.
- **How it was verified:**
  ```
  uv lock && uv sync --group dev
  uv run pytest -q
  24 passed in 0.88s
  ```
  Tests were written first and failed with import errors before the
  modules existed. Manifest generation asserted the HF API `sha` equals the
  pinned revision. Review pass added: an autouse fixture that detaches log
  handlers after each test, a 1–65535 range check on both ports, a test
  that the real `snapshot_download` signature accepts the kwargs we pass,
  and `huggingface_hub<2` in `pyproject.toml`.
- **Limitations / follow-ups:** Integrity is size-based; bit-flips on the
  volume disk are not detected (see `DECISIONS.md`). Job log sink
  (`jobs.jsonl`) is added when the job runner exists (Task 2).
