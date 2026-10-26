# Decisions

Append-only, newest first. Each entry records what was chosen, what else was
on the table, and the evidence that settled it.

## 2026-10-26 — Queue cap, 429/503 admission, cancel = delete a queued row
- **Decision:** `TUNECAST_MAX_QUEUED` (default 8, positive int, invalid values rejected at startup) caps jobs in `queued`; running jobs do not count. The count, the idempotency-key check and the insert share one lock hold in `JobStore.create`. A full queue returns 429 with fixed `Retry-After: 30` and nothing is created. Submits return 503 + `Retry-After` while `ReadyState.model` is false. A replay of an existing `Idempotency-Key` bypasses both. Deleting a `queued` job cancels it (row removed; the worker skips ids that are gone or no longer queued, and claims with a conditional update); deleting a running job is 409 with a message that it cannot be cancelled; finished jobs delete as before. The UI treats a 5xx carrying `Retry-After` as a definitive refusal and skips Task 10's reconcile; ngrok 502/504 carry no such header and still reconcile.
- **Alternatives considered:** `queue.Queue(maxsize)` (drifts from the DB after restarts and cancels); an app-layer count before `create` (racy); counting running jobs too (cap 8 would mean 7 waiting); a live `sidecar.ready()` probe per submit (blocking, up to 10 s, and could 503 every submit while a take generates if `/health` stalls); a `cancelled` status (touches the schema); `Retry-After` from the estimator (a guess); building seed-sweep code (no sweep exists).
- **Why:** The DB is the source of truth and one lock hold removes the check-then-insert race (tested with 16 store threads and 12 API threads at the boundary). `state.model` is set by `boot.py` only after the sidecar is ready and the API starts serving only after that, so it is cheap and never blocks. The cap protects against an accidental paid-GPU backlog; it is whole-server back-pressure, not per-client rate limiting.
- **Consequences:** Rule for any future seed sweep: submit one job at a time with an explicit count, honour 429 and `Retry-After`; `TUNECAST_MAX_CONCURRENT` stays 1. `state.model` never turns false after boot, so a sidecar that dies mid-run is caught by boot's exit code 5, not by this guard. `/ready` is unchanged (a full queue is back-pressure, not unreadiness). The CLI does not honour `Retry-After` (429 is a 4xx and fails fast; 503 is retried at a fixed interval). Cancelled ids stay in the in-memory queue until popped.

## 2026-10-26 — Experiment lineage, honest timings, peak GPU
- **Decision:** Jobs store `parent_job_id`, `experiment_id`, `model_revision` and a `config_snapshot` JSON (named non-secret fields only). A root job's experiment is its own id, a child inherits its parent's, an explicit value wins; old rows are backfilled to their own id. Lineage fields join the Task 10 fingerprint only when set. Peak GPU memory is sampled by a ~1 s thread during `generate` and stored in the existing `gpu` column (`peak_used_mb`). `real_time_factor` = inference seconds ÷ real audio seconds. The estimator learns seconds per output second from `audio_duration_s`, adds a lyrics-length term only after 3 early-ending runs, and reports `confidence` low or medium, never high. Export is `GET /experiments/{id}/export?format=json|csv`. The page gets a "Refine" button (fills the form from a take and submits with `parent_job_id`), the only UI path that creates children, so the prompt diff and lineage can be exercised and tested.
- **Alternatives considered:** null experiment on roots (children would never group with the parent without mutating it); a separate `peak_gpu_mb` column (duplicates data); an always-on lyrics regression (the stub always fills the ceiling, so the data would not support it); validating the parent in the route (would turn a replay after the parent is deleted into a 422); a plain `<a href>` export (cannot send the bearer header).
- **Why:** Meets issue #5 with no new dependency and keeps Task 10 replay semantics. The estimator and confidence label avoid presenting a guess as a measurement.
- **Consequences:** Peak GPU is whole-GPU `nvidia-smi` usage: it includes resident weights and any concurrent job. The estimator is in memory, so confidence resets to low after a pod restart. CSV cells are written verbatim, so a lyric starting with `=`, `+`, `-` or `@` is a formula in Excel; JSON is lossless and the exposure is one shared key. No UTF-8 BOM, so Excel may garble non-ASCII lyrics. Task 13 replaces the page, including Refine; keep the behaviour (duplicate-and-edit with lineage).

## 2026-10-26 — Idempotent submits via `Idempotency-Key`
- **Decision:** `POST /jobs` takes an optional `Idempotency-Key` header. The key and a sha256 fingerprint of the validated body are stored on the job row (nullable `request_id` under a UNIQUE index, `request_hash`). A repeat with the same body returns 200 and the existing job; a changed body returns 409; a new key returns 202. `GET /jobs?request_id=` returns `[job]` or `[]` for reconciliation. The UI and CLI generate one key per attempt and reuse it on retry; the UI treats a 5xx like a lost response and reconciles instead of showing "Not started". This supersedes "the initial submit stays single-shot" in the 2026-09-03 client-retry entry: the CLI now retries submit on transport errors and 5xx with the same key, and still never retries 4xx.
- **Alternatives considered:** the key in the request body (pollutes the model and the fingerprint); an in-memory dedupe cache (lost on restart, racy); check-then-insert (racy); `GET /jobs/by-request/{id}` (404 conflates "none" with a wrong URL); clearing the pending key on "not found" (false negatives while the original POST is in flight).
- **Why:** The UNIQUE index plus IntegrityError handling is the only arbiter, so concurrent identical submits create one job (tested with 8 simultaneous requests). The fingerprint is taken before the random seed is filled, otherwise a seedless retry gets a new seed and a false 409. Replay is 200 because nothing new was accepted. The ngrok tunnel can return 502/504 after the server committed, so a 5xx must not clear the key.
- **Consequences:** Idempotency lasts as long as the job row; a delete or prune frees the key. A failed job replays as failed, so a re-run needs a new key. Old rows have no key. Revisit if jobs are ever pruned faster than clients retry.

## 2026-10-26 — Browser regression suite: Playwright for Python, gaps as strict xfail
- **Decision:** `tests/e2e/` uses pytest-playwright against the real FastAPI app plus `StubSidecar` on a free local port. Tests carry an `e2e` marker, excluded by default with `addopts = -m 'not e2e'`; CI runs them as a separate step. The workflow's `build` job `needs: test` and is skipped on `pull_request`. UI behaviour the issues want but the page lacks is written as `xfail(strict=True)` with a Task reference.
- **Alternatives considered:** Playwright for Node (second toolchain); mocking the API with `page.route` (tests the mock, not the app); asserting current buggy behaviour as correct; running e2e inside the default `pytest` run.
- **Why:** Python keeps one toolchain. A real server makes reconnect and lost-response tests genuine; routes are used only to drop a response the server already accepted. Strict xfail turns each known gap into a test that fails the moment it is fixed, forcing removal of the marker. Excluding e2e by default keeps `pytest` fast and browserless.
- **Consequences:** Tasks 10 and 14 must remove their xfail markers or CI reports XPASS failures. The e2e step adds about 60 s plus chromium install to CI. The CI gate itself is unproven until a real PR runs it. Revisit if selectors in `index.html` change (Task 13).

## 2026-10-26 — Validate audio in the sidecar client, with a hand-written WAV parser
- **Decision:** `HttpSidecar` validates the response body in memory with `inspect_wav` (a `struct` parser for PCM, IEEE float and EXTENSIBLE) before the atomic write, and `generate` returns `AudioInfo`. Duration, sample rate, channels and bytes go into four nullable columns, added to old databases with `PRAGMA table_info` + `ALTER TABLE ADD COLUMN`.
- **Alternatives considered:** validating in `JobRunner` after the file is written; stdlib `wave`; `soundfile`/`mutagen`; a migration framework; storing audio info inside the `timings` JSON; validating the file on disk after writing.
- **Why:** The reproduced bug was a mocked 200 JSON body saved as `.wav` and shown green. Validating before the write means invalid output never exists on disk. `wave` rejects float and EXTENSIBLE WAV, and the other options add a dependency or a table for four columns. A real column is queryable, which Task 12 needs. The real pod output `song-client.wav` (149.188 s, 32 kHz stereo) validates.
- **Consequences:** The whole body is held in memory (about 46 MB at 360 s, as before). A data chunk that declares fewer bytes than were received is accepted (trailing chunks are legal), so `bytes` can exceed the declared audio size. Revisit if the sidecar streams or returns other formats.

## 2026-10-26 — Scope rulings on the GitHub issue backlog
- **Decision:** (1) #7's per-user access and quotas are not built. (2) #6's queue cap is built: whole-server limit, default 8, env-configurable. (3) #8 adds no new measurements.
- **Alternatives considered:** per-user accounts for a public deployment; no admission limit; new pod runs for multi-prompt/seed benchmarks.
- **Why:** Original spec puts multi-user and per-client rate limiting out of scope, and usage is about 3–4 songs a month on one shared key. A queue cap is protection against accidental paid-GPU backlog, not per-client throttling. New measurements cost pod credits and the owner runs the pod by hand.
- **Consequences:** The app stays single-key; do not share the URL broadly. Revisit if it goes public. The portfolio shows one measured run, labelled as such.

## 2026-10-26 — Order of the GitHub issue backlog
- **Decision:** CI gate (#3) → audio validation (#4) → idempotency (#2) → queue bounds (#6) → lineage (#5) → tabs UI (#1) → network/auth state (#7) → portfolio (#8), as Tasks 8–15 in `TASK.md`. One task per gate.
- **Alternatives considered:** Issue-number order; UI first (#1) because it is the most visible; merging #7 into #1 since both rewrite `static/index.html`.
- **Why:** Issues #2, #5 and #6 each say they must precede reruns, sweeps or batches, and #5 needs the real duration from #4. The tabs UI consumes all four, so it goes after them. #3 goes first so every later change runs under a test gate. #7 follows #1 because #1 extracts the page JS/CSS; doing #7 first would mean rewriting it twice. #8 needs the final UI for screenshots. #3's tab-specific browser tests cannot exist before #1, so that part moves into Task 13.
- **Consequences:** The UI work waits behind four backend tasks. Revisit if the owner wants a visible UI change sooner; Tasks 9–12 do not depend on the UI.

## 2026-09-03 — Client retries transport failures, not 4xx
- **Decision:** `client/generate.py` retries transport errors and 5xx responses five times, three seconds apart, while polling and downloading. It does not retry 4xx, and the initial submit stays single-shot. On giving up it prints the job URL.
- **Alternatives considered:** failing fast on any error (the original behaviour); retrying everything including submit; an exponential backoff.
- **Why:** The free ngrok tunnel dropped the polling connection twice during real three-minute jobs. The job itself keeps running on the pod, so aborting the client throws away paid GPU work for a blip that resolves in seconds. Retrying a submit risks queueing the same song twice; retrying a 401 or a 422 cannot succeed.
- **Consequences:** A genuinely dead server now takes about 15 seconds to report instead of failing immediately. Acceptable for a tool that already waits minutes.

## 2026-09-03 — Install onto the base image's system Python, not an isolated venv
- **Decision:** The Dockerfile adds `sglang-omni==0.1.4`, `fastapi`, `uvicorn`, `huggingface_hub` (all exact-pinned in `docker/requirements.txt`) to the base image's system Python with `uv pip install --system`. Every build writes the fully resolved set to `/app/docker/installed.txt` and CI prints it.
- **Alternatives considered:** A fresh `/opt/venv` from a cross-platform `uv pip compile` lock (the original plan); `--system-site-packages` venv.
- **Why:** Reading the base image's build history showed it is `lmsysorg/sglang:v0.5.18` with `sgl-kernel 0.4.6.post1`, flashinfer 0.6.17 (cubins), torch 2.13 and sglang-omni's dependency set already in system Python, plus UCX built from source. A venv resolved from PyPI lacks `sgl-kernel` at that version (not on PyPI; sglang's bare PyPI metadata does not depend on it) and would fail at runtime, while duplicating ~7 GB of CUDA wheels. The digest pin freezes the base's package set; the four additions are exact-pinned.
- **Consequences:** Transitive packages that the base lacks and sglang-omni 0.1.4 needs resolve at build time; `installed.txt` records them. If a rebuild ever drifts, promote `installed.txt` to a `--no-deps` lock. Revisit when the base image changes.

## 2026-09-03 — `tini` as container entrypoint, Python handles SIGTERM itself during boot
- **Decision:** The Dockerfile (Task 5) runs `tini -- python -m tunecast.boot`. `Supervisor.run()` installs SIGTERM/SIGINT handlers on entry so a stop during the sidecar or tunnel wait ends boot promptly with exit 0.
- **Alternatives considered:** Python as bare PID 1 (ignores SIGTERM until uvicorn installs handlers; never reaps orphaned `sgl-omni` workers); a bash entrypoint with `exec`; supervisord.
- **Why:** `sgl-omni` is multi-process. If its parent dies, its workers are re-parented to PID 1 and only an init that reaps (tini) clears them. The Python-side handler covers the 30-minute model-load window that uvicorn's handler does not.
- **Consequences:** One extra apt package in the image. Signal path: tini → python (handler sets stop) → uvicorn/children stopped in `finally`.

## 2026-09-02 — Weights integrity check is size manifest + marker, not sha256
- **Decision:** Boot verifies weights by comparing every file's size against a committed manifest (88 entries from the HF API at the pinned revision) and by a `.tunecast_complete` marker containing the revision. No full-file hashing.
- **Alternatives considered:** sha256 of all 57 GB on every boot; trusting `snapshot_download` alone.
- **Why:** Hashing 57 GB costs minutes of paid pod time on every boot. `snapshot_download` already verifies per-file hashes during download; the size check catches truncated or partial trees, which is the realistic failure.
- **Consequences:** Silent bit-flips on the volume disk are not detected. Revisit if a corrupt-weights failure is ever observed.

## 2026-09-02 — Defaults: 180 s duration, WAV only
- **Decision:** `duration_s` defaults to 180, max 360 (model cap of 9000 frames at 25 fps). Output format is `wav` only.
- **Alternatives considered:** MP3/Opus transcoding via ffmpeg to reduce tunnel bandwidth.
- **Why:** The official sidecar emits WAV only; the user chose WAV over the tunnel and accepts the ngrok 1 GB/month cap (~40 downloads, expected 3–4). Transcoding would add a dependency for no requested value.
- **Consequences:** ~23 MB per 3-min song over the tunnel. Revisit if monthly volume grows past ~30 songs.

## 2026-09-02 — Sequential job queue, default concurrency 1
- **Decision:** In-process worker threads consume a `queue.Queue`; `TUNECAST_MAX_CONCURRENT` defaults to 1, capped at 4.
- **Alternatives considered:** Forwarding all submissions to the sidecar immediately (it supports 16 concurrent requests); Celery/Redis.
- **Why:** Spec requires "never OOM the GPU" and a visible queue position. One job at a time makes both trivial and matches the usage profile (3–4 songs, sequential). External queues add processes and dependencies for nothing.
- **Consequences:** Second GPU is used by the pipeline's acoustic stage, not by parallel jobs. Raise the env var to batch if throughput ever matters.

## 2026-09-02 — Single Python supervisor as PID 1
- **Decision:** `tunecast.boot` validates env, ensures weights, spawns `sgl-omni` and `ngrok` as subprocesses, runs uvicorn in-process, and supervises children.
- **Alternatives considered:** supervisord; a bash entrypoint plus separate service scripts; running the API inside the sgl-omni process.
- **Why:** One process owns readiness state, restarts ngrok with backoff, and can exit non-zero on unrecoverable failure so RunPod surfaces it. No new dependency; ~150 lines.
- **Consequences:** If the supervisor crashes, the pod restarts. Acceptable.

## 2026-09-02 — Audio delivered as WAV over the ngrok tunnel
- **Decision:** `/jobs/{id}/audio` streams the WAV through ngrok. No RunPod-proxy fallback in the UI.
- **Alternatives considered:** Compressed audio over the tunnel; audio via RunPod's per-pod proxy URL; paid ngrok plan.
- **Why:** User decision. Expected 3–4 songs/month ≈ 92 MB against a 1 GB/month cap.
- **Consequences:** ~40 downloads/month ceiling. README states it.

## 2026-09-02 — Image built by GitHub Actions, pushed to public `arpitkadam/tunecast`
- **Decision:** `.github/workflows/docker.yml` builds on push to `main` and `v*` tags, pushes `sha-<7>` and `latest` tags, uses Docker Hub as layer cache.
- **Alternatives considered:** Build on the laptop (Docker Desktop); build inside a RunPod pod (no Docker daemon there, needs buildah/kaniko).
- **Why:** User refused to download CUDA wheels to the laptop. Actions is free, reproducible, and off-laptop. In-pod builds still have to push to Hub, so they are strictly harder.
- **Consequences:** Needs `DOCKERHUB_USERNAME` / `DOCKERHUB_TOKEN` repo secrets. Runner disk is a risk with a 14.8 GB compressed base; measured on the first run, fallback is buildah on a RunPod CPU pod.

## 2026-09-02 — Pod volume disk instead of network volume
- **Decision:** Weights and outputs live on the pod's volume disk at `/workspace` (80 GB). Weights re-download on every fresh pod. Overrides the `TASK.md` constraint.
- **Alternatives considered:** RunPod network volume (spec default); stopping instead of terminating the pod.
- **Why:** Usage is ~once a month. Network volume idles at ~US$0.07/GB/month (≈ US$7 for 100 GB). A 57 GB re-download at datacenter bandwidth costs 3–8 min of a US$1.98/h pod (≈ US$0.10–0.26). Stopped pods bill volume storage higher and are pinned to one host.
- **Consequences:** Cold start on a fresh pod includes the download. Retention becomes session-lifetime; UI warns to download before terminating.

## 2026-09-02 — SGLang-Omni sidecar as the inference backend
- **Decision:** Run the official `sgl-omni serve` (sglang-omni 0.1.4) as a localhost subprocess; the FastAPI app fronts it with auth, queue, storage, and UI.
- **Alternatives considered:** Diffusers `ModularPipeline` in-process (needs an unmerged diffusers commit, single GPU); ComfyUI.
- **Why:** The model card names SGLang-Omni as the serving path; its `minimax_music3` module places the AR stage on GPU 0 and the acoustic stage on GPU 1 automatically, which uses both L40S as MiniMax designed. Zero model code to own. Stub mode swaps the HTTP client for a fake.
- **Consequences:** Per-job parameters are limited to the official surface (lyrics, description, seed, duration, format). Two processes in the container. Image carries the full sglang stack.

## 2026-09-02 — Base image pinned by digest: `hongccc/sglang-omni:dev`
- **Decision:** `FROM hongccc/sglang-omni:dev@sha256:02a85f00438c901c72a2eb2ef738974a807f63af3d13084445604f3344067b19`.
- **Alternatives considered:** `nvidia/cuda` base plus building flash-attn-4 and UCX 1.20 from source; waiting for an official `sgl-project` image.
- **Why:** sglang-omni's install docs name this image as the recommended path and it ships the flash-attn-4/UCX prerequisites that would otherwise take hours to compile and are impossible on a free GitHub runner. The digest makes the build deterministic despite the mutable `dev` tag.
- **Consequences:** Availability depends on a personal Docker Hub namespace. Revisit when an official image exists or if the digest disappears.
