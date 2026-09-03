#!/usr/bin/env python3
"""Submit a song to a Tunecast server, wait for it, save the WAV. Standard library only.

    python client/generate.py --url https://sliding-ethically-beckham.ngrok-free.dev \
        --lyrics-file lyrics.txt --description "warm acoustic pop, female vocals" \
        --duration 180 --out song.wav

Exit codes: 0 saved, 1 the job failed on the server (message printed), 2 transport or auth error.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request


RETRIES = 5          # a free ngrok tunnel drops a connection now and then
RETRY_WAIT_S = 3.0


def request(url: str, key: str, method: str = "GET", body: dict | None = None, timeout: float = 60) -> bytes:
    headers = {"Authorization": f"Bearer {key}", "ngrok-skip-browser-warning": "1"}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def request_with_retries(url: str, key: str, method: str = "GET", body: dict | None = None,
                         timeout: float = 60, attempts: int = RETRIES) -> bytes:
    """Retry transport failures and 5xx. A dropped tunnel must not abandon a job that is still running
    on the server. 4xx is not retried: a bad key or a bad request will not fix itself."""
    for attempt in range(1, attempts + 1):
        try:
            return request(url, key, method, body, timeout)
        except urllib.error.HTTPError as e:
            if e.code < 500 or attempt == attempts:
                raise
            reason = f"HTTP {e.code}"
        except (urllib.error.URLError, OSError) as e:
            if attempt == attempts:
                raise
            reason = str(e)
        print(f"connection problem ({reason}), retrying {attempt}/{attempts - 1}", file=sys.stderr)
        time.sleep(RETRY_WAIT_S)
    raise RuntimeError("unreachable")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate one song through a Tunecast server.")
    parser.add_argument("--url", required=True, help="server base URL, e.g. https://<domain> or http://127.0.0.1:8080")
    parser.add_argument("--key", default=os.environ.get("TUNECAST_API_KEY"), help="API key (default: $TUNECAST_API_KEY)")
    parser.add_argument("--lyrics-file", required=True, help="text file with lyrics; section tags like [Verse] on their own lines")
    parser.add_argument("--description", required=True, help="style caption: genre, tempo, key, instruments, vocals, mood")
    parser.add_argument("--duration", type=int, default=180, help="maximum seconds, 1-360 (default 180); the model may end the song sooner")
    parser.add_argument("--seed", type=int, default=None, help="integer seed; omitted = server picks one")
    parser.add_argument("--out", default="song.wav", help="output WAV path (default song.wav)")
    parser.add_argument("--poll", type=float, default=5.0, help="seconds between status checks (default 5)")
    args = parser.parse_args(argv)

    if not args.key:
        print("no API key: pass --key or set TUNECAST_API_KEY", file=sys.stderr)
        return 2
    base = args.url.rstrip("/")
    with open(args.lyrics_file, encoding="utf-8") as f:
        lyrics = f.read()
    body = {"lyrics": lyrics, "description": args.description, "duration_s": args.duration, "format": "wav"}
    if args.seed is not None:
        body["seed"] = args.seed

    try:
        job = json.loads(request(f"{base}/jobs", args.key, "POST", body, timeout=30))
    except urllib.error.HTTPError as e:
        print(f"submit failed: HTTP {e.code} {e.read().decode('utf-8', 'replace')[:300]}", file=sys.stderr)
        return 2
    except (urllib.error.URLError, OSError) as e:
        print(f"cannot reach {base}: {e}", file=sys.stderr)
        return 2
    print(f"submitted {job['id']} (seed {job['params']['seed']}, {args.duration}s)")

    started = time.monotonic()
    while True:
        time.sleep(args.poll)
        try:
            job = json.loads(request_with_retries(f"{base}/jobs/{job['id']}", args.key))
        except (urllib.error.HTTPError, urllib.error.URLError, OSError) as e:
            print(f"status checks kept failing, giving up: {e}", file=sys.stderr)
            print(f"the job may still be running; retrieve it later with: {base}/jobs/{job['id']}", file=sys.stderr)
            return 2
        status = job["status"]
        if status == "queued":
            print(f"queued, {job['queue_position']} ahead")
        elif status == "running":
            pr = job["progress"]
            print(f"running {pr['elapsed_s']:.0f}s of about {pr['estimated_total_s']:.0f}s (estimate)")
        elif status == "failed":
            print(f"failed: {job['error']}", file=sys.stderr)
            return 1
        elif status == "succeeded":
            break

    try:
        audio = request_with_retries(f"{base}{job['audio_url']}", args.key, timeout=600)
    except (urllib.error.HTTPError, urllib.error.URLError, OSError) as e:
        print(f"download failed: {e}", file=sys.stderr)
        return 2
    with open(args.out, "wb") as f:
        f.write(audio)
    timings = job.get("timings", {})
    print(
        f"succeeded: inference {timings.get('inference_s', '?')}s, wall {time.monotonic() - started:.0f}s, "
        f"saved {args.out} ({len(audio):,} bytes)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
