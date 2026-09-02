# Decisions

Append-only, newest first. Each entry records what was chosen, what else was
on the table, and the evidence that settled it.

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
