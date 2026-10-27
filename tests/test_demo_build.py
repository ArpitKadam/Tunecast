"""demo/ is generated from tunecast/static; a stale copy would show visitors a UI the app no longer has."""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GENERATED = ("index.html", "app.js", "app.css")


def test_committed_demo_matches_a_fresh_build(tmp_path):
    subprocess.run([sys.executable, str(ROOT / "scripts" / "build_demo.py"), str(tmp_path)], check=True)
    for name in GENERATED:       # compared without line endings: a Windows checkout may convert them
        fresh = (tmp_path / name).read_bytes().replace(b"\r\n", b"\n")
        committed = (ROOT / "demo" / name).read_bytes().replace(b"\r\n", b"\n")
        assert committed == fresh, f"demo/{name} is stale: run `python scripts/build_demo.py`"
