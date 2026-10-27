"""Render the README media from the real app on the stub sidecar. No pod, no model.

    uv run python scripts/capture_media.py

Writes media/player.png, lab.png, mobile-player.png, mobile-lab.png, offline-stale.png and
two-tabs.gif (two-tabs.webm instead when ffmpeg is not on PATH). The audio in these takes is the
stub's 400 Hz tone, so the per-take timing line is hidden: stub timings are not measurements.
Needs the dev group and `uv run playwright install chromium`.
"""

import json
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from playwright.sync_api import Page, sync_playwright  # noqa: E402

import tunecast.app  # noqa: E402
from tests.e2e.conftest import KEY, LiveServer  # noqa: E402

MEDIA = ROOT / "media"
DESKTOP = {"width": 1280, "height": 800}
PHONE = {"width": 390, "height": 844}
POLL = "window.__tunecast = {pollMs: 300, capMs: 1500}"      # the page's own test hook, so going offline shows within seconds
HIDE_STUB_TIMINGS = ".take .stats { display: none }"
LINES = "[Verse]\nYou came in from the cold without a word\nSet the kettle down and let it be\n[Chorus]\nSo let it stay quiet\nLet the kitchen light stay low"
SPARSE = "[Verse]\nYou came in from the cold without a word\n[Chorus]\nSo let it stay quiet"
NEON = "[Verse]\nNeon on the wet street\nEngines hum beneath my feet\n[Chorus]\nKeep the city in my veins"
PINE = "[Verse]\nMorning light filtering through the pine\n[Chorus]\nSoftly the world begins to breathe"

# oldest first, so the newest take sits at the top of the Player; (title, lyrics, style, seconds, seed, favorite, parent index)
TAKES = [
    ("Morning pine", PINE, "warm acoustic pop, upbeat, female vocals, acoustic guitar", 9, 19, False, None),
    ("", NEON, "dark electronic, mid-tempo, male vocals, synths, bass", 8, 42, False, None),
    ("Kitchen light", LINES, "folk, melancholic, slow tempo, female vocals, piano, soft ambient pads", 12, 7, True, None),
    ("Kitchen light, sparser", SPARSE, "folk, melancholic, slow tempo, female vocals, piano", 12, 7, False, 2),
]


def call(srv: LiveServer, method: str, path: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(
        srv.url + path, method=method, data=json.dumps(body).encode() if body else None,
        headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.load(resp)


def seed_takes(srv: LiveServer) -> list[str]:
    ids: list[str] = []
    for title, lyrics, style, seconds, seed, favorite, parent in TAKES:
        body = {"lyrics": lyrics, "description": style, "duration_s": seconds, "seed": seed}
        if parent is not None:
            body["parent_job_id"] = ids[parent]
        job = srv.submit(**body)
        ids.append(job["id"])
        while srv.status(job["id"]) != "succeeded":
            time.sleep(0.05)
        labels = {"favorite": favorite} | ({"title": title} if title else {})
        call(srv, "PATCH", f"/jobs/{job['id']}", labels)
    return ids


def open_page(browser, srv: LiveServer, viewport: dict, **context) -> Page:
    ctx = browser.new_context(viewport=viewport, **context)
    ctx.add_init_script(POLL)
    ctx.add_init_script(f"try {{ sessionStorage.setItem('tunecast_key', '{KEY}'); }} catch {{}}")
    page = ctx.new_page()
    page.goto(srv.url)
    page.locator(".take.succeeded").nth(len(TAKES) - 1).wait_for()
    page.add_style_tag(content=HIDE_STUB_TIMINGS)
    return page


def play_newest(page: Page) -> None:
    page.locator(".take").first.get_by_role("button", name="Play").click()
    page.wait_for_function("() => document.querySelector('#audio').currentTime > 1")
    page.wait_for_timeout(400)      # let the clock and the scrub bar catch up with the audio


def shots(browser, srv: LiveServer) -> None:
    for prefix, viewport in (("", DESKTOP), ("mobile-", PHONE)):
        page = open_page(browser, srv, viewport)
        play_newest(page)
        page.screenshot(path=MEDIA / f"{prefix}player.png")
        page.locator(".take").first.get_by_role("button", name="Edit a copy").click()
        page.locator("#ab-a").select_option(index=2)      # options run newest first: 1 sparser, 2 Kitchen light
        page.locator("#ab-b").select_option(index=1)
        page.evaluate("window.scrollTo(0, 0)")
        page.screenshot(path=MEDIA / f"{prefix}lab.png")
        page.context.close()


def stale_shot(browser, srv: LiveServer) -> None:
    page = open_page(browser, srv, DESKTOP)
    page.wait_for_timeout(1000)
    srv.stop()
    page.get_by_text("offline", exact=True).wait_for(timeout=15000)
    page.wait_for_timeout(1500)
    page.screenshot(path=MEDIA / "offline-stale.png")
    page.context.close()
    srv.start()


def recording(browser, srv: LiveServer) -> Path:
    tmp = Path(tempfile.mkdtemp())
    page = open_page(browser, srv, DESKTOP, record_video_dir=str(tmp), record_video_size={"width": 960, "height": 600})
    page.wait_for_timeout(800)
    page.locator(".take").first.get_by_role("button", name="Play").click()
    page.wait_for_timeout(1800)
    page.locator(".take").first.get_by_role("button", name="Edit a copy").click()      # opens the Lab with a filled draft
    page.wait_for_timeout(2200)
    page.get_by_role("tab", name="Player").click()
    page.wait_for_timeout(1800)
    video = Path(page.video.path())
    page.context.close()
    return video


def to_gif(webm: Path, gif: Path) -> bool:
    if not shutil.which("ffmpeg"):
        return False
    graph = "fps=10,scale=720:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=48[p];[b][p]paletteuse=dither=bayer:bayer_scale=5"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(webm), "-vf", graph, str(gif)], check=True)
    return True


def main() -> None:
    MEDIA.mkdir(exist_ok=True)
    tunecast.app.query_gpus = lambda: []      # /info would report this laptop's GPU; the masthead should say "stub mode"
    srv = LiveServer(Path(tempfile.mkdtemp()))
    srv.start()
    srv.state.tunnel = True
    seed_takes(srv)
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--autoplay-policy=no-user-gesture-required"])
        shots(browser, srv)
        webm = recording(browser, srv)
        stale_shot(browser, srv)
        browser.close()
    if not to_gif(webm, MEDIA / "two-tabs.gif"):
        shutil.copy(webm, MEDIA / "two-tabs.webm")
    srv.stop()
    for f in sorted(MEDIA.iterdir()):
        print(f"{f.stat().st_size / 1024:7.0f} KB  {f.name}")


if __name__ == "__main__":
    main()
