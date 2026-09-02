from pathlib import Path

import pytest

from tunecast.config import ConfigError, load_settings

BASE = {
    "TUNECAST_API_KEY": "k",
    "NGROK_AUTHTOKEN": "t",
    "NGROK_DOMAIN": "example.ngrok-free.dev",
}


def test_missing_api_key_raises():
    with pytest.raises(ConfigError, match="TUNECAST_API_KEY"):
        load_settings({"NGROK_ENABLED": "0"})


def test_ngrok_required_when_enabled():
    with pytest.raises(ConfigError, match="NGROK_AUTHTOKEN"):
        load_settings({"TUNECAST_API_KEY": "k", "NGROK_DOMAIN": "d"})
    with pytest.raises(ConfigError, match="NGROK_DOMAIN"):
        load_settings({"TUNECAST_API_KEY": "k", "NGROK_AUTHTOKEN": "t"})


def test_ngrok_optional_when_disabled():
    s = load_settings({"TUNECAST_API_KEY": "k", "NGROK_ENABLED": "0"})
    assert s.ngrok_enabled is False
    assert s.ngrok_authtoken is None
    assert s.ngrok_domain is None


def test_defaults():
    s = load_settings(BASE)
    assert s.api_key == "k"
    assert s.ngrok_enabled is True
    assert s.stub is False
    assert s.port == 8080
    assert s.sidecar_port == 8000
    assert s.max_concurrent == 1
    assert s.keep_last == 200
    assert s.dit_steps is None
    assert s.dit_cfg_scale is None
    assert s.hf_token is None
    assert s.model_id == "MiniMaxAI/MiniMax-Music3"
    assert s.model_revision == "fbdf52fbaaca799592917417eb05f1899f1255ec"
    assert s.data_dir == Path("/workspace")
    assert s.models_dir == Path("/workspace/models/MiniMax-Music3")
    assert s.outputs_dir == Path("/workspace/tunecast/outputs")
    assert s.db_path == Path("/workspace/tunecast/jobs.db")
    assert s.logs_dir == Path("/workspace/tunecast/logs")


def test_optional_values_parsed():
    s = load_settings({**BASE, "HF_TOKEN": "hf", "TUNECAST_DIT_STEPS": "20", "TUNECAST_DIT_CFG_SCALE": "1.5", "TUNECAST_PORT": "9000"})
    assert s.hf_token == "hf"
    assert s.dit_steps == 20
    assert s.dit_cfg_scale == 1.5
    assert s.port == 9000


def test_max_concurrent_clamped():
    assert load_settings({**BASE, "TUNECAST_MAX_CONCURRENT": "9"}).max_concurrent == 4
    assert load_settings({**BASE, "TUNECAST_MAX_CONCURRENT": "0"}).max_concurrent == 1


def test_stub_data_dir_default():
    s = load_settings({**BASE, "TUNECAST_STUB": "1"})
    assert s.stub is True
    assert s.data_dir == Path(".data").resolve()


def test_explicit_data_dir_wins(tmp_path):
    s = load_settings({**BASE, "TUNECAST_STUB": "1", "TUNECAST_DATA_DIR": str(tmp_path)})
    assert s.data_dir == tmp_path


def test_invalid_int_raises():
    with pytest.raises(ConfigError, match="TUNECAST_PORT"):
        load_settings({**BASE, "TUNECAST_PORT": "abc"})


def test_port_out_of_range_raises():
    with pytest.raises(ConfigError, match="TUNECAST_PORT"):
        load_settings({**BASE, "TUNECAST_PORT": "70000"})
    with pytest.raises(ConfigError, match="TUNECAST_SIDECAR_PORT"):
        load_settings({**BASE, "TUNECAST_SIDECAR_PORT": "0"})
