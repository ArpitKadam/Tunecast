"""The offline demo, served as plain static files from the repo root (what GitHub Pages or `python -m http.server` does)."""
import functools
import http.server
import re
import threading
from pathlib import Path
from urllib.parse import urlparse

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.e2e

ROOT = Path(__file__).resolve().parents[2]


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def site():
    handler = functools.partial(Quiet, directory=str(ROOT))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


@pytest.fixture
def demo(page: Page, site):
    page.requested = []
    page.on("request", lambda r: page.requested.append(r.url))
    page.goto(site + "/demo/")
    expect(page.locator("#main")).to_be_visible()
    return page


def audio_time(page: Page) -> float:
    return page.evaluate("document.querySelector('#audio').currentTime")


def test_demo_opens_unlocked_with_a_permanent_banner_and_the_real_take(demo: Page):
    expect(demo.locator("#keybox")).to_be_hidden()
    banner = demo.locator(".demo-banner")
    expect(banner).to_be_visible()
    expect(banner).to_contain_text("OFFLINE DEMO — recorded fixtures, no GPU, nothing is generated")
    take = demo.locator(".take")
    expect(take).to_have_count(1)
    expect(take.locator(".title")).to_have_value(re.compile("Measured run, 2026-09-03"))
    expect(take.locator(".len")).to_have_text("2:29")
    expect(take.locator(".stats")).to_contain_text("inference 2:33")
    expect(take.locator(".stats")).to_contain_text("RTF 1.03x")
    demo.get_by_role("tab", name="Lab").click()
    expect(banner).to_be_visible()      # sticky: still there on the other tab


def test_both_tabs_work_and_start_take_is_disabled_with_a_reason(demo: Page):
    demo.get_by_role("tab", name="Lab").click()
    expect(demo.locator("#panel-lab")).to_be_visible()
    expect(demo.locator("#panel-player")).to_be_hidden()
    start = demo.get_by_role("button", name="Start take")
    expect(start).to_be_disabled()
    expect(demo.locator("#demo-why")).to_contain_text("no model to run")
    expect(start).to_have_accessible_description(re.compile("no model to run"))
    demo.get_by_role("tab", name="Player").click()
    expect(demo.locator("#panel-player")).to_be_visible()


def test_edit_a_copy_fills_the_lab_but_nothing_can_be_started(demo: Page):
    demo.get_by_role("button", name="Edit a copy").click()
    expect(demo.locator("#panel-lab")).to_be_visible()
    expect(demo.locator("#lineage")).to_contain_text("Copy of Measured run")
    expect(demo.locator("#sections .section")).not_to_have_count(0)
    expect(demo.get_by_role("button", name="Start take")).to_be_disabled()


def test_sample_take_plays_and_keeps_playing_across_tabs(demo: Page):
    demo.locator(".take .play").click()
    demo.wait_for_function("() => { const a = document.querySelector('#audio'); return !a.paused && a.currentTime > 0.3; }")
    demo.get_by_role("tab", name="Lab").click()
    before = audio_time(demo)
    demo.wait_for_function("t => document.querySelector('#audio').currentTime > t + 0.3", arg=before)
    demo.get_by_role("tab", name="Player").click()
    assert not demo.evaluate("document.querySelector('#audio').paused")
    assert demo.evaluate("document.querySelector('#audio').duration") == pytest.approx(149.188, abs=0.05)


def test_labels_edit_in_memory_and_reload_restores_the_fixture(demo: Page):
    demo.locator(".take .fav").click()
    expect(demo.locator(".take .fav")).to_have_attribute("aria-pressed", "true")
    demo.reload()
    expect(demo.locator(".take .fav")).to_have_attribute("aria-pressed", "false")


def test_nothing_leaves_localhost_and_the_api_is_answered_in_the_page(demo: Page, site):
    demo.locator(".take .play").click()
    demo.wait_for_function("() => document.querySelector('#audio').currentTime > 0.3")
    demo.wait_for_timeout(3500)      # at least one poll
    assert demo.requested
    for url in demo.requested:
        if url.startswith(("blob:", "data:")):
            continue
        assert url.startswith(site), url
    paths = {urlparse(u).path for u in demo.requested}
    assert not paths & {"/ready", "/info", "/jobs"}, paths
