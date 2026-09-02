import json

from tunecast.log import log_event, redact, setup_logging


def test_redact_masks_secret_like_keys():
    env = {
        "TUNECAST_API_KEY": "s",
        "HF_TOKEN": "t",
        "NGROK_AUTHTOKEN": "u",
        "MY_SECRET": "v",
        "TUNECAST_PORT": "8080",
    }
    assert redact(env) == {
        "TUNECAST_API_KEY": "***",
        "HF_TOKEN": "***",
        "NGROK_AUTHTOKEN": "***",
        "MY_SECRET": "***",
        "TUNECAST_PORT": "8080",
    }


def test_log_event_writes_one_json_line(tmp_path):
    logger = setup_logging(tmp_path, "boot.jsonl")
    try:
        log_event(logger, "env_ok", elapsed_s=0.5, since_boot_s=0.5)
        lines = (tmp_path / "boot.jsonl").read_text(encoding="utf-8").strip().splitlines()
    finally:
        for h in list(logger.handlers):
            h.close()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["event"] == "env_ok"
    assert rec["elapsed_s"] == 0.5
    assert rec["since_boot_s"] == 0.5
    assert rec["level"] == "INFO"
    assert "ts" in rec


def test_setup_logging_without_dir_only_logs_to_stdout(capsys):
    logger = setup_logging(None)
    log_event(logger, "hello", n=1)
    out = capsys.readouterr().out.strip().splitlines()[-1]
    assert json.loads(out)["event"] == "hello"


def test_setup_logging_twice_does_not_duplicate_handlers(tmp_path):
    a = setup_logging(None)
    b = setup_logging(None)
    assert a is b
    assert len(b.handlers) == 1
