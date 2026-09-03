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
| Sidecar | `tunecast/sidecar.py` | `HttpSidecar` (sgl-omni client) and `StubSidecar` (sine WAV) behind one interface |
| Jobs | `tunecast/jobs.py` | SQLite `JobStore`, `JobRunner` worker threads, queue position, progress estimate, prune |
| GPU info | `tunecast/gpu.py` | `nvidia-smi` query, empty list when absent |
| API | `tunecast/app.py` | FastAPI factory, bearer auth, routes, static UI |
| Tunnel | `tunecast/tunnel.py` | ngrok subprocess, bind check via local API, restart loop |
| Supervisor | `tunecast/boot.py` | Boot sequence, sidecar process, readiness state, exit codes |
| UI | `tunecast/static/index.html` | Single page, vanilla JS |
| Client | `client/generate.py` | Submit, poll, save; end-to-end proof |
| Image | `Dockerfile`, `docker/requirements.txt`, `.dockerignore` | Digest-pinned base, four exact-pinned additions on system Python, ngrok binary by sha256, tini entrypoint |
| CI | `.github/workflows/docker.yml` | Build and push on `main` / `v*` |

## Data flow: one generation

1. `POST /jobs` → auth → pydantic validation → `JobStore.create` (status `queued`) → `JobRunner.submit(id)` → 202 with job JSON.
2. Worker thread pops the id, `JobStore.update(status="running", started_at=…)`, builds the sidecar payload (`input`, `instructions`, `seed`, `max_new_tokens = duration_s × 25`, `response_format`, `stream=false`), POSTs to `http://127.0.0.1:8000/v1/audio/speech`.
3. Response bytes written to `outputs/<id>.wav`; `timings`, `gpu` captured; status `succeeded`. Any failure → status `failed`, `error` filled.
4. `GET /jobs/{id}` returns the row plus computed `queue_position` (jobs ahead: running + queued earlier) and `progress` (estimate, capped 0.95 while running).
5. `GET /jobs/{id}/audio` streams the file with `Content-Disposition: attachment`.
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
    def generate(self, params: GenerateParams, out_path: Path) -> None: ...   # raises SidecarError
    def describe(self) -> dict: ...

# jobs.py
class JobStore:  create(params) -> Job; get(id) -> Job | None; list(limit) -> list[Job]
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

- No request except `/health`, `/ready`, and `/` succeeds without the bearer key.
- `POST /jobs` never waits on inference.
- `/ready` is 200 only while both the sidecar and the tunnel are up.
- A job that stops running for any reason ends `succeeded` or `failed`, never stuck.
- Nothing under `/workspace` is required to exist at boot; boot creates what it needs.
- Secrets never appear in logs, the image, or the repo.
- The image contains no model weights.

## Directory map

```
Tunecast/
├── tunecast/            application package (see Components)
│   └── static/index.html
├── tests/               pytest, stub mode only
├── client/generate.py
├── docker/              requirements.txt: additions on top of the base image
├── docs/               gitignored: superpowers/{specs,plans}/
├── Dockerfile
├── pyproject.toml, uv.lock
├── .github/workflows/docker.yml
├── TASK.md, DECISIONS.md, ARCHITECTURE.md, EXPLANATION.md, README.md
└── CLAUDE.md
```
