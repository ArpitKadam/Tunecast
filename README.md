# Tunecast

Self-hosted [MiniMax Music 3](https://huggingface.co/MiniMaxAI/MiniMax-Music3) song
generation on a RunPod GPU pod: one Docker image, an authenticated async REST API,
a one-page web UI, and a stable public URL through ngrok. Built for a workflow of
"start a pod, make a few songs, terminate it".

Powered by **MiniMax-Music3**. Licence: MiniMax-Music3 Community License. Commercial
use is allowed; the UI must display "MiniMax-Music3" (it does); above US$20M/yr
revenue you need written authorisation from MiniMax; a hosted service must block the
licence's Exhibit A prohibited uses. Read the licence before serving other people.

## How it fits together

```
browser / client/generate.py
   │  HTTPS + Bearer key
   ▼
ngrok  sliding-ethically-beckham.ngrok-free.dev
   │
   ▼  RunPod pod, 2×L40S
tunecast.boot (PID 1 under tini)
   ├─ FastAPI :8080   auth, job queue, WAV storage, UI, /ready
   │     └─ HTTP ──▶ sgl-omni serve :8000 (official inference server)
   │                  GPU 0: Qwen3-8B autoregressive stage
   │                  GPU 1: flow-matching + waveform decode
   ├─ ngrok agent (restarted with backoff if it drops)
   └─ /workspace (pod volume disk): weights 57.4 GB, outputs, jobs.db, logs
```

- Weights (`MiniMaxAI/MiniMax-Music3`, revision `fbdf52fb…`, 57.4 GB) download on the
  first boot of every fresh pod and are verified by a size manifest. They are never
  inside the image.
- Outputs live only for the pod's lifetime. Save what you want to keep before you
  terminate.
- The image is `arpitkadam/tunecast` on Docker Hub, built by GitHub Actions from
  this repo, pinned to a base image digest and exact package versions.

## One-time setup

1. **Docker Hub**: create an access token (read/write). Add it to this GitHub repo as
   secrets `DOCKERHUB_USERNAME` and `DOCKERHUB_TOKEN`. Every push to `main` then
   builds and pushes `arpitkadam/tunecast:latest` and an immutable
   `arpitkadam/tunecast:sha-<7>` tag.
2. **ngrok**: you need the agent **authtoken** (dashboard → "Your Authtoken") and the
   static dev domain. The REST API key is a different thing and will not start a
   tunnel.
3. **API key**: pick a long random string; it is the only credential for the API and
   UI.
4. **RunPod template**: create it once with the values below.

### RunPod template

| Setting | Value |
| --- | --- |
| Container image | `arpitkadam/tunecast:latest` (or a `sha-…` tag to pin) |
| GPU | 2 × NVIDIA L40S (48 GB each) |
| Container disk | 20 GB |
| Volume disk | 80 GB, mount path `/workspace` |
| Expose HTTP ports | `8080` |
| CUDA filter | the image is CUDA 13.0.3 and its base declares `cuda>=13.0, driver>=535`. Select a host offering CUDA 13.0 or newer. Not yet confirmed on real hardware (Task 7) |
| Start command | leave empty (image entrypoint) |

Environment variables on the template:

| Name | Value |
| --- | --- |
| `TUNECAST_API_KEY` | your key (required) |
| `NGROK_AUTHTOKEN` | ngrok agent authtoken (required) |
| `NGROK_DOMAIN` | `sliding-ethically-beckham.ngrok-free.dev` (required) |
| `HF_TOKEN` | optional, lifts Hugging Face rate limits |
| `TUNECAST_MAX_CONCURRENT` | optional, default `1` |
| `TUNECAST_KEEP_LAST` | optional, default `200` |
| `TUNECAST_DIT_STEPS`, `TUNECAST_DIT_CFG_SCALE` | optional sidecar knobs, leave unset |

`.env.example` documents every variable and every baked constant.

Why a volume disk and not a network volume: usage is a few songs a month, and a
network volume costs about US$7/month idle for 100 GB, while re-downloading 57 GB at
datacenter speed costs a few minutes of pod time. See `DECISIONS.md`.

## Per-session spin-up

1. Deploy a pod from the template. Watch the pod logs; every boot stage is a JSON
   line (`env_ok`, `weights_download_start`, `weights_ready`, `sidecar_ready`,
   `api_bound`, `tunnel_ready`, `api_ready`).
2. When `api_ready` appears, open `https://sliding-ethically-beckham.ngrok-free.dev/`.
   ngrok's free plan shows an interstitial once per browser; click "Visit Site".
3. Enter the API key once (stored in that browser only). Fill lyrics and style, pick a
   length, start the take. The reel shows queue position, an estimated progress
   meter, then a player and "Save WAV".
4. Save your WAVs. Terminate the pod. Everything on `/workspace` is gone after that.

`GET /ready` answers 200 only when both the model and the tunnel are up, 503
otherwise. `GET /health` is liveness only.

## API

All routes except `/health`, `/ready` and `/` need `Authorization: Bearer <key>`.
Add `ngrok-skip-browser-warning: 1` to skip ngrok's interstitial on non-browser
clients.

| Method | Path | What |
| --- | --- | --- |
| POST | `/jobs` | body `{"lyrics","description","duration_s":180,"seed":null,"format":"wav"}` → 202 with the job |
| GET | `/jobs?limit=50` | newest first |
| GET | `/jobs/{id}` | status, `queue_position` (jobs ahead), `progress` (estimate), `timings`, `gpu`, `error` |
| GET | `/jobs/{id}/audio` | the WAV (409 until succeeded) |
| DELETE | `/jobs/{id}` | 204; 409 while running |
| GET | `/info` | model, revision, sidecar, GPUs, limits |

Limits: lyrics ≤ 20 000 chars, description ≤ 10 000, duration 1–360 s (the model's
9000-frame cap at 25 fps), seed 0–2³¹−1. Output is 32 kHz 16-bit stereo WAV
(≈ 7.7 MB per minute). These are the parameters the official inference server
accepts; sampling temperature, top-p/top-k and reference audio are rejected by it.

## Client script

```bash
python client/generate.py --url https://sliding-ethically-beckham.ngrok-free.dev \
  --key "$TUNECAST_API_KEY" --lyrics-file lyrics.txt \
  --description "warm acoustic pop, intimate female vocals, fingerpicked guitar" \
  --duration 180 --seed 7 --out song.wav
```

Prints queue position and progress, saves the file, exits 0. Exit 1 means the
server reported a failed job (message printed), exit 2 means auth or transport
failure. Standard library only, runs on any Python 3.

## Local development (no GPU)

```bash
uv sync --group dev
uv run pytest -q
TUNECAST_STUB=1 NGROK_ENABLED=0 TUNECAST_API_KEY=dev uv run python -m tunecast.boot
```

Stub mode swaps the inference server for a generator that writes a 400 Hz sine
of the requested length, so the API, queue, UI and client run without CUDA. With
`NGROK_ENABLED=1` and your ngrok variables in `.env` the real tunnel comes up too.
On Windows use `TUNECAST_PORT=8090` if something else owns 8080.

## Measured numbers

Filled in from the real pod run (Task 7). Until then these are unmeasured.

| Metric | Value |
| --- | --- |
| Fresh pod: boot → api_ready, including the 57.4 GB download | to be measured |
| Model load (sidecar_start → sidecar_ready) | to be measured |
| Inference time, 180 s song | to be measured |
| GPU memory per card during a job | to be measured |
| Cost per 180 s song at US$1.98/h | to be measured |

Image build on GitHub Actions: 14 min 16 s, 14.84 GB compressed, first run.

## Limitations

- ngrok free plan: 1 GB transfer per month. A 3-minute WAV is ~23 MB, so roughly
  40 downloads a month. The UI only fetches audio when you click "Load audio".
- The interstitial page cannot be removed on the free plan.
- Progress while a job runs is an estimate; the inference server does not stream.
- Outputs are not persisted beyond the pod. Prune keeps the last 200 succeeded
  jobs within a session.
- Weight integrity is checked by file sizes plus a marker, not by hashing.
- One shared key, no per-user accounts, no rate limiting.

## Teardown

Terminate the pod. Nothing else bills: the template is free, the image lives on
Docker Hub, the ngrok domain is free-tier. To remove everything: delete the RunPod
template, the Docker Hub repository, and the GitHub secrets.

## Serverless note

RunPod serverless would cut idle cost to zero between requests, but every cold
worker would either re-download 57 GB or need a network volume (the monthly cost
this design avoids). For a few songs a month, a pod you terminate is cheaper.

## Boot exit codes

1 bad environment (missing `TUNECAST_API_KEY` or ngrok vars), 2 weights could not
be completed, 3 the inference server never answered, 4 the tunnel never bound,
5 the inference server died, 6 port 8080 was already taken. Every failure also
logs a JSON line with the reason.
