"""Guards on the image definition: the pins the spec promises must be present in the files CI builds from."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASE_DIGEST = "sha256:02a85f00438c901c72a2eb2ef738974a807f63af3d13084445604f3344067b19"


def test_dockerfile_pins_base_digest_tini_and_entrypoint():
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert f"FROM hongccc/sglang-omni:dev@{BASE_DIGEST}" in text
    assert "tini" in text
    assert 'ENTRYPOINT ["tini", "--", "python3", "-m", "tunecast.boot"]' in text
    assert "EXPOSE 8080" in text


def test_dockerfile_pins_ngrok_by_version_and_sha256():
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert re.search(r"ARG NGROK_VERSION=3\.\d+\.\d+", text)
    assert re.search(r"ARG NGROK_SHA256=[0-9a-f]{64}", text)
    assert "sha256sum -c" in text


def test_requirements_pin_sglang_omni_and_app_deps():
    reqs = (ROOT / "docker" / "requirements.txt").read_text(encoding="utf-8")
    assert "sglang-omni==0.1.4" in reqs
    for name in ("fastapi==", "uvicorn[standard]==", "huggingface_hub=="):
        assert name in reqs, name


def test_dockerignore_keeps_secrets_and_junk_out():
    ignore = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    for entry in (".env", ".env.*", ".git", ".data", "tests", "docs"):
        assert entry in ignore, entry


def test_workflow_pushes_immutable_sha_tag_and_latest_with_registry_cache():
    wf = (ROOT / ".github" / "workflows" / "docker.yml").read_text(encoding="utf-8")
    assert "arpitkadam/tunecast" in wf
    assert "type=sha" in wf
    assert "type=raw,value=latest,enable={{is_default_branch}}" in wf
    assert "type=registry,ref=arpitkadam/tunecast:buildcache" in wf
    assert "secrets.DOCKERHUB_USERNAME" in wf and "secrets.DOCKERHUB_TOKEN" in wf
    assert "free-disk-space" in wf
