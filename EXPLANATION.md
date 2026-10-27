# Explanation log

One entry per completed task, newest first. Plain language for a teammate
reading cold. Why-questions belong in `DECISIONS.md`.

## 2026-10-27 — Task 15: offline portfolio front door (#8)

- **What changed:** The README now opens with a pitch, a two-tab recording, screenshots, the one real sample (`song-client.wav`) and an offline demo. The pod runbook (template, spin-up, recovery, troubleshooting, teardown) moved to `RUNBOOK.md`. The measured numbers are labelled as one run; the length table is labelled as an extrapolation. Screenshots, the recording and `demo/` were added with the scripts that generate them. No code under `tunecast/` changed and nothing was measured.
- **How it works:** `scripts/capture_media.py` starts the real app on the stub sidecar, seeds four takes through the API (titles, a favorite, one child), and captures PNGs and a GIF with Playwright. `scripts/build_demo.py` copies `app.js`/`app.css` and writes `index.html` with relative asset URLs, the shim first and a banner. In `demo/`, `shim.js` replaces `fetch` for the API routes and serves one fixture take, the measured run, whose audio is the root `song-client.wav`. Start take is disabled with a reason, `/ready` answers 503 (no model) and the key prompt is skipped.
- **How it was verified:** `uv run pytest -q`: 203 passed, 49 deselected. `uv run pytest -m e2e -q`: 49 passed, including 6 demo tests (banner, tabs, playback across tab switches, Start take disabled, label edits in memory, no request leaves localhost) served from a static server at the repo root. A real `python -m http.server` plus Chromium opened `/demo/` and the take played. Every PNG was opened; GIF frames were sampled. Reviewer first returned RED for three false README statements (billing while stopped, 18.2 MB vs 19.1 MB, "copied unchanged") and two unlabelled extrapolations; all were fixed, along with the stub caption, the confusing green "online" indicator in the demo, `.dockerignore` and a runbook paragraph. The README numbers were rechecked against the Task 7 entry and the file itself (149.188 s, 32 kHz stereo, 19,096,136 bytes; US$0.085 per song at US$1.98/h; the four-song US$0.42 line is labelled projected).
- **Limitations / follow-ups:** The demo has one take, so no A/B. Screenshots show stub tones and a forced-on tunnel lamp. Pages is not enabled. The demo needs a static server, not `file://`. The demo's `created_at` is a placeholder hidden by CSS. The README claims no hosted link exists.

## 2026-10-27 — Task 14: network and authentication state made visible

- **What changed:** The page says whether it is online, reconnecting or offline, and how old its data is ("updated 12s ago"). While it cannot reach the server the takes list is marked "Stale", the model and tunnel lamps turn into hollow "unknown" rings instead of showing their last state, and a "Retry now" button appears. Polling backs off from 3 s up to 30 s and resets on success. A "Sign out" button forgets the key and everything loaded with it. The key box has a "Remember on this device" checkbox, off by default: the key then lives in `sessionStorage` and disappears when the tab closes.
- **How it works:** `app.js` keeps a `link` state counted by `note(ok)`: one failed poll or readiness check is "reconnecting", three in a row "offline", any success "online". A single timer loop `tick()` polls, retries the unlock, or checks `/ready`, with delay `3 s × 2^failures` capped at 30 s and a 10 s abort per request. `lock()` is shared by Sign out and a 401: it forgets the key from both storages, revokes the audio blobs, ejects the dock track, clears the takes and shows the key box, keeping the Lab draft. Playback of an already loaded take is not interrupted by an outage. `/ready` answering 503 counts as reachable. No server changes.
- **How it was verified:** `uv run pytest -q`: 202 passed, 43 deselected. `uv run pytest -m e2e -q`: 43 passed, 0 xfailed, no errors (two consecutive runs by the implementer, one more by me). The two Task 14 xfails are now real passing tests with their assertions kept. New browser tests stop and restart the server and assert the transitions online → reconnecting → offline → online, the ticking label, stale on and off, backoff by counting `/jobs` requests, Retry now (large backoff so only the click can explain a fast recovery; the test fails with the click handler disabled), playback surviving an outage, sign-out stopping requests and clearing storage, audio, lineage banner and A/B, remember on and off, a legacy `localStorage` key still unlocking, a revoked key ejecting audio, key entry while unreachable, and no horizontal overflow at 390x844. Mutation checks (constant poll interval; no blob cleanup or lamp clearing) made the matching tests fail. Screenshots at 1280x800 and 390x844 were inspected. Reviewer verdict GREEN; its findings (live-region rewrites, `/info` timeout, scheduler `finally`, weak Retry test, teardown race, sign-out assertions) were fixed.
- **Limitations / follow-ups:** The Lab draft survives sign-out by design. With a stored key and an unreachable server at load the page shows only the masthead until the server returns. The 3-failure threshold and 30 s cap are constants. Not run through a real ngrok tunnel, so how its 502/504 pages register is assumed (non-OK counts as failure). Chromium only. Backoff test uses timing bounds. "Sign out" is now the first Tab stop.

## 2026-10-26 — Task 13: Player and Lab tabs on one shared state

- **What changed:** The page is now two tabs. Player: searchable takes (title, description, lyrics, seed, status), editable titles, favorites with a "Favorites only" filter, Play and Save WAV, the real duration, stats, diff against the parent, Export experiment, Cancel/Delete. Lab: a lyrics section editor (add, remove, reorder, retag, plus a Text view), style preset chips, seed lock, "Edit a copy" (duplicate-and-edit with lineage) and an A/B compare. A dock at the bottom holds the one audio element and is visible on both tabs. Titles and favorites are saved on the server (`PATCH /jobs/{id}`); the page's JS and CSS are now separate files under `/static`.
- **How it works:** `app.js` keeps one state object (last poll, current track, A/B picks, parent id, pending submit). Tabs follow the ARIA tablist pattern with roving tabindex and hidden-not-removed panels, so audio and drafts survive switching. The section editor parses `#lyrics` into sections and writes every edit back; chips toggle phrases in the description; A/B swaps the blob source and carries position and play state over. Submit, reconcile, 429/503 and cancel behaviour from Tasks 10–12 is unchanged. Assets are served from memory by an allow-list route with content-hash URLs.
- **How it was verified:** `uv run pytest -q`: 202 passed, 33 deselected. `uv run pytest -m e2e -q`: 31 passed, 2 xfailed (Task 14's, still failing for their intended reasons: no offline text exists; `ready()` still swallows errors). New tests cover PATCH validation/auth/404/replay, static routes and media types, a walk of every route for 401 without a key, tab keyboard navigation, playback surviving tab switches, drafts surviving tab switches, search, favorites and titles across reload, duplicate-and-edit lineage, A/B position and zero audio requests on selection, section/chip/seed-lock behaviour, the dock not covering focused controls, and no horizontal overflow at 390x844 on both tabs. Stub-sidecar screenshots at 1280x800 and 390x844 were reviewed; a real mobile Lab overflow (long unbroken lyric line, 2130 px) was found and fixed. Reviewer verdict GREEN; its risks (A/B prefetch cost, favorite double-click race, dropped playing take, silent draft overwrite, dock covering focus, hidden data-cap warning, title trim order, public OpenAPI) were fixed. Only the dock test was confirmed to fail without its fix.
- **Limitations / follow-ups:** Not run through the real ngrok tunnel or a built image. Search covers the newest 50 takes. Prune can delete favorites. A/B flip is not gapless. Chromium only. For Task 14: key handling is `key()/setKey()/forgetKey()` in `app.js`, `api()` is the fetch wrapper (401 calls `forgetKey()` and `showKeybox()`), `poll()` and `ready()` swallow failures and are where offline/stale states hook in, and sign-out should stop `pollTimer`, forget loaded takes and show the key box.

## 2026-10-26 — Task 11: bounded queue admission and cancel

- **What changed:** At most `TUNECAST_MAX_QUEUED` (default 8) takes can wait. A submit beyond that gets 429 with `Retry-After`; a submit before the model server is ready gets 503 with `Retry-After`. Neither creates anything. Replaying an existing `Idempotency-Key` still returns its job on a full queue. Deleting a queued take cancels it; deleting a running take is refused with 409. The page shows the refusal with the retry time, keeps the draft, and labels the button Cancel, Can't cancel or Delete.
- **How it works:** `JobStore.create` counts queued rows, checks the request key and inserts under one lock hold, and raises `QueueFull` when full. `submit` turns that into 429. The worker skips ids whose job is gone or not `queued` and claims a job with a conditional update. The 503 guard reads `ReadyState.model`.
- **How it was verified:** `uv run pytest -q`: 185 passed, 17 deselected. `uv run pytest -m e2e -q`: 15 passed, 2 xfailed (Task 14's). Tests cover cap and cap+1 (429, nothing created), concurrent submits at the boundary (16 store threads, 12 API threads) never exceeding the cap, replay at a full queue (200, 409 for a changed body), 503, queued cancel never reaching the sidecar, running cancel 409, both worker-skip races, and config validation; e2e covers the 429 message with the draft kept and the cancel buttons. Task 11 was built in a separate worktree and merged onto Task 12 by three-way merge; the merged tree passes both suites. Reviewer verdict GREEN; its readiness-probe finding was fixed.
- **Limitations / follow-ups:** `Retry-After` is a fixed 30 s. A cancelled id stays in the in-memory queue until popped. `client/generate.py` does not honour `Retry-After`. `/ready` is unchanged.

## 2026-10-26 — Task 12: experiment lineage and honest measurements

- **What changed:** Every job records its parent, its experiment, the model revision and a small config snapshot. Real-time factor and peak GPU memory are measured instead of guessed, the progress estimator learns from the actual audio length, and an experiment exports as JSON or CSV. Each take shows seed, queue time, inference time, real-time factor and what changed against its parent; "Refine" starts a child take and "Export experiment" downloads the file.
- **How it works:** `POST /jobs` accepts optional `parent_job_id` and `experiment_id`; `JobStore.create` resolves the experiment under the store lock. The runner starts a `GpuPeakSampler` thread before inference and stops it in `finally`; the highest per-GPU memory seen is stored. `job_to_dict` adds `real_time_factor` and, for live jobs, `estimate {seconds, confidence}`. The export route flattens an experiment's jobs into 19 fields.
- **How it was verified:** `uv run pytest -q`: 168 passed, 15 deselected. `uv run pytest -m e2e -q`: 13 passed, 2 xfailed (Task 14's). New `tests/test_experiments.py` covers lineage and validation, idempotency interaction, no secrets in the snapshot, RTF, the estimator, the sampler (fake query, stopped on failure), export JSON and CSV escaping, and old-schema migration with backfill. The e2e test refines a take and downloads the export. Reviewer verdict GREEN; its findings (backfill, blob revoke, visible confidence label, sampler start inside `try`) were fixed.
- **Limitations / follow-ups:** Peak GPU includes weights and concurrent jobs. Estimator state resets on restart. CSV is not protected against spreadsheet formula injection and has no BOM. Not mutation-tested test by test. Task 13 will replace the page and must keep Refine's behaviour.

## 2026-10-26 — Task 10: idempotent submissions

- **What changed:** A network blip can no longer queue the same song twice. The page and `client/generate.py` send an `Idempotency-Key` with each submission and reuse it on retry. After a lost response the page says "Submission status unknown, checking…" and looks the job up by key.
- **How it works:** The server stores the key and a hash of the validated body on the job row under a UNIQUE index. The insert either succeeds (202, enqueued) or hits the key: same body gives 200 with the existing job, a changed body gives 409; nothing is enqueued on the replay path. `GET /jobs?request_id=` returns the matching job or an empty list. If the POST fails, or comes back 5xx, the page polls that lookup with backoff and shows the job, "Not submitted" (draft kept), or "status unknown, resending is safe". The key is kept until the server answers definitively, so clicking again is safe. The CLI uses one UUID per run and retries submit like polling.
- **How it was verified:** `uv run pytest -q`: 133 passed, 14 deselected. `uv run pytest -m e2e -q`: 12 passed, 2 xfailed (the Task 14 ones). Tests cover same key/body replay, 409, no-key behaviour, 8 concurrent identical submits giving one job and one sidecar call, malformed keys, lookup and its auth, old-schema migration, CLI retries reusing the key, and e2e lost-response, never-reached and 504-after-accept cases. The 504 test fails with the 5xx handling removed and passes with it. Reviewer verdict GREEN; its 5xx finding was fixed before commit.
- **Limitations / follow-ups:** A deleted or pruned job frees its key. A failed job replays as failed. Old rows have no key. A POST still in flight when the lookup says "none" is a small theoretical gap; keeping the key on retry covers it. After all submit retries fail with 5xx, the CLI message does not say the job may exist (nit, left).

## 2026-10-26 — Task 8: PR CI gate and browser regression tests

- **What changed:** Pull requests now run the unit tests and a Playwright browser suite. The Docker image can only be built after those pass, and pull requests never build or push it.
- **How it works:** The `test` job runs `uv sync --frozen --group dev`, `pytest`, installs chromium, then `pytest -m e2e`. `build` has `needs: test` and `if: github.event_name != 'pull_request'`. The e2e fixture boots the actual app with the stub sidecar on a free port; chromium drives the real page. Reconnect tests stop and restart the server; the lost-response test aborts the POST after the server accepted it. Three known UI gaps are strict xfails: lost response should say "status unknown" (Task 10), an offline indicator and lamps clearing when the server is unreachable (Task 14).
- **How it was verified:** `uv run pytest -q` gives 115 passed, 12 deselected. `uv run pytest -m e2e -q` gives 9 passed, 3 xfailed (about 62 s); `--runxfail` showed each xfail failing for its intended reason. `uv lock --check` passes. Reviewer verdict GREEN.
- **Limitations / follow-ups:** The workflow has not run on GitHub; "a failing PR test blocks publish" is unverified until a PR exists. Tab-specific cases wait for Task 13, so issue #3 stays open until then. No test for drafts across a page reload (the page does not persist them). Worker threads from `JobRunner` are not stopped between e2e tests (daemon threads, harmless at this size).

## 2026-10-26 — Task 9: validate generated audio, store real duration

- **What changed:** A job is green only if the sidecar returned a complete, playable WAV. The API returns the real duration, sample rate, channel count and byte size, and the UI shows the real length instead of the requested maximum (older rows without it show "up to M:SS").
- **How it works:** `HttpSidecar.generate` reads the body (a short body raises `IncompleteRead`, reported as truncated) and passes it to `inspect_wav`, which walks the RIFF chunks and requires a PCM or float fmt chunk with consistent fields plus a data chunk of at least one whole frame that fits in the bytes received. Only then is the file written via `.part` and `os.replace`. Any failure raises `SidecarError`, the runner marks the job `failed` with the message, and no output file remains. On success the four `audio_*` columns are saved; older databases get them added at startup.
- **How it was verified:** `uv run pytest -q tests --ignore=tests/e2e` gives 115 passed (87 before). `tests/test_audio_validation.py` covers JSON 200, empty body, truncated transfer, timeout, disk-write failure, valid PCM16/float32/EXTENSIBLE, no leftover file, API fields and old-schema migration. The real pod file `song-client.wav` validates as 149.188 s, 32 kHz, stereo. Reviewer verdict GREEN. The `index.html` one-line change was not driven in a browser.
- **Limitations / follow-ups:** A data chunk smaller than the body is accepted. `timeout_s` is per socket operation, not a total deadline (unchanged). A valid WAV is left on disk if the final DB update raises. Estimator still uses the requested duration (Task 12).

## 2026-10-26 — Issue backlog written into TASK.md

- **What changed:** The eight open GitHub issues (#1–#8) are now Tasks 8–15
  in `TASK.md`, each with dependencies and acceptance criteria. Docs only; no
  code touched.
- **How it works:** Issue bodies were read with `gh issue view`, then ordered
  by the dependencies the issues state themselves (see `DECISIONS.md`,
  2026-10-26). Task numbers continue the existing table; `#n` means the
  GitHub issue.
- **How it was verified:** `gh issue list --state all` returned exactly eight
  issues, all open, numbers 1–8; each appears once in the new table.
- **Limitations / follow-ups:** Owner rulings recorded the same day: #7
  per-user access skipped, #6 queue cap approved (default 8), #8 uses no new
  measurements. Nothing implemented yet; waiting on "Go for Task 8".

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
  client run, GPU sampling under load, job recovery, evidence capture), a
  section on lyric-driven length, and a troubleshooting table covering all
  five defects found in this task. The recovery step explains why a job
  survives its client: submission is asynchronous, the job row lives in
  SQLite on the volume disk and the worker thread runs regardless of who is
  listening, so a dropped tunnel never costs the work. It gives the
  status-by-ID and download-by-ID commands, a way to list recent jobs when
  the ID is lost, and explains each flag, including why `tr -d` on `.env`
  matters (a trailing carriage return from a Windows editor becomes part of
  the key and produces a 401).
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
