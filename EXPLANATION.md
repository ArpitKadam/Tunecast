# Explanation log

One entry per completed task, newest first. Plain language for a teammate
reading cold. Why-questions belong in `DECISIONS.md`.

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
