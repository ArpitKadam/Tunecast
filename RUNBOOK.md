# Tunecast runbook

Operational commands for the RunPod pod: template, per-session spin-up, the
verification commands used on the real pod, job recovery, troubleshooting and
teardown. The overview is in [`README.md`](README.md); the design is in
[`ARCHITECTURE.md`](ARCHITECTURE.md).

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
| Volume disk | 80 GB, mount path `/workspace` (see the warning below) |
| Expose HTTP ports | `8080` |
| CUDA filter | the image is CUDA 13.0.3. Verified working on a host with driver 580.178.04 |
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
| `TUNECAST_MAX_QUEUED` | optional, default `8`; jobs allowed to wait in `queued` (a running job does not count); at least 1 |
| `TUNECAST_DIT_STEPS`, `TUNECAST_DIT_CFG_SCALE` | optional sidecar knobs, leave unset |

`.env.example` documents every variable and every baked constant.

**Do not put the 80 GB on container disk.** RunPod's two disk fields are easy to
transpose. Container disk is temporary and is wiped when the pod stops; volume
disk is what actually gets mounted at `/workspace` and survives a stop. With
volume disk at 0 the mount path is ignored, the weights land on temporary
storage, and stopping the pod costs a full re-download.

Why a pod volume disk and not a network volume: usage is a few songs a month, and
a network volume costs about US$7/month idle for 100 GB, while re-downloading the
weights takes under a minute of pod time (57.2 s, measured in the
[README](README.md#one-measured-run-2026-09-03)). See `DECISIONS.md`.

## Per-session spin-up

1. Deploy a pod from the template. Watch the pod logs; every boot stage is a JSON
   line (`env_ok`, `weights_download_start`, `weights_ready`, `sidecar_ready`,
   `api_bound`, `tunnel_ready`, `api_ready`).
2. When `api_ready` appears, open `https://sliding-ethically-beckham.ngrok-free.dev/`.
   ngrok's free plan shows an interstitial once per browser; click "Visit Site".
3. Enter the API key once (stored in that browser only). Fill lyrics and style, set
   the maximum length, start the take. The reel shows queue position, an estimated
   progress meter, then a player and "Save WAV". The song ends when the lyrics run
   out, so length is a ceiling rather than a target.
4. Save your WAVs. Terminate the pod. Everything on `/workspace` is gone after that.

`GET /ready` answers 200 only when both the model and the tunnel are up, 503
otherwise. `GET /health` is liveness only.

## Pod runbook

Every command below was used to bring up and verify the real pod (the example-file
paths now point at the English pair in `client/`; the first run used a Hindi pair).
Pod-terminal commands run in RunPod's web terminal (Connect tab → **Enable web
terminal**); the rest run on your machine.

### 1. Watch the boot

```bash
while true; do
  clear
  du -sh /workspace/models/MiniMax-Music3 2>/dev/null || echo "weights: downloading"
  python3 -c 'import json
for l in open("/workspace/tunecast/logs/boot.jsonl"):
    r = json.loads(l)
    print(r["event"], r.get("since_boot_s",""), r.get("was",""), r.get("now",""), r.get("error",""))' 2>/dev/null
  sleep 30
done
```

Healthy sequence, with the timings measured on 2 × L40S:

```
env_ok 0.0
weights_download_start
weights_download_done
weights_ready 57.2
cuda_visible_devices_pinned  None 0,1
sidecar_start 57.31
sidecar_ready 147.37
api_bound 147.39
tunnel_start
tunnel_ready 147.9
api_ready 147.9
```

Ctrl-C at `api_ready`.

### 2. Pre-flight checks

The web terminal is a **different process from the supervisor and does not share
its environment**, so read the API key out of the running process rather than
expecting `$TUNECAST_API_KEY` to be set:

```bash
PID=$(pgrep -f "[t]unecast.boot" | head -1)
KEY=$(python3 -c "import sys; env = open('/proc/' + sys.argv[1] + '/environ', 'rb').read().decode(); print(next(v for v in env.split(chr(0)) if v.startswith('TUNECAST_API_KEY='))[17:])" "$PID")

nvidia-smi --query-gpu=index,name,memory.used,memory.total --format=csv
curl -s http://127.0.0.1:8080/ready; echo
curl -s -H "Authorization: Bearer $KEY" http://127.0.0.1:8080/info | python3 -m json.tool | head -25
```

`/ready` must print `{"model":true,"tunnel":true}`. After the model loads, both
cards should show memory in use (measured: 25,283 MiB on GPU 0 and 13,947 MiB on
GPU 1). If GPU 1 is near zero, the two-stage placement did not take effect.

### 3. Cheap smoke test, inside the pod

Ten seconds of audio with no tunnel involved, so a failure here is the model and
not the network:

```bash
cat > /tmp/req.json <<'JSON'
{"lyrics": "[Verse]\nchal diye hum", "description": "warm acoustic pop, female vocals, guitar", "duration_s": 10}
JSON

JOB=$(curl -s -X POST http://127.0.0.1:8080/jobs \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  --data-binary @/tmp/req.json \
  | python3 -c 'import sys, json; print(json.load(sys.stdin)["id"])')
echo "job $JOB"

while true; do
  curl -s -H "Authorization: Bearer $KEY" "http://127.0.0.1:8080/jobs/$JOB" \
    | python3 -c 'import sys, json; j = json.load(sys.stdin); print(j["status"], j["progress"]["fraction"], j.get("error") or "")'
  sleep 5
done
```

Ctrl-C at `succeeded` or `failed`. On failure the `error` field carries the
inference server's own message.

### 4. A real song, from your machine

This is the end-to-end path: client → ngrok → API → inference server.

```bash
cd /path/to/Tunecast
set -a; . <(tr -d '\r' < .env); set +a
uv run python client/generate.py \
  --url https://sliding-ethically-beckham.ngrok-free.dev \
  --key "$TUNECAST_API_KEY" \
  --lyrics-file client/example-lyrics-en.txt \
  --description "$(cat client/example-style-en.txt)" \
  --duration 180 --seed 7 --out song-client-en.wav
```

Run this in the pod terminal a few times while it works, to catch peak load. It
is the only figure the logs cannot reconstruct afterwards:

```bash
nvidia-smi --query-gpu=index,memory.used,utilization.gpu --format=csv,noheader
```

Measured under load: GPU 0 at 26,685 MiB and 96–97 %, GPU 1 at 13,957 MiB
bursting to 100 %.

### 5. Recover a job when the client dies

**Why this situation exists.** Generation is asynchronous by design. `POST /jobs`
writes the job to SQLite on the volume disk, hands it to a worker thread, and
returns 202 immediately. The worker keeps going regardless of who is listening.
The client is only a poller, so nothing that happens on your machine or on the
network can stop a job: a dropped ngrok connection, Ctrl-C, a closed laptop, or
the client giving up after its retries all leave the job running and its WAV
written to `/workspace/tunecast/outputs/`.

**So never resubmit a job that looked like it failed in transit.** Resubmitting
pays for the same song twice, and on a US$1.98/hour pod each three-minute song is
about US$0.085 of GPU time. Look it up by ID instead.

The ID is printed the moment you submit, on the `submitted <id> (seed …)` line,
which is worth keeping until the file is on disk.

```bash
cd /path/to/Tunecast
set -a; . <(tr -d '\r' < .env); set +a
JOB=20260903-034812-0a6346

curl -s -H "Authorization: Bearer $TUNECAST_API_KEY" -H 'ngrok-skip-browser-warning: 1' \
  "https://sliding-ethically-beckham.ngrok-free.dev/jobs/$JOB" | python -m json.tool
```

What each part is doing:

- `set -a; . <(tr -d '\r' < .env); set +a` loads `.env` and exports every variable
  in it. `tr -d '\r'` strips the carriage returns that Windows editors leave
  behind, which would otherwise become part of the key and produce a 401.
- The bearer header carries the API key; every route except `/health`, `/ready`
  and the UI page requires it.
- `ngrok-skip-browser-warning` suppresses ngrok's free-plan interstitial, which
  would otherwise return an HTML page instead of your JSON.
- `python -m json.tool` pretty-prints. On Windows it is `python`; inside the pod
  it is `python3`.

Read `status` in the output. `succeeded` means the audio is waiting, `running`
means keep waiting, `failed` means the `error` field explains why.

Once it says `succeeded`, download it:

```bash
curl -s -H "Authorization: Bearer $TUNECAST_API_KEY" -H 'ngrok-skip-browser-warning: 1' \
  -o song-client-en.wav "https://sliding-ethically-beckham.ngrok-free.dev/jobs/$JOB/audio"
```

`-o` writes the body to a file rather than to the terminal, which matters because
the response is a multi-megabyte WAV.

**If you lost the ID**, list recent jobs, newest first:

```bash
curl -s -H "Authorization: Bearer $TUNECAST_API_KEY" -H 'ngrok-skip-browser-warning: 1' \
  "https://sliding-ethically-beckham.ngrok-free.dev/jobs?limit=10" \
  | python -c "import sys, json; [print(j['id'], j['status'], j['params']['duration_s'], j['created_at']) for j in json.load(sys.stdin)]"
```

Or just reload the web UI, which lists the same jobs with a player and a
**Save WAV** button on each finished one.

Jobs survive a pod restart as records but not as work in progress: anything left
`running` or `queued` when the supervisor restarts is marked `failed` with the
error `pod restarted`, because its worker thread is gone. Finished songs stay
downloadable as long as the volume disk lives.

### 6. Collect the evidence, then shut down

```bash
echo "=== GPUS";    nvidia-smi --query-gpu=index,name,memory.used,memory.total --format=csv
echo "=== DISK";    df -h /workspace | tail -1
echo "=== WEIGHTS"; du -sh /workspace/models/MiniMax-Music3
echo "=== BOOT";    cat /workspace/tunecast/logs/boot.jsonl
echo "=== JOBS";    cat /workspace/tunecast/logs/jobs.jsonl
echo "=== OUTPUTS"; ls -la /workspace/tunecast/outputs/
```

Save your WAVs first. **Stop** keeps the volume disk and its weights, so the next
session skips the download and starts at the 90 s model load. **Terminate**
destroys everything.

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| Pod runs but there is no `/workspace/tunecast/logs`, and `pgrep -f tunecast.boot` returns nothing | The template points at a stock RunPod image. Set **Container Image** to `arpitkadam/tunecast:latest`. A Docker Command override also suppresses our entrypoint, so clear that field. |
| `torch.AcceleratorError: CUDA error: invalid device ordinal` | RunPod sets `CUDA_VISIBLE_DEVICES` to an empty string. sglang-omni reads that as an explicit "zero GPUs" configuration while the driver exposes none. Fixed since image `sha-304cb59`, which pins the variable from `nvidia-smi` and logs `cuda_visible_devices_pinned`. |
| Weights download again after a stop | Container disk and volume disk are transposed. The 80 GB belongs on **volume** disk; container disk is temporary and is wiped on stop. |
| `Tini is not running as PID 1` | Cosmetic since `sha-304cb59`, which runs `tini -s` so reaping works even though RunPod injects its own PID 1. |
| Client prints `Remote end closed connection without response` | The free ngrok tunnel dropped a connection. The client retries five times before giving up. **The job keeps running on the pod either way**, so recover it by ID rather than resubmitting; see "Recover a job when the client dies". Confirm the tunnel with `grep -c tunnel_down /workspace/tunecast/logs/boot.jsonl` and by checking `/ready`. |
| `Python was not found` on Windows | The pod-terminal snippets use `python3`, which Windows does not have. Run those inside the pod and use the client script from your machine. |
| Pod exits immediately with code 1 | A missing or misspelled environment variable. The log line names it. |
| Browser shows an ngrok interstitial | Free-plan behaviour, once per browser. Click "Visit Site". The API and the client bypass it with the `ngrok-skip-browser-warning` header. |

## Teardown

Terminate the pod. Nothing else bills: the template is free, the image lives on
Docker Hub, the ngrok domain is free-tier. To remove everything: delete the RunPod
template, the Docker Hub repository, and the GitHub secrets.

## Boot exit codes

1 bad environment (missing `TUNECAST_API_KEY` or ngrok vars), 2 weights could not
be completed, 3 the inference server never answered, 4 the tunnel never bound,
5 the inference server died, 6 port 8080 was already taken. Every failure also
logs a JSON line with the reason.
