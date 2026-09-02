"""Environment-driven settings. Missing secrets are a hard failure, never an open server."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

MODEL_ID = "MiniMaxAI/MiniMax-Music3"
MODEL_REVISION = "fbdf52fbaaca799592917417eb05f1899f1255ec"
MAX_CONCURRENT_CAP = 4
TRUE_VALUES = {"1", "true", "yes", "on"}


class ConfigError(Exception):
    """Raised when the environment is unusable; the message names the variable."""


@dataclass(frozen=True)
class Settings:
    api_key: str
    ngrok_enabled: bool
    ngrok_authtoken: str | None
    ngrok_domain: str | None
    hf_token: str | None
    stub: bool
    data_dir: Path
    port: int
    sidecar_port: int
    max_concurrent: int
    keep_last: int
    dit_steps: int | None
    dit_cfg_scale: float | None
    model_id: str = MODEL_ID
    model_revision: str = MODEL_REVISION

    @property
    def models_dir(self) -> Path:
        return self.data_dir / "models" / self.model_id.rsplit("/", 1)[-1]

    @property
    def outputs_dir(self) -> Path:
        return self.data_dir / "tunecast" / "outputs"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "tunecast" / "jobs.db"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "tunecast" / "logs"


def _str(env: Mapping[str, str], name: str) -> str | None:
    value = env.get(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    value = _str(env, name)
    return default if value is None else value.lower() in TRUE_VALUES


def _int(env: Mapping[str, str], name: str, default: int | None) -> int | None:
    value = _str(env, name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        raise ConfigError(f"{name} must be an integer, got {value!r}") from None


def _port(env: Mapping[str, str], name: str, default: int) -> int:
    value = _int(env, name, default)
    if not 1 <= value <= 65535:
        raise ConfigError(f"{name} must be between 1 and 65535, got {value}")
    return value


def _float(env: Mapping[str, str], name: str) -> float | None:
    value = _str(env, name)
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        raise ConfigError(f"{name} must be a number, got {value!r}") from None


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    env = os.environ if env is None else env

    api_key = _str(env, "TUNECAST_API_KEY")
    if not api_key:
        raise ConfigError("TUNECAST_API_KEY is required")

    ngrok_enabled = _bool(env, "NGROK_ENABLED", True)
    ngrok_authtoken = _str(env, "NGROK_AUTHTOKEN")
    ngrok_domain = _str(env, "NGROK_DOMAIN")
    if ngrok_enabled:
        if not ngrok_authtoken:
            raise ConfigError("NGROK_AUTHTOKEN is required when NGROK_ENABLED=1")
        if not ngrok_domain:
            raise ConfigError("NGROK_DOMAIN is required when NGROK_ENABLED=1")
    else:
        ngrok_authtoken = ngrok_domain = None

    stub = _bool(env, "TUNECAST_STUB", False)
    data_dir_raw = _str(env, "TUNECAST_DATA_DIR")
    if data_dir_raw:
        data_dir = Path(data_dir_raw)
    elif stub:
        data_dir = Path(".data").resolve()
    else:
        data_dir = Path("/workspace")

    max_concurrent = min(max(_int(env, "TUNECAST_MAX_CONCURRENT", 1), 1), MAX_CONCURRENT_CAP)

    return Settings(
        api_key=api_key,
        ngrok_enabled=ngrok_enabled,
        ngrok_authtoken=ngrok_authtoken,
        ngrok_domain=ngrok_domain,
        hf_token=_str(env, "HF_TOKEN"),
        stub=stub,
        data_dir=data_dir,
        port=_port(env, "TUNECAST_PORT", 8080),
        sidecar_port=_port(env, "TUNECAST_SIDECAR_PORT", 8000),
        max_concurrent=max_concurrent,
        keep_last=max(_int(env, "TUNECAST_KEEP_LAST", 200), 1),
        dit_steps=_int(env, "TUNECAST_DIT_STEPS", None),
        dit_cfg_scale=_float(env, "TUNECAST_DIT_CFG_SCALE"),
        model_revision=_str(env, "TUNECAST_MODEL_REVISION") or MODEL_REVISION,
    )
