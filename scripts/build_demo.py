"""Assemble demo/ from the real UI so the demo cannot drift from it.

    python scripts/build_demo.py [out_dir]       # default: demo/

Copies tunecast/static/app.js and app.css unchanged, and writes index.html from the real page with its asset
URLs made relative, the fixture shim loaded first, and a permanent banner. demo/shim.js, demo.css and
fixtures.json are hand-written and stay where they are. The generated files are committed so that GitHub
Pages or `python -m http.server` from the repo root serves /demo/ with no build step;
tests/test_demo_build.py fails when they fall behind tunecast/static.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "tunecast" / "static"

BANNER = (
    '<div class="demo-banner" role="note"><strong>OFFLINE DEMO</strong> &mdash; recorded fixtures, no GPU, nothing is generated '
    "<span>The take is the real output of the 2026-09-03 pod run; the page is the real UI.</span></div>\n"
)
SWAPS = [
    ("<title>Tunecast</title>", "<title>Tunecast offline demo</title>"),
    ('<link rel="stylesheet" href="/static/app.css?v={{asset_version}}">',
     '<link rel="stylesheet" href="app.css">\n<link rel="stylesheet" href="demo.css">'),
    ('<script type="module" src="/static/app.js?v={{asset_version}}"></script>',
     '<script src="shim.js"></script>\n<script type="module" src="app.js"></script>'),
    ("<body>\n", "<body>\n" + BANNER),
]


def build(out: Path) -> None:
    page = (STATIC / "index.html").read_text(encoding="utf-8")
    for old, new in SWAPS:
        if page.count(old) != 1:
            sys.exit(f"build_demo: expected exactly one {old!r} in tunecast/static/index.html; update SWAPS")
        page = page.replace(old, new)
    out.mkdir(exist_ok=True)
    (out / "index.html").write_text(page, encoding="utf-8", newline="\n")
    for name in ("app.js", "app.css"):
        (out / name).write_bytes((STATIC / name).read_bytes())


if __name__ == "__main__":
    build(Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "demo")
