import re

import pytest
from playwright.sync_api import Page, expect

from .conftest import KEY

pytestmark = pytest.mark.e2e

LYRICS = "[Verse]\nMorning light\n[Chorus]\nSoftly the world begins to breathe"
STYLE = "warm acoustic pop, fingerpicked guitar"
STATES = re.compile(r"queued|running|succeeded")
LAMP_ON = re.compile(r"\bon\b")


def enter_key(page: Page, key: str) -> None:
    page.get_by_placeholder("API key").fill(key)
    page.keyboard.press("Enter")


def unlocked(page: Page, server) -> Page:
    page.goto(server.url)
    expect(page.locator("#keybox")).to_be_visible()
    enter_key(page, KEY)
    expect(page.locator("#main")).to_be_visible()
    return page


def fill_take(page: Page, description: str = STYLE) -> None:
    page.locator("#lyrics").fill(LYRICS)
    page.locator("#description").fill(description)
    page.locator("#duration").fill("1")


def start_take(page: Page) -> None:
    page.get_by_role("button", name="Start take").click()


def test_invalid_key_is_refused_and_forgotten(page: Page, server):
    page.goto(server.url)
    enter_key(page, "wrong-key")
    expect(page.locator("#keymsg")).to_contain_text("refused")
    expect(page.locator("#keybox")).to_be_visible()
    expect(page.locator("#main")).to_be_hidden()
    expect(page.get_by_placeholder("API key")).to_be_focused()
    assert page.evaluate("localStorage.getItem('tunecast_key')") is None

    enter_key(page, KEY)
    expect(page.locator("#main")).to_be_visible()


def test_key_revoked_mid_session_returns_to_keybox_and_keeps_draft(page: Page, server):
    unlocked(page, server)
    fill_take(page)
    page.evaluate("localStorage.setItem('tunecast_key', 'rotated')")
    expect(page.locator("#keybox")).to_be_visible(timeout=10_000)
    expect(page.locator("#main")).to_be_hidden()

    enter_key(page, KEY)
    expect(page.locator("#main")).to_be_visible()
    expect(page.locator("#lyrics")).to_have_value(LYRICS)
    expect(page.locator("#description")).to_have_value(STYLE)


def test_draft_survives_submit_and_polling(page: Page, server):
    unlocked(page, server)
    fill_take(page)
    page.locator("#seed").fill("42")
    start_take(page)
    expect(page.locator(".take.succeeded")).to_have_count(1)
    expect(page.locator("#lyrics")).to_have_value(LYRICS)
    expect(page.locator("#description")).to_have_value(STYLE)
    expect(page.locator("#seed")).to_have_value("42")
    expect(page.locator(".take .seed")).to_have_text("seed 42")


def test_audio_loads_and_plays_after_job_succeeds(page: Page, server):
    unlocked(page, server)
    fill_take(page)
    start_take(page)
    take = page.locator(".take.succeeded")
    expect(take).to_have_count(1)
    expect(take.locator("audio")).to_be_hidden()

    take.get_by_role("button", name="Load audio").click()
    audio = take.locator("audio")
    expect(audio).to_be_visible()
    expect(take.get_by_role("link", name="Save WAV")).to_be_visible()
    page.wait_for_function("document.querySelector('audio').readyState >= 1")
    assert audio.evaluate("a => a.duration") == pytest.approx(1.0, abs=0.05)

    audio.evaluate("a => a.play()")
    page.wait_for_function("!document.querySelector('audio').paused && document.querySelector('audio').currentTime > 0.1")


def test_reconnect_resumes_polling_without_reload(page: Page, server):
    unlocked(page, server)
    expect(page.locator("#empty")).to_be_visible()
    server.stop()
    page.wait_for_timeout(3500)
    server.start()
    job = server.submit()
    expect(page.locator(".take")).to_have_count(1, timeout=10_000)
    expect(page.locator(".take .seed")).to_have_text(f"seed {job['params']['seed']}")
    expect(page.locator(".take .state")).to_have_text(STATES)


@pytest.mark.xfail(strict=True, reason="Task 14: no offline or reconnecting indicator while polling fails")
def test_offline_state_is_shown_while_server_is_unreachable(page: Page, server):
    unlocked(page, server)
    server.stop()
    expect(page.get_by_text(re.compile(r"offline|reconnecting", re.I))).to_be_visible(timeout=8_000)


@pytest.mark.xfail(strict=True, reason="Task 14: readiness lamps keep their last state when the check fails")
def test_lamps_clear_when_server_is_unreachable(page: Page, server):
    server.state.model = server.state.tunnel = True
    unlocked(page, server)
    expect(page.locator("#lamp-model")).to_have_class(LAMP_ON)
    server.stop()
    expect(page.locator("#lamp-model")).not_to_have_class(LAMP_ON, timeout=13_000)


def lose_submit_response(page: Page) -> None:
    def handler(route):
        if route.request.method == "POST":
            route.fetch()
            route.abort("connectionreset")
        else:
            route.continue_()

    page.route("**/jobs", handler)


def test_lost_submit_response_still_shows_the_accepted_take_once(page: Page, server):
    unlocked(page, server)
    lose_submit_response(page)
    fill_take(page)
    start_take(page)
    expect(page.locator(".take")).to_have_count(1, timeout=10_000)
    expect(page.get_by_role("button", name="Start take")).to_be_enabled()
    expect(page.locator("#lyrics")).to_have_value(LYRICS)
    page.wait_for_timeout(3500)
    expect(page.locator(".take")).to_have_count(1)


@pytest.mark.xfail(strict=True, reason="Task 10: a lost response is reported as 'Not started' instead of 'status unknown'")
def test_lost_submit_response_is_not_reported_as_not_started(page: Page, server):
    unlocked(page, server)
    lose_submit_response(page)
    fill_take(page)
    start_take(page)
    note = page.locator("#formnote")
    expect(note).to_contain_text(re.compile("status unknown", re.I))
    expect(note).not_to_contain_text("Not started")


def test_keyboard_tab_order_and_visible_focus(page: Page, server):
    unlocked(page, server)
    page.reload()
    expect(page.locator("#main")).to_be_visible()
    for field in ("lyrics", "description", "duration", "seed"):
        page.keyboard.press("Tab")
        expect(page.locator(f"#{field}")).to_be_focused()
        style = page.evaluate("[getComputedStyle(document.activeElement).outlineStyle, getComputedStyle(document.activeElement).outlineWidth]")
        assert style == ["solid", "2px"], field
    page.keyboard.press("Tab")
    expect(page.get_by_role("button", name="Start take")).to_be_focused()


def test_keyboard_only_key_entry_and_submit(page: Page, server):
    page.goto(server.url)
    expect(page.get_by_placeholder("API key")).to_be_focused()
    page.keyboard.type(KEY)
    page.keyboard.press("Enter")
    expect(page.locator("#main")).to_be_visible()

    fill_take(page)
    page.locator("#seed").focus()
    page.keyboard.press("Enter")
    expect(page.locator(".take")).to_have_count(1)
    expect(page.locator("#formnote")).to_have_text("Queued.")


def horizontal_overflow(page: Page) -> int:
    return page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")


def test_mobile_layout_has_no_overflow_and_controls_are_reachable(page: Page, server):
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(server.url)
    expect(page.locator("#keybox")).to_be_visible()
    assert horizontal_overflow(page) <= 0

    enter_key(page, KEY)
    expect(page.locator("#main")).to_be_visible()
    fill_take(page, description="x" * 300)
    start_take(page)
    take = page.locator(".take.succeeded")
    expect(take).to_have_count(1)
    take.get_by_role("button", name="Load audio").click()
    expect(take.locator("audio")).to_be_visible()

    assert horizontal_overflow(page) <= 0
    for control in (take.get_by_role("link", name="Save WAV"), take.get_by_role("button", name="Delete")):
        control.scroll_into_view_if_needed()
        box = control.bounding_box()
        assert 0 <= box["x"] and box["x"] + box["width"] <= 390
        control.hover()
