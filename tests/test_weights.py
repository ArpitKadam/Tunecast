import logging
from pathlib import Path

import pytest

from tunecast import weights
from tunecast.config import load_settings
from tunecast.weights import MARKER, WeightsError, ensure_weights, load_manifest, verify, write_marker

REV = "abc123"
FAKE = [("a.bin", 3), ("sub/b.bin", 5)]
LOG = logging.getLogger("test")


@pytest.fixture
def fake_manifest(monkeypatch):
    monkeypatch.setattr(weights, "load_manifest", lambda: list(FAKE))


def build_tree(root: Path, sizes: dict[str, int] | None = None, marker: str | None = REV) -> Path:
    for path, size in FAKE:
        p = root / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x" * (sizes or {}).get(path, size))
    if marker is not None:
        write_marker(root, marker)
    return root


def settings_for(tmp_path: Path):
    return load_settings({
        "TUNECAST_API_KEY": "k",
        "NGROK_ENABLED": "0",
        "TUNECAST_DATA_DIR": str(tmp_path),
        "TUNECAST_MODEL_REVISION": REV,
    })


def test_load_manifest_matches_pinned_snapshot():
    m = load_manifest()
    assert len(m) == 88
    assert sum(size for _, size in m) == 57353379600
    assert "language_model/model-00001-of-00004.safetensors" in {p for p, _ in m}


def test_verify_ok_on_matching_tree(tmp_path, fake_manifest):
    assert verify(build_tree(tmp_path), REV) == []


def test_verify_reports_missing_and_short_files(tmp_path, fake_manifest):
    root = build_tree(tmp_path, sizes={"a.bin": 1})
    (root / "sub" / "b.bin").unlink()
    assert sorted(verify(root, REV)) == ["a.bin", "sub/b.bin"]


def test_verify_fails_on_wrong_revision_marker(tmp_path, fake_manifest):
    assert verify(build_tree(tmp_path, marker="other"), REV) == [MARKER]


def test_verify_fails_without_marker(tmp_path, fake_manifest):
    assert verify(build_tree(tmp_path, marker=None), REV) == [MARKER]


def test_verify_on_missing_dir_lists_everything(tmp_path, fake_manifest):
    assert sorted(verify(tmp_path / "nope", REV)) == sorted([MARKER, "a.bin", "sub/b.bin"])


def test_ensure_weights_skips_download_when_complete(tmp_path, fake_manifest, monkeypatch):
    s = settings_for(tmp_path)
    build_tree(s.models_dir)

    def boom(**kwargs):
        raise AssertionError("snapshot_download must not be called")

    monkeypatch.setattr(weights, "snapshot_download", boom)
    assert ensure_weights(s, LOG) == 0.0


def test_ensure_weights_downloads_and_marks_when_missing(tmp_path, fake_manifest, monkeypatch):
    s = settings_for(tmp_path)
    calls = []

    def fake_download(**kwargs):
        calls.append(kwargs)
        build_tree(Path(kwargs["local_dir"]), marker=None)
        return kwargs["local_dir"]

    monkeypatch.setattr(weights, "snapshot_download", fake_download)

    elapsed = ensure_weights(s, LOG)

    assert elapsed >= 0.0
    assert len(calls) == 1
    assert calls[0]["repo_id"] == "MiniMaxAI/MiniMax-Music3"
    assert calls[0]["revision"] == REV
    assert Path(calls[0]["local_dir"]) == s.models_dir
    assert calls[0]["token"] is None
    assert (s.models_dir / MARKER).read_text(encoding="utf-8").strip() == REV
    assert verify(s.models_dir, REV) == []


def test_ensure_weights_raises_when_download_leaves_tree_incomplete(tmp_path, fake_manifest, monkeypatch):
    s = settings_for(tmp_path)
    monkeypatch.setattr(weights, "snapshot_download", lambda **kw: kw["local_dir"])
    with pytest.raises(WeightsError, match="a.bin"):
        ensure_weights(s, LOG)
    assert not (s.models_dir / MARKER).exists()


def test_real_snapshot_download_accepts_the_kwargs_we_pass():
    import inspect

    from huggingface_hub import snapshot_download

    params = inspect.signature(snapshot_download).parameters
    assert {"repo_id", "revision", "local_dir", "token"} <= set(params)
