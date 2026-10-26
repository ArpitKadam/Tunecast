# TASK: Self-hosted MiniMax Music 3 song-generation server on RunPod

## Goal

Build a reproducible, pre-baked Docker image that runs MiniMax Music 3 (song generation) on a RunPod GPU pod, exposes it as an authenticated REST API plus a minimal web UI, and tunnels it to a stable public URL via ngrok. Every pod spin-up must be fast and require no manual setup beyond starting the pod with the right template and environment variables.

This document is a specification. It deliberately does not prescribe the implementation order or process — decide that yourself. Plan and explain before writing code.

## Context and constraints

- The pod cannot be kept running continuously. Pod-local storage is ephemeral; nothing that lives only on the pod's disk can be relied on between sessions.
- The base RunPod image has essentially nothing installed except PyTorch. All Python and system dependencies must be baked into the Docker image.
- Model weights must live on a RunPod network volume, mounted at pod start, never inside the image and never re-downloaded on every boot. First-run population of the volume must be handled (idempotent: download only if absent, verify integrity).
- Target hardware: 2× NVIDIA L40S (48 GB VRAM each, ~$1.98/hr). The server must actually make use of the available VRAM (model sharding or placement across both GPUs if a single card is insufficient; otherwise justify single-GPU use and treat the second card as headroom/batching capacity).
- The RunPod account balance is small. Design for minimal idle cost: fast startup, no unnecessary background work, clear shutdown behaviour. Cost per generation and cold-start time are first-class metrics to report.
- Weights and inference code come from the official MiniMax open-weights release on Hugging Face and the accompanying official GitHub inference code. Locate and verify the exact repository IDs, revision/commit, and licence before anything else. Do not assume model architecture, parameter count, or dependency set — read them from the source. If the licence restricts commercial use or redistribution, surface that explicitly.
- ngrok: a static dev domain already exists — `sliding-ethically-beckham.ngrok-free.dev` — along with an ngrok auth token/API key. The tunnel must bind to this domain so the client-side URL never changes between pod sessions.

## Deliverables

1. **Dockerfile + build context** producing a single image published to Docker Hub (repository name and tag scheme are yours to choose; tags must be immutable and the `latest` tag must track the most recent verified build). The image must be pullable by RunPod without extra credentials, or the private-registry configuration must be documented.
2. **Server application** inside the image:
   - REST API (JSON) covering at minimum: submit a generation job, poll job status/progress, list recent jobs, download the resulting audio, health/readiness check, and a model/GPU info endpoint.
   - Generation must be asynchronous — a submit call returns immediately with a job ID; long-running inference never blocks the HTTP worker or the tunnel.
   - Every request except health checks is protected by a shared API key passed as a bearer token. The key is supplied via an environment variable on the pod; missing key at startup is a hard failure, not an open server.
   - A simple web UI served from the same process: enter prompt/lyrics/parameters, submit, watch progress, play and download the result. It must work through the ngrok domain over HTTPS and must send the API key (entered once, stored client-side).
   - Expose every generation parameter the official inference code supports (prompt, lyrics, duration, seed, sampling/guidance settings, reference audio if supported, output format) with sane defaults and validation.
   - Output audio persisted to the network volume with a retention policy, so results survive pod restarts.
   - Structured logging with timings per stage (model load, per-job inference, encode) and GPU memory usage.
3. **ngrok integration** inside the container: the tunnel starts automatically on boot, binds the static domain, restarts if it drops, and the server does not report ready until the tunnel is up. ngrok credentials and domain come from environment variables, never baked into the image.
4. **Startup/entrypoint logic** that: mounts/validates the network volume, populates weights if absent, loads the model onto the GPUs, starts the API, starts the tunnel, and exits non-zero with a clear message on any unrecoverable failure.
5. **RunPod template definition** (or equivalent documented configuration): image, GPU type, volume mount path, exposed ports, required environment variables, and container disk size.
6. **Client example**: a small script runnable from the local machine that submits a job, polls, and saves the audio — proves the end-to-end path through the tunnel.
7. **README** covering: one-time setup (Docker Hub, network volume creation, template creation, environment variables), per-session spin-up, expected cold-start time, measured generation time and cost per song on the target hardware, known limitations, and teardown.
8. **Verification evidence**: a real end-to-end run on the target pod — logs, timing numbers, and at least one generated audio file — not a dry run.

## Non-functional requirements

- Cold start (pod boot → API ready with model loaded, weights already on volume) should be measured and minimised; report the number and where the time goes.
- The image must build deterministically: pinned base image digest, pinned Python dependencies, pinned inference-code commit.
- No secrets in the image, the repo, or logs (API key, ngrok token, Hugging Face token).
- Graceful handling of concurrent submissions: queue them, never OOM the GPU; expose queue position in job status.
- Errors from inference must surface in job status with a useful message, not as a dead job.
- The whole thing must be runnable and testable locally (CPU or single GPU) with a flag that stubs or downsizes the model, so the API/UI/tunnel can be developed without burning pod credits.

## Out of scope

- Fine-tuning or modifying the model.
- Multi-user accounts, billing, or rate limiting beyond the single shared key.
- Serverless RunPod endpoints (pods only, for now — note if serverless would be materially cheaper and why).

## Open decisions (yours to make, but document the reasoning)

- Web framework, job queue mechanism, and process model.
- Whether to keep the model resident across jobs (assumed yes) and how to handle the second GPU.
- Docker Hub repository naming and whether it is public or private.
- Audio output format and default duration.
- Result retention policy on the volume.
- Container disk size and network volume size, with the numbers justified by actual weight and dependency sizes.

## Questions to raise before proceeding if unresolved

- Exact Hugging Face repo ID(s) and licence terms for MiniMax Music 3 — confirm before building.
- Whether the official inference code supports multi-GPU natively or needs adaptation.
- Whether the free-tier ngrok domain imposes bandwidth or connection limits that affect audio download.

---

## Amendments (2026-09-02, approved by owner)

These override the clauses above. Reasoning in `DECISIONS.md`; full design in
`docs/superpowers/specs/2026-09-02-tunecast-design.md`.

1. **Storage:** weights and outputs live on the pod's volume disk at
   `/workspace` (80 GB), not a network volume. Weights re-download on every
   fresh pod (idempotent, size-verified). No idle storage cost.
2. **Retention:** session lifetime. Outputs are lost on terminate; UI warns
   to download first. Keep-last prune guards the disk within a session.
3. **Build:** GitHub Actions builds and pushes `arpitkadam/tunecast`
   (public). Nothing is built on the laptop.
4. **Audio delivery:** WAV through the ngrok tunnel; the 1 GB/month free
   cap is accepted for 3–4 songs/month.
5. **Provisioning and verification:** owner creates the RunPod template and
   pod manually from the README and provides the run logs; the agent holds
   no RunPod credentials.
6. **Backend:** SGLang-Omni 0.1.4 sidecar (official path, native 2-GPU
   placement). Per-job parameters are the official surface only.

## Task backlog (gated: one task per "Go for Task n")

| # | Task | Status |
| --- | --- | --- |
| 1 | Foundation: project layout, settings, JSON logging, weights manifest + verify/download, tests | done 2026-09-02 |
| 2 | Sidecar clients (HTTP + stub), SQLite job store, worker queue, GPU info, FastAPI routes + auth, API tests | done 2026-09-03 |
| 3 | Supervisor `tunecast.boot`, ngrok tunnel control, readiness, local stub run with real tunnel | done 2026-09-03 |
| 4 | Web UI (single static page) served at `/`, manual check through tunnel | done 2026-09-03 |
| 5 | Dockerfile, GPU dependency lock, GitHub Actions workflow, first image on Docker Hub | done 2026-09-03 (run 33687738008, `sha-7ddf5a7`) |
| 6 | RunPod template (in README), client script, README, EXPLANATION entry | done 2026-09-03 |
| 7 | Pod verification run (owner-executed), measured numbers into README/EXPLANATION, fixes | done 2026-09-03 |

## Issue backlog (GitHub issues #1–#8, opened 2026-10-23)

Source: <https://github.com/ArpitKadam/Tunecast/issues>. All eight are open.
Task numbers continue from the table above; `#n` always means the GitHub
issue, never a task. Order is by dependency, reasoning in `DECISIONS.md`
(2026-10-26). Same gate as before: one task per "Go for Task n".

| # | Issue | Task | Depends on | Status |
| --- | --- | --- | --- | --- |
| 8 | [#3](https://github.com/ArpitKadam/Tunecast/issues/3) | PR CI gate (`pytest`, publish gated on it) + Playwright harness and regressions for the existing page | none | done 2026-10-26 (issue #3 stays open until Task 13 adds the tab tests) |
| 9 | [#4](https://github.com/ArpitKadam/Tunecast/issues/4) | Validate generated WAV; store real `audio_duration_s`, sample rate, channels, bytes | 8 | done 2026-10-26 |
| 10 | [#2](https://github.com/ArpitKadam/Tunecast/issues/2) | Idempotent submissions: client request ID, UNIQUE constraint, body fingerprint, reconcile by ID | 8 | done 2026-10-26 |
| 11 | [#6](https://github.com/ArpitKadam/Tunecast/issues/6) | Bounded queue admission (429/503 + `Retry-After`), queued-job cancel, sequential sweep default | 10 | done 2026-10-26 (no sweep feature exists; the rule for one is in `DECISIONS.md`) |
| 12 | [#5](https://github.com/ArpitKadam/Tunecast/issues/5) | Experiment lineage (`parent_job_id`, experiment ID, model revision, config snapshot), honest timings, JSON/CSV export | 9, 10 | done 2026-10-26 |
| 13 | [#1](https://github.com/ArpitKadam/Tunecast/issues/1) | Player and Lab tabs on one shared queue/player state | 9, 10, 11, 12 | todo |
| 14 | [#7](https://github.com/ArpitKadam/Tunecast/issues/7) | Visible network/auth state: offline/stale indicators, backoff, sign-out, remember-key choice | 13 | todo |
| 15 | [#8](https://github.com/ArpitKadam/Tunecast/issues/8) | Offline portfolio front door: screenshots, recording, sample, offline demo, README/runbook split | 13, 14 | todo |

### Acceptance criteria

**Task 8 (#3).** `.github/workflows/docker.yml` gains a `pull_request`
trigger and a test job (`uv sync --frozen --group dev` + `pytest`); the image
publish job `needs:` it. Playwright suite runs in CI against the stub sidecar
and covers: draft retention, audio loading, reconnect, lost submit response,
invalid key, keyboard navigation, mobile layout. Tab-specific cases (both
tabs, tab-switch playback) land with Task 13. Verify: a PR with a failing test
blocks publish.

**Task 9 (#4).** Sidecar response is validated as a WAV (header, non-zero
frames, length matches the data chunk) before a job is `succeeded`. Malformed,
truncated, empty and timed-out bodies fail the job with a clear error and
leave no partial file. Columns stored and returned by the API. Tests: malformed
200 (JSON body), empty body, truncated transfer, timeout, disk-write failure.
The UI shows actual duration, not the requested ceiling.

**Task 10 (#2).** `POST /jobs` accepts a client request ID persisted with a
UNIQUE constraint and a body fingerprint. Same ID + same body returns the same
job; same ID + changed body returns 409. UI and `client/generate.py` reuse the
ID on retry and, after a lost response, show "Submission status unknown,
checking" and reconcile by ID. Reproduce the lost-response case in a test
first.

**Task 11 (#6).** Atomic capacity check at admission (default cap 8 queued
jobs, env-configurable, `TUNECAST_`-prefixed like the other settings); full queue returns
429/503 with `Retry-After`; readiness reflects admission. Queued jobs can be
cancelled; running jobs cannot (sidecar has no cancel) and the UI must not
pretend otherwise. Seed sweeps default to sequential with an explicit job
count. Test: concurrent submits past the cap never exceed it.

**Task 12 (#5).** Jobs store `parent_job_id`, experiment ID, real duration,
model revision and config snapshot. UI shows seed, prompt diff, queue time,
inference time, real-time factor. Estimator learns from actual output length
with an uncertainty label; peak GPU memory is sampled during inference, not
after. JSON/CSV export of an experiment.

**Task 13 (#1).** Player: searchable takes, title/favorite, persistent
playback, download, generated duration. Lab: lyrics section editor,
structured style presets, seed lock, duplicate-and-edit, two-take A/B.
Switching tabs neither interrupts audio nor drops drafts. Stack choice
(refactor vanilla page vs React + TypeScript + Vite built to static assets)
is made at the start of the task and recorded in `DECISIONS.md`; either way
FastAPI, same-origin auth and current routes stay, and the HTML test that
forbids external assets is updated for local asset links. Adds the two-tab
Playwright cases deferred from Task 8.

**Task 14 (#7).** Polling and readiness failures are surfaced: offline,
reconnecting, last-updated, stale indicators; retry backoff; lamps cleared on
failed checks; explicit sign-out; remember-key is an opt-in choice (default
session-only).

**Task 15 (#8).** README opens with screenshots, a short two-tab recording, a
playable sample and an explicitly labelled offline demo using bundled
fixtures. Long operational commands move to a runbook with links kept. Dated
benchmarks stay; the one measured run is labelled as one run. No new
measurements: screenshots and recording come from the stub sidecar, the
playable sample is the existing `song-client.wav`, and anything not measured
is not presented as a benchmark.

### Owner rulings (2026-10-26)

- **#7 per-user access and quotas: skipped.** Out of scope (single shared
  key). Task 14 covers only network/auth state, sign-out and remember-key.
- **#6 queue limits: approved.** Read as whole-server queue capacity
  protection, not per-client rate limiting. Default cap 8 queued jobs,
  configurable by env var.
- **#8 measurements: no new pod runs.** Task 15 uses existing artifacts and
  stub-rendered screenshots only; it does not block on a pod session.
