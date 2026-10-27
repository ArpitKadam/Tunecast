# Architecture

Current-state description of how Tunecast is built. Rationale lives in
`DECISIONS.md`; per-task narratives in `EXPLANATION.md`.

## System at a glance

```
Browser / client script
        │  HTTPS, Bearer key, ngrok-skip-browser-warning
        ▼
ngrok edge (sliding-ethically-beckham.ngrok-free.dev)
        │  tunnel
        ▼
┌──────────────────────── RunPod pod (2×L40S) ────────────────────────┐
│  tunecast.boot (PID 1, Python)                                      │
│   ├─ uvicorn + FastAPI  :8080   auth · jobs · UI · /ready           │
│   │     └─ JobRunner threads ──HTTP──▶ sgl-omni serve :8000 (local) │
│   │                                     GPU0: Qwen3-8B AR + RVQ     │
│   │                                     GPU1: flow-matching + DAV   │
│   ├─ ngrok agent subprocess (restarted with backoff)                │
│   └─ JSON logs → stdout + /workspace/tunecast/logs/*.jsonl          │
│                                                                     │
│  /workspace (pod volume disk, 80 GB)                                │
│   ├─ models/MiniMax-Music3/   57.4 GB + .tunecast_complete           │
│   └─ tunecast/ jobs.db · outputs/<id>.wav · logs/                   │
└─────────────────────────────────────────────────────────────────────┘
```

## Components

| Component | File | Responsibility |
| --- | --- | --- |
| Settings | `tunecast/config.py` | Parse and validate env into a frozen `Settings`; hard-fail on missing secrets |
| Logging | `tunecast/log.py` | JSON formatter, stdout + file sinks, secret redaction, `log_event` helper |
| Weights | `tunecast/weights.py`, `tunecast/weights_manifest.json` | Manifest verify, idempotent `snapshot_download`, completion marker |
| Sidecar | `tunecast/sidecar.py` | `HttpSidecar` (sgl-omni client) and `StubSidecar` (sine WAV) behind one interface; `inspect_wav` validates every response and `generate` returns `AudioInfo` |
| Jobs | `tunecast/jobs.py` | SQLite `JobStore` (nullable `audio_duration_s`, `audio_sample_rate`, `audio_channels`, `audio_bytes`, `request_id` under a UNIQUE index, `request_hash`, `parent_job_id`, `experiment_id` (indexed; old rows backfilled to their own id), `model_revision`, `config_snapshot`, `title`, `favorite` (labels: outside the fingerprint, updatable); all added in place to older DBs), `JobRunner` worker threads, queue position, progress estimate, prune |
| GPU info | `tunecast/gpu.py` | `nvidia-smi` query, empty list when absent; `GpuPeakSampler` (daemon thread, ~1 s poll, started before `generate`, stopped in `finally`) records peak whole-GPU memory per job |
| API | `tunecast/app.py` | FastAPI factory, bearer auth, routes, `PATCH /jobs/{id}` labels, the page and its two content-versioned assets (`/static/app.js`, `/static/app.css`) |
| Tunnel | `tunecast/tunnel.py` | ngrok subprocess, bind check via local API, restart loop |
| Supervisor | `tunecast/boot.py` | Boot sequence, sidecar process, readiness state, exit codes |
| UI | `tunecast/static/index.html`, `app.js`, `app.css` | Markup and `<template>`s; one ES module holding the shared state (jobs, current track, A/B picks, Lab draft, pending submit), the Player and Lab tab panels, one `<audio>` in a player dock outside the panels, polling and key handling; styles |
| Client | `client/generate.py`, `client/example-*-en.txt` | Submit, poll with retries, save; end-to-end proof, plus a worked English example (the Hindi pair from the measured run is bundled in `demo/fixtures.json`) |
| Demo | `demo/`, `scripts/build_demo.py` | Offline copy of the UI. `index.html`, `app.js`, `app.css` are generated from `tunecast/static/` and committed (`tests/test_demo_build.py` guards drift). `shim.js` answers the API from `fixtures.json` in memory, `demo.css` styles the banner. Audio is the root `song-client.wav`. Needs a static server, not `file://` |
| Media | `media/`, `scripts/capture_media.py` | README screenshots and a two-tab recording rendered from the real app on the stub sidecar via the e2e `LiveServer`. Interface only, not measurements |
| Image | `Dockerfile`, `docker/requirements.txt`, `.dockerignore` | Digest-pinned base, four exact-pinned additions on system Python, ngrok binary by sha256, tini entrypoint |
| CI | `.github/workflows/docker.yml` | `test` job on push, pull request and dispatch (unit tests, then the Playwright e2e suite on chromium); `build` (image build and push to Docker Hub) `needs: test` and never runs on pull requests |

## Data flow: one generation

1. `POST /jobs` → auth → 503 + `Retry-After` while `ReadyState.model` is false (a replay of an existing key still returns its job) → pydantic validation + optional `Idempotency-Key` header (`[A-Za-z0-9_-]{8,128}`, else 422) → fingerprint the body (before the random seed is filled) → fill seed → optional lineage fields `parent_job_id` / `experiment_id` (fingerprinted only when set, so older retries keep their hashes) → `JobStore.create` (resolves the experiment: root = own id, child inherits; unknown parent → 422, checked after the duplicate-key check) (status `queued`) → `JobRunner.submit(id)` → 202 with job JSON. `create` also counts `queued` rows under the store lock: at or over `TUNECAST_MAX_QUEUED` (default 8) it raises `QueueFull` → 429 + `Retry-After: 30`, nothing inserted or enqueued. A key already on a job raises `DuplicateRequest` first, so a replay is never refused for capacity: same fingerprint → 200 with the existing job, different → 409, nothing enqueued either way. `GET /jobs?request_id=<id>` returns `[job]` or `[]` so a client can reconcile after a lost response.
2. Worker thread pops the id, skips it if the job is gone or no longer `queued` (cancelled), claims it with a conditional update (0 rows = cancelled meanwhile), `JobStore.update(status="running", started_at=…)`, builds the sidecar payload (`input`, `instructions`, `seed`, `max_new_tokens = duration_s × 25`, `response_format`, `stream=false`), POSTs to `http://127.0.0.1:8000/v1/audio/speech`.
3. The body must be non-empty and complete (Content-Length enforced); `inspect_wav` checks the RIFF/WAVE header, fmt (PCM, float or EXTENSIBLE) and a data chunk of whole frames that fits in the bytes received. Only then is it written atomically (`.part`, `os.replace`) to `outputs/<id>.wav`. Duration, sample rate, channels and size are stored; `timings`, `gpu` captured; status `succeeded`. Any failure (bad body, truncation, timeout, disk error) → no file left, status `failed`, `error` filled.
4. `GET /jobs/{id}` returns the row plus computed `queue_position` (jobs ahead: running + queued earlier) and `progress` (estimate, capped 0.95 while running). Live jobs also carry `estimate {seconds, confidence: low|medium}`; finished jobs carry `real_time_factor` (inference seconds ÷ real audio seconds). The `Estimator` learns from `audio_duration_s`, not the requested ceiling.
5. Connection and auth state live in `app.js`: `link {state, fails, okAt}` is written only by `note(ok)` (online → reconnecting after one failed poll or readiness check → offline after 3 → online on any success); one `tick()` timer chain polls when signed in, retries `unlock()` when a key is stored, and otherwise checks `/ready`, with delay `min(3 s × 2^fails, 30 s)` and a 10 s per-request abort; `lock(msg)` is the single teardown for sign-out and 401. The key is stored in `sessionStorage` by default, in `localStorage` (`tunecast_key`) only when "Remember on this device" is ticked. Masthead controls: `#link-state` (`role=status`), `#link-age`, `#retry`, `#signout`. `/ready` answering 503 counts as reachable. No new routes.
6. `PATCH /jobs/{id}` `{title?, favorite?}` sets labels on a job in any status (404 unknown, 422 invalid, `extra=forbid`). The UI loads `/`, then `/static/app.css?v=<hash>` and `/static/app.js?v=<hash>`; audio is fetched with the key into a blob only on Play, Save WAV or an A/B flip, then played by the one dock element. `GET /jobs/{id}/audio` streams the file with `Content-Disposition: attachment`. `GET /experiments/{id}/export?format=json|csv` returns the experiment's jobs in the flat `EXPORT_FIELDS` schema (404 unknown experiment, 422 bad format).
6. Prune deletes the oldest succeeded jobs beyond `TUNECAST_KEEP_LAST`.

## Key interfaces

```python
# config.py
def load_settings(env: Mapping[str, str] = os.environ) -> Settings   # raises ConfigError

# weights.py
def verify(models_dir: Path, revision: str) -> list[str]   # relative paths missing/mismatched; [] = ok
def ensure_weights(settings: Settings) -> float            # seconds spent downloading; 0.0 if present

# sidecar.py
@dataclass(frozen=True)
class GenerateParams: lyrics: str; description: str; duration_s: int; seed: int; format: str
class Sidecar(Protocol):
    def ready(self) -> bool: ...
    def generate(self, params: GenerateParams, out_path: Path) -> AudioInfo: ...   # raises SidecarError
    def describe(self) -> dict: ...

# jobs.py
class JobStore:  create(params, request_id=None, request_hash=None, max_queued=None, *, lineage…) -> Job   # raises DuplicateRequest(job) | QueueFull(queued) | UnknownParent
                 get(id) -> Job | None; get_by_request_id(id) -> Job | None; list(limit) -> list[Job]
                 update(id, **fields) -> int (rows changed); delete(id) -> bool (False if running/unknown)
                 ahead_of(id) -> int; queued_ids() -> list[str]; mark_stale_failed() -> int; prune(keep_last) -> list[str]
class JobRunner: submit(job_id: str) -> None; start() -> None
def job_to_dict(job: Job, store: JobStore, estimator: Estimator) -> dict

# app.py
class ReadyState: model: bool; tunnel: bool
def create_app(settings, store, runner, sidecar, state: ReadyState) -> FastAPI

# tunnel.py
class NgrokTunnel: start() -> None; wait_bound(timeout_s) -> bool; alive() -> bool; stop() -> None

# boot.py
def build_sidecar_command(settings) -> list[str]
class SidecarProcess: start(); alive(); returncode(); wait_ready(client, timeout_s) -> bool; stop()
class Supervisor(settings, host="0.0.0.0"): run() -> int; request_stop()
def main() -> int   # exit codes: 1 config, 2 weights, 3 sidecar never ready, 4 tunnel never bound, 5 sidecar died, 6 API port taken
```

Boot order: env → weights → sidecar ready → job store/workers → **bind API socket** → ngrok bound → serve. The socket is bound before the tunnel exists so the public domain never fronts a closed port.

## Invariants

- No request except `/health`, `/ready`, `/` and `/static/app.js|app.css` succeeds without the bearer key. The two assets hold no secrets and are named by content hash. OpenAPI/docs routes are disabled; a test walks every route and asserts 401 without a key.
- While the link is not online the page only marks state (indicator, stale list, unknown lamps); the playing blob is untouched. Only `lock()` (sign-out or a 401) ejects audio and forgets loaded takes.
- The page has exactly one `<audio>`, outside the tab panels. Panels are hidden, never removed, so tab switches keep playback and drafts.
- `POST /jobs` never waits on inference; a job outlives any client, and is recoverable by id until its pod dies.
- `/ready` is 200 only while both the sidecar and the tunnel are up.
- A job that stops running for any reason ends `succeeded` or `failed`, never stuck.
- `DELETE /jobs/{id}`: a `queued` job is cancelled (row removed, never reaches the sidecar), a finished job is deleted, a `running` job returns 409 because the sidecar has no cancel.
- At most one job per non-null `request_id`; the UNIQUE index decides, not application code. A job is enqueued only by the call that inserted it. Deleting or pruning a job frees its key. The UI and CLI reuse one key for every retry of a submission, including after a 5xx.
- Nothing under `/workspace` is required to exist at boot; boot creates what it needs.
- Secrets never appear in logs, the image, or the repo.
- The image contains no model weights.
- The sidecar always sees a `CUDA_VISIBLE_DEVICES` that matches the devices `nvidia-smi` reports, never the host's value.

## Directory map

```
Tunecast/
├── tunecast/            application package (see Components)
│   └── static/          index.html, app.js, app.css
├── tests/               pytest, stub mode only
│   └── e2e/             Playwright browser suite (`-m e2e`); `LiveServer` = real app + stub sidecar on a free port
├── client/generate.py
├── demo/               offline UI demo: generated index.html/app.js/app.css + shim.js, demo.css, fixtures.json (served statically)
├── media/              README screenshots and recording, rendered by scripts/capture_media.py from the stub sidecar
├── scripts/            build_demo.py (regenerates demo/), capture_media.py (regenerates media/)
├── song-client.wav     the one real output of the measured pod run (not in the image)
├── docker/              requirements.txt: additions on top of the base image
├── docs/               gitignored: superpowers/{specs,plans}/
├── Dockerfile
├── pyproject.toml, uv.lock
├── .github/workflows/docker.yml
├── TASK.md, DECISIONS.md, ARCHITECTURE.md, EXPLANATION.md, README.md
├── RUNBOOK.md          RunPod template, per-session commands, job recovery, troubleshooting, teardown
└── CLAUDE.md
```
