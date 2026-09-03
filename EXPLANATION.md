# Explanation log

One entry per completed task, newest first. Plain language for a teammate
reading cold. Why-questions belong in `DECISIONS.md`.

## 2026-09-03 — Task 7: pod verification run

- **What happened:** The first real pod on 2 × L40S (driver 580.178.04)
  downloaded all 57.4 GB of weights successfully, then the inference
  server died on startup with `torch.AcceleratorError: CUDA error: invalid
  device ordinal`, and the container crash-looped.
- **Root cause:** RunPod sets `CUDA_VISIBLE_DEVICES` to an **empty
  string**, not unset. `sglang_omni.models.minimax_music3.config`
  branches on `os.environ.get("CUDA_VISIBLE_DEVICES") is not None`, so an
  empty value takes the "explicitly configured" path, splits to `[""]`,
  filters out the blank, and concludes zero visible GPUs. The CUDA driver
  independently reads the same empty value as "expose no devices", so the
  stage was placed on an ordinal the driver did not have. A shell in the
  pod reported two GPUs because RunPod's web terminal is a separate
  process with a different environment from the container's PID 1.
- **Fixes:**
  - `tunecast/boot.py` gained `sidecar_env()`, which reads the real device
    list from `nvidia-smi` and pins `CUDA_VISIBLE_DEVICES` to
    `0,1,…,N-1` before launching the sidecar, logging the old and new
    values as `cuda_visible_devices_pinned`. When `nvidia-smi` is
    unavailable it leaves the host value untouched and logs
    `gpu_query_unavailable`. `sidecar_start` now also records the pinned
    value and the GPU list.
  - The entrypoint became `tini -s`, because RunPod injects its own PID 1
    and the pod log showed `Tini is not running as PID 1 ... zombie
    reaping won't work`. The `-s` flag registers tini as a child subreaper
    regardless of its PID.
- **How it was verified:** `uv run pytest -q`, 85 passed.
  `test_sidecar_env_pins_cuda_visible_devices_to_real_gpu_count` covers
  the three host states seen or plausible: set-and-empty (the observed
  RunPod value), a UUID string, and unset, plus the no-`nvidia-smi` case.
  Not yet re-run on a pod.
- **Second pod run, on image `sha-304cb59`:** clean boot and a real song.
  Stage timings from `boot.jsonl`: `env_ok` 0.0 s, weights downloaded and
  verified by 57.2 s (54 GB at roughly 950 MB/s), `sidecar_ready` 147.4 s
  (90.2 s of model load), `api_bound` 147.39 s, `tunnel_ready` and
  `api_ready` 147.9 s. Total cold start 147.9 s including the full
  download. `cuda_visible_devices_pinned` logged `None → 0,1`, confirming
  the fix. After load, GPU 0 held 25,283 MiB and GPU 1 13,947 MiB, so the
  two-stage placement worked. `/ready` returned
  `{"model":true,"tunnel":true}` and `/info` reported both cards.
  A 180 s request through the ngrok tunnel with the Hindi example lyrics
  produced 149.2 s of audio in 153.9 s of inference (19,096,136 bytes,
  ratio 1.03 s of compute per second of audio, about US$0.085 at
  US$1.98/h). Under load GPU 0 ran 96–97 % at 26,685 MiB and GPU 1 burst to
  100 % at 13,957 MiB.
- **Third defect, documentation:** the run returned 149 s for a 180 s
  request. That is correct behaviour, not a bug: `duration_s` maps to
  `max_new_tokens`, which is a ceiling, and the autoregressive model ends
  the song when the lyrics are exhausted. The README, the UI label and the
  client's `--duration` help all implied an exact length and now say
  maximum, with the measured example given.
- **Fourth defect, operational:** the first attempt had container disk 80 GB
  and volume disk 0 GB, so `/workspace` was never mounted and the weights
  sat on temporary storage. README now warns about transposing those two
  fields.
- **Fifth defect, client fragility:** the ngrok tunnel dropped the client's
  polling connection twice during real three-minute jobs, and the client
  treated any transport error as fatal, abandoning a job that was still
  running on the pod. `request_with_retries` now retries transport errors
  and 5xx five times at three-second intervals while polling and
  downloading; 4xx and the initial submit are not retried. When it does
  give up it prints the job URL so the audio can be collected later rather
  than regenerated. Two tests cover it: recovery from intermittent drops,
  and giving up after persistent ones.
- **Behavioural finding, song length:** length follows lyric quantity, not
  `duration_s`. The measured point is 697 characters producing 149.2 s,
  about 0.21 s of audio per character. README now carries a conversion
  table, and the example lyric files are sized for roughly three minutes.
- **Documentation:** README gained a Pod runbook holding every command
  actually used to bring up and verify the pod (boot watch, pre-flight with
  the process-environment key trick, in-pod smoke test, the end-to-end
  client run, GPU sampling under load, evidence capture), a section on
  lyric-driven length, and a troubleshooting table covering all five
  defects found in this task.
- **Still open:** a song generated through the web UI rather than the
  client, for completeness of the spec's UI deliverable.

## 2026-09-03 — Task 6: Client script and README

- **What changed:** `client/generate.py`, a rewritten `README.md`, and
  `tests/test_client.py`. The RunPod template lives in the README rather
  than a separate `docs/runpod-template.md`, because `docs/` is gitignored
  in this repo and the template belongs with the setup instructions.
- **How it works:** `client/generate.py` is standard library only, so it
  runs on any Python 3 without installing anything. It reads lyrics from a
  file, posts the job, prints queue position and estimated progress while
  polling, downloads the WAV, and prints the server's inference time and
  the wall time. Exit 0 saved, 1 the server reported a failed job with its
  message, 2 auth or transport failure. Every request carries the bearer
  key and `ngrok-skip-browser-warning`. `--key` defaults to
  `$TUNECAST_API_KEY`.
  The README covers the architecture, licence obligations, one-time setup
  (Docker Hub secrets, ngrok authtoken vs API key, API key, RunPod
  template table with env vars), per-session spin-up, the API table with
  real limits, the client script, local stub development, an empty
  measured-numbers table for Task 7 to fill, limitations, teardown, the
  serverless note, and the boot exit codes.
- **How it was verified:**
  ```
  uv run pytest -q
  84 passed
  ```
  `tests/test_client.py` runs the real supervisor in stub mode on a free
  port and drives the actual client `main()` against it: a 2 s song is
  submitted, polled, downloaded, and checked as a 64 000-frame WAV; a
  wrong key and an unreachable host both exit 2.
  End-to-end through the real tunnel
  (`https://sliding-ethically-beckham.ngrok-free.dev`, stub sidecar):
  ```
  submitted 20260902-222422-046b83 (seed 7, 20s)
  succeeded: inference 1.016s, wall 5s, saved song.wav (2,560,044 bytes)
  wav 2 ch 32000 Hz 20.0 s
  wrong key -> submit failed: HTTP 401 -> exit 2
  ```
  README claims were checked against the code: stage names, exit codes
  1–6, route list, the four request limits, and 32 kHz stereo 16-bit
  (7.7 MB per minute, 23 MB for 180 s) all match. One false claim was
  removed: the CUDA-filter row had said driver compatibility was verified
  in the pod notes, which is Task 7 work, so it now says unconfirmed.
- **Limitations / follow-ups:** The measured-numbers table is empty until
  the pod run. The subagent review of this task could not run (provider
  rate limit), so the accuracy pass above was done directly.

## 2026-09-03 — Task 5: Image and CI

- **What changed:** `Dockerfile`, `.dockerignore`, `docker/requirements.txt`,
  `.github/workflows/docker.yml`, `tests/test_image.py`.
- **How it works:**
  - `FROM hongccc/sglang-omni:dev@sha256:02a85f00…` (digest pin; the base
    is lmsysorg/sglang v0.5.18 on Ubuntu 24.04 with CUDA 13.0.3, and its
    system Python already holds sglang, sgl-kernel, flashinfer with cubins,
    torch 2.13 and sglang-omni's dependency set). Layers, in cache-friendly
    order: apt `tini`; ngrok 3.39.11 from the versioned tarball, verified
    against a sha256 baked as an `ARG`; pinned `uv` 0.11.16; the four exact
    pins in `docker/requirements.txt` installed with `uv pip install
    --system`, with the resolved set frozen to `/app/docker/installed.txt`;
    then our package last with `--no-deps`, followed by an import smoke
    test. `ENTRYPOINT ["tini","--","python3","-m","tunecast.boot"]`,
    `EXPOSE 8080`, a `HEALTHCHECK` on `/health` with a 30-minute start
    period for model load. `TUNECAST_DATA_DIR=/workspace` and
    `SGLANG_OMNI_AUTO_CLONE=0` (disables the base's dev entrypoint habit).
  - Workflow: on push to `main`, tags `v*`, or manual dispatch. Frees
    runner disk (`jlumbroso/free-disk-space`), logs `df -h` before and
    after, buildx, Docker Hub login from `DOCKERHUB_USERNAME` /
    `DOCKERHUB_TOKEN` secrets, `metadata-action` tags `sha-<7>`
    (immutable), `latest` on main, semver on tags, `build-push-action` with
    a registry build cache at `arpitkadam/tunecast:buildcache`, then
    prints the image's `installed.txt`.
  - `.dockerignore` keeps `.env*`, `.git`, `.data`, tests, docs and the
    project markdown out of the build context.
- **How it was verified so far:**
  ```
  uv run pytest -q
  81 passed
  ```
  `tests/test_image.py` guards the promises: base digest, tini entrypoint,
  ngrok version + sha256 + `sha256sum -c`, the four pins, `.dockerignore`
  entries, and the workflow's tags, cache, secrets and disk step. Workflow
  YAML parses. Inputs were verified against live sources: PyPI metadata
  for sglang 0.5.18 / sglang-omni 0.1.4 / flash-attn-4 / flashinfer, the
  base image manifest and config pulled from Docker Hub (76 layers, 14.8 GB
  compressed), the ngrok tarball downloaded and hashed locally. `uv build
  --wheel` confirmed hatchling packages `static/index.html` and
  `weights_manifest.json`. The build-time smoke test imports our package
  and only checks that `sglang_omni` is installed (no CUDA on the runner).
  **First CI run (GitHub Actions run 33687738008, commit 7ddf5a7):**
  success in 14 min 16 s. Runner disk 118 GB free before the build, 71 GB
  after (the runner had 145 GB, more than assumed). Build and push took
  about 6 min; pulling the image back for the package listing another
  5.5 min. ngrok 3.39.11 checksum verified, `tunecast import ok`. The
  install layer resolved 278 packages and changed three: added
  `sglang-omni 0.1.4` and `httptools`, upgraded `huggingface-hub` 1.28.0 →
  1.29.0. Everything else came from the base. Pushed
  `arpitkadam/tunecast:latest` and `:sha-7ddf5a7`, both digest
  `sha256:4ab2ede8…`, 14.84 GB compressed (same as the base within
  rounding). Resolved set (358 entries, Python 3.12.3, torch
  2.13.0+cu130, sglang-kernel 0.4.6.post1, sglang editable from the base's
  source tree) committed as `docker/installed.txt` for reference.
- **Limitations / follow-ups:** The image is unverified on a GPU until
  Task 7. `sglang` is an editable install inside the base image, so it
  appears as a path in `installed.txt`, not a version pin; the base digest
  pins it.

## 2026-09-03 — Task 4: Web UI

- **What changed:** `tunecast/static/index.html` (one file, vanilla JS,
  inline CSS, no external assets) served by `GET /` in `app.py`. One API
  test replaced: `test_root_serves_ui_with_attribution_and_warning`.
- **How it works:** On load the page checks `localStorage` for the key;
  without one it shows the key panel. Every request goes through one
  `api()` helper that adds `Authorization: Bearer` and
  `ngrok-skip-browser-warning`; a 401 clears the key and re-shows the
  panel with the reason. The take sheet posts to `/jobs`; the reel polls
  `/jobs?limit=50` every 3 s and updates rows in place (keyed by id) so
  audio players survive re-renders. Takes are numbered newest-first,
  show state ("queued, 2 ahead", "running", "succeeded", "failed"),
  length, seed, time, description, a 30-segment meter driven by the
  server's progress fraction (labelled estimate while running), timings,
  and the server's error text verbatim. Audio is fetched only when the
  user clicks "Load audio" (each fetch crosses the ngrok 1 GB cap), then
  played from a blob URL and offered as "Save WAV". Delete is two-click.
  `/ready` drives two lamps; `/info` fills the GPU readout. "Powered by
  MiniMax-Music3" sits in the header (licence) and a pinned red banner
  states that outputs die with the pod.
- **How it was verified:**
  ```
  uv run pytest -q
  76 passed
  ```
  Driven with the gstack headless browser against the stub server on
  port 8090: key panel → unlock → fill lyrics/style, length 1:00 → Start
  take → "running" with amber meter and "0:01 of about 0:36 (estimate)"
  → "succeeded, rendered in 0:03" → Load audio → player and Save WAV
  visible → 390 px mobile layout. No console errors from the page. Then
  through `https://sliding-ethically-beckham.ngrok-free.dev/`: ngrok's
  interstitial appeared once, "Visit Site" → key panel → unlock → reel
  listed the take, both lamps green. Screenshots in the session temp
  directory (`tc-ui/ui-*.png`).
  A stale server from the Task 3 run was found still holding 8090 and
  serving the old placeholder; killed, rerun. Fixes from the screenshot
  pass: no underline on the Save link, extra bottom padding on phones so
  the banner never covers the last take.
- **Limitations / follow-ups:** The interstitial is unavoidable on the
  free ngrok plan and appears once per browser. Progress is the server's
  estimate. Loading audio holds the whole WAV in browser memory (~23 MB
  per 3-min song); fine for 3–4 takes.

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
