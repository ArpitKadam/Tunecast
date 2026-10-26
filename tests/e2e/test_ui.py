import json
import re
import time
from pathlib import Path

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


def open_tab(page: Page, name: str) -> None:
    page.get_by_role("tab", name=name).click()


def fill_take(page: Page, description: str = STYLE) -> None:
    open_tab(page, "Lab")
    page.get_by_label("Text", exact=True).check()
    page.locator("#lyrics").fill(LYRICS)
    page.locator("#description").fill(description)
    page.locator("#duration").fill("1")


def start_take(page: Page) -> None:
    page.get_by_role("button", name="Start take").click()


def ring(page: Page, element: str = "document.activeElement") -> list:
    return page.evaluate(f"(() => {{ const s = getComputedStyle({element}); return [s.outlineStyle, s.outlineWidth]; }})()")


def playback(page: Page) -> dict:
    return page.evaluate("(() => { const a = document.querySelector('#audio'); return {t: a.currentTime, paused: a.paused, take: a.dataset.take}; })()")


def wait_playing(page: Page, take_id: str | None = None, after: float = 0.1) -> None:
    page.wait_for_function(
        "([id, t]) => { const a = document.querySelector('#audio'); return !a.paused && a.currentTime > t && (!id || a.dataset.take === id); }",
        arg=[take_id, after],
    )


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
    audio = page.locator("#audio")
    expect(audio).not_to_have_attribute("src", re.compile("."))
    expect(page.locator("#now-title")).to_have_text("Nothing playing")

    open_tab(page, "Player")
    take.get_by_role("button", name="Play").click()
    expect(audio).to_have_attribute("src", re.compile("^blob:"))
    expect(page.locator("#now-title")).to_have_text("Take 1")
    expect(take.get_by_role("link", name="Save WAV")).to_be_visible()
    page.wait_for_function("document.querySelector('audio').readyState >= 1")
    assert audio.evaluate("a => a.duration") == pytest.approx(1.0, abs=0.05)

    audio.evaluate("a => { a.currentTime = 0; return a.play(); }")
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


def test_lost_submit_response_shows_status_unknown_until_reconciled(page: Page, server):
    unlocked(page, server)
    lose_submit_response(page)

    def slow_lookup(route):
        time.sleep(1.5)
        route.continue_()

    page.route(re.compile(r"/jobs\?request_id="), slow_lookup)
    fill_take(page)
    start_take(page)
    note = page.locator("#formnote")
    expect(note).to_contain_text(re.compile("status unknown", re.I))
    expect(note).not_to_contain_text("Not started")
    expect(page.locator(".take")).to_have_count(1, timeout=10_000)
    expect(note).not_to_contain_text("Not started")


def test_gateway_error_after_server_accepted_submit_is_reconciled_not_duplicated(page: Page, server):
    unlocked(page, server)

    def gateway_timeout(route):
        if route.request.method == "POST":
            route.fetch()
            route.fulfill(status=504, body="gateway timeout")
        else:
            route.continue_()

    page.route("**/jobs", gateway_timeout)
    fill_take(page)
    start_take(page)
    expect(page.locator(".take")).to_have_count(1, timeout=15_000)
    expect(page.locator("#formnote")).not_to_contain_text("Not started")
    expect(page.locator(".take")).to_have_count(1)


def test_submit_never_reaching_the_server_is_reported_not_submitted_and_retry_reuses_the_key(page: Page, server):
    unlocked(page, server)
    keys = []

    def drop_before_server(route):
        if route.request.method == "POST":
            keys.append(route.request.headers.get("idempotency-key"))
            route.abort("connectionreset")
        else:
            route.continue_()

    page.route("**/jobs", drop_before_server)
    fill_take(page)
    start_take(page)
    expect(page.locator("#formnote")).to_contain_text("Not submitted", timeout=15_000)
    expect(page.get_by_role("button", name="Start take")).to_be_enabled()
    expect(page.locator("#lyrics")).to_have_value(LYRICS)
    expect(page.locator(".take")).to_have_count(0)

    start_take(page)
    expect(page.locator("#formnote")).to_contain_text("Not submitted", timeout=15_000)
    assert len(keys) == 2 and keys[0] and keys[0] == keys[1]


def test_keyboard_tab_order_and_visible_focus(page: Page, server):
    unlocked(page, server)
    page.reload()
    expect(page.locator("#main")).to_be_visible()
    page.keyboard.press("Tab")
    expect(page.get_by_role("tab", name="Player")).to_be_focused()
    page.keyboard.press("ArrowRight")
    expect(page.get_by_role("tab", name="Lab")).to_be_focused()
    page.keyboard.press("Tab")
    expect(page.get_by_label("Sections", exact=True)).to_be_focused()
    page.keyboard.press("ArrowRight")
    expect(page.get_by_label("Text", exact=True)).to_be_checked()
    assert ring(page, "document.activeElement.nextElementSibling") == ["solid", "2px"]    # the radio is see-through; its label shows focus

    stops = [page.locator("#lyrics")]
    stops += [page.get_by_role("toolbar", name=group).get_by_role("button").first for group in ("Genre", "Mood", "Tempo", "Vocals", "Instruments")]
    stops += [page.locator(field) for field in ("#description", "#duration", "#seed", "#seedlock")]
    for stop in stops:
        page.keyboard.press("Tab")
        expect(stop).to_be_focused()
        assert ring(page) == ["solid", "2px"], stop
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
    open_tab(page, "Player")
    take.get_by_role("button", name="Play").click()
    expect(page.locator("#audio")).to_have_attribute("src", re.compile("^blob:"))
    expect(page.locator("#now-title")).to_have_text("Take 1")

    assert horizontal_overflow(page) <= 0
    for control in (take.get_by_role("link", name="Save WAV"), take.get_by_role("button", name="Delete")):
        control.scroll_into_view_if_needed()
        box = control.bounding_box()
        assert 0 <= box["x"] and box["x"] + box["width"] <= 390
        control.hover()


def test_refined_take_shows_seed_timings_real_time_factor_and_prompt_diff(page: Page, server):
    unlocked(page, server)
    fill_take(page)
    page.locator("#seed").fill("42")
    start_take(page)
    expect(page.locator(".take.succeeded")).to_have_count(1)
    open_tab(page, "Player")
    root = page.locator(".take").first
    expect(root.locator(".seed")).to_have_text("seed 42")
    expect(root.locator(".stats")).to_contain_text(re.compile(r"queued \d+\.\ds \S inference \d+\.\ds \S RTF \d+\.\d\dx"))
    expect(root.locator(".diff")).to_have_text("")

    root.get_by_role("button", name="Edit a copy").click()
    page.get_by_label("Lock seed").uncheck()     # a copy keeps the parent's seed unless unlocked
    page.locator("#description").fill("brighter pop, handclaps")
    start_take(page)
    expect(page.locator(".take.succeeded")).to_have_count(2)
    open_tab(page, "Player")
    child = page.locator(".take").first
    expect(child.locator(".diff")).to_have_text("vs parent: description, seed changed")
    expect(child.locator(".stats")).to_contain_text("RTF")

    with page.expect_download() as download:
        child.get_by_role("link", name="Export experiment").click()
    assert re.fullmatch(r"experiment-[\w-]+\.json", download.value.suggested_filename)
    rows = json.loads(Path(download.value.path()).read_text(encoding="utf-8"))
    assert len(rows) == 2
    assert rows[1]["parent_job_id"] == rows[0]["job_id"] and rows[1]["experiment_id"] == rows[0]["job_id"]


def test_full_queue_shows_retry_after_and_keeps_the_draft(page: Page, full_queue_server):
    unlocked(page, full_queue_server)
    expect(page.locator(".take")).to_have_count(2)
    fill_take(page)
    start_take(page)
    note = page.locator("#formnote")
    expect(note).to_contain_text("Not started: queue is full")
    expect(note).to_contain_text("try again in 30 s")
    expect(page.locator(".take")).to_have_count(2)
    expect(page.locator("#lyrics")).to_have_value(LYRICS)
    expect(page.locator("#description")).to_have_value(STYLE)


def test_queued_take_can_be_cancelled_but_a_running_one_cannot(page: Page, full_queue_server):
    unlocked(page, full_queue_server)
    expect(page.locator(".take")).to_have_count(2)
    running = page.locator(".take.running")
    expect(running.get_by_role("button", name="Can't cancel")).to_be_disabled()
    queued = page.locator(".take.queued")
    queued.get_by_role("button", name="Cancel").click()
    queued.get_by_role("button", name="Really cancel").click()
    expect(page.locator(".take")).to_have_count(1)
    expect(page.locator(".take.running")).to_have_count(1)


# --- Task 13: Player and Lab tabs on one shared state ---------------------------------------------------

def test_tabs_render_and_are_reachable_by_keyboard(page: Page, server):
    unlocked(page, server)
    page.reload()
    expect(page.locator("#main")).to_be_visible()
    player, lab = page.get_by_role("tab", name="Player"), page.get_by_role("tab", name="Lab")
    expect(page.get_by_role("tab")).to_have_count(2)
    expect(player).to_have_attribute("aria-controls", "panel-player")
    expect(lab).to_have_attribute("aria-controls", "panel-lab")
    page.keyboard.press("Tab")
    expect(player).to_be_focused()
    assert ring(page) == ["solid", "2px"]
    expect(player).to_have_attribute("aria-selected", "true")
    expect(page.get_by_role("tabpanel")).to_have_count(1)

    for key, tab, panel in (("ArrowRight", lab, "#panel-lab"), ("ArrowRight", player, "#panel-player"),
                            ("End", lab, "#panel-lab"), ("Home", player, "#panel-player"), ("ArrowLeft", lab, "#panel-lab")):
        page.keyboard.press(key)
        expect(tab).to_be_focused()
        expect(tab).to_have_attribute("aria-selected", "true")
        expect(page.locator(panel)).to_be_visible()
        expect(page.get_by_role("tabpanel")).to_have_count(1)
    expect(player).to_have_attribute("aria-selected", "false")
    expect(page.locator("#panel-player")).to_be_hidden()

    page.keyboard.press("Tab")
    assert page.evaluate("document.getElementById('panel-lab').contains(document.activeElement)")
    page.keyboard.press("Shift+Tab")
    expect(lab).to_be_focused()


def test_tab_switch_does_not_interrupt_playback(page: Page, server):
    server.submit(duration_s=6)
    unlocked(page, server)
    take = page.locator(".take.succeeded")
    expect(take).to_have_count(1, timeout=10_000)
    take.get_by_role("button", name="Play").click()
    wait_playing(page, after=0.2)
    before = playback(page)

    open_tab(page, "Lab")
    expect(page.locator("#panel-player")).to_be_hidden()
    page.wait_for_timeout(400)
    mid = playback(page)
    assert not mid["paused"] and mid["t"] > before["t"] and mid["take"] == before["take"]

    page.keyboard.press("ArrowLeft")
    expect(page.get_by_role("tab", name="Player")).to_have_attribute("aria-selected", "true")
    page.wait_for_timeout(400)
    after = playback(page)
    assert not after["paused"] and after["t"] > mid["t"] and after["take"] == before["take"]
    expect(take.get_by_role("button", name="Pause")).to_be_visible()


def test_drafts_survive_tab_switches(page: Page, server):
    unlocked(page, server)
    open_tab(page, "Lab")
    add = page.get_by_role("toolbar", name="Add section")
    add.get_by_role("button", name="Verse", exact=True).click()
    page.keyboard.type("Morning light")
    add.get_by_role("button", name="Chorus", exact=True).click()
    page.keyboard.type("Softly")
    page.locator("#description").fill("folk, brushed snare")
    page.locator("#seed").fill("1234")
    page.locator("#duration").fill("90")

    for _ in range(2):
        open_tab(page, "Player")
        expect(page.locator("#panel-lab")).to_be_hidden()
        open_tab(page, "Lab")
        cards = page.locator(".section")
        expect(cards).to_have_count(2)
        expect(cards.nth(1).locator(".lines")).to_have_value("Softly")
        expect(page.locator("#lyrics")).to_have_value("[Verse]\nMorning light\n[Chorus]\nSoftly")
        expect(page.locator("#description")).to_have_value("folk, brushed snare")
        expect(page.get_by_role("toolbar", name="Genre").get_by_role("button", name="folk")).to_have_attribute("aria-pressed", "true")
        expect(page.locator("#seed")).to_have_value("1234")
        expect(page.get_by_label("Lock seed")).to_be_checked()
        expect(page.locator("#durlabel")).to_have_text("1:30")
        page.get_by_label("Text", exact=True).check()      # the second round runs in the text view
    expect(page.locator("#lyrics")).to_be_visible()


def test_search_filters_takes(page: Page, server):
    server.submit(description="dark electronic, synths", seed=4242)
    server.submit(description="warm folk, violin", lyrics="[Verse]\nriver stones")
    server.submit(description="bright pop")
    unlocked(page, server)
    expect(page.locator(".take.succeeded")).to_have_count(3, timeout=10_000)
    shown, search = page.locator(".take:visible"), page.get_by_label("Search takes")
    expect(page.get_by_text("newest 50 takes")).to_be_visible()
    for query, count in (("violin", 1), ("4242", 1), ("RIVER", 1), ("warm violin", 1), ("succeeded", 3), ("pop", 1)):
        search.fill(query)
        expect(shown).to_have_count(count)
    expect(page.locator("#count")).to_have_text("1 of 3 takes")
    expect(shown.locator(".desc")).to_have_text("bright pop")

    search.fill("violin synths")
    expect(shown).to_have_count(0)
    expect(page.locator("#nomatch")).to_be_visible()
    search.fill("")
    expect(shown).to_have_count(3)
    expect(page.locator("#nomatch")).to_be_hidden()
    expect(page.locator("#count")).to_have_text("3 takes")


def patched(page: Page):
    return page.expect_response(lambda r: r.request.method == "PATCH" and r.ok)


def test_favorite_and_title_persist_across_reload(page: Page, server):
    server.submit()
    server.submit()
    unlocked(page, server)
    expect(page.locator(".take.succeeded")).to_have_count(2, timeout=10_000)
    newest = page.locator(".take").first
    with patched(page):
        newest.get_by_role("button", name="Favorite").click()
    expect(newest.get_by_role("button", name="Favorite")).to_have_attribute("aria-pressed", "true")
    title = newest.get_by_label("Title")
    title.fill("Night drive")
    with patched(page):
        title.press("Enter")

    page.reload()
    expect(page.locator("#main")).to_be_visible()
    newest, oldest = page.locator(".take").first, page.locator(".take").nth(1)
    expect(newest.get_by_label("Title")).to_have_value("Night drive")
    expect(newest.get_by_role("button", name="Favorite")).to_have_attribute("aria-pressed", "true")
    expect(oldest.get_by_label("Title")).to_have_value("")
    expect(oldest.get_by_role("button", name="Favorite")).to_have_attribute("aria-pressed", "false")

    page.get_by_label("Favorites only").check()
    expect(page.locator(".take:visible")).to_have_count(1)
    expect(page.locator(".take:visible").get_by_label("Title")).to_have_value("Night drive")
    page.get_by_label("Favorites only").uncheck()
    page.get_by_label("Search takes").fill("night drive")
    expect(page.locator(".take:visible")).to_have_count(1)


def test_edit_a_copy_loads_the_lab_and_creates_a_child(page: Page, server):
    root = server.submit(lyrics="[Verse]\nfirst line\n\n[Chorus]\nhook line", description="pop, warm", seed=11, duration_s=2)
    unlocked(page, server)
    expect(page.locator(".take.succeeded")).to_have_count(1, timeout=10_000)
    page.locator(".take").first.get_by_role("button", name="Edit a copy").click()

    expect(page.locator("#panel-lab")).to_be_visible()
    lineage = page.locator("#lineage")
    expect(lineage).to_contain_text("Copy of Take 1")
    expect(lineage).to_contain_text("Nothing changed yet.")
    cards = page.locator(".section")
    expect(cards).to_have_count(2)
    expect(cards.nth(0).locator(".tag")).to_have_value("Verse")
    expect(cards.nth(0).locator(".lines")).to_have_value("first line")
    expect(cards.nth(0).locator(".lines")).to_be_focused()
    expect(cards.nth(1).locator(".tag")).to_have_value("Chorus")
    expect(cards.nth(1).locator(".lines")).to_have_value("hook line")
    expect(page.locator("#description")).to_have_value("pop, warm")
    expect(page.get_by_role("toolbar", name="Mood").get_by_role("button", name="warm")).to_have_attribute("aria-pressed", "true")
    expect(page.locator("#seed")).to_have_value("11")
    expect(page.get_by_label("Lock seed")).to_be_checked()
    expect(page.locator("#durlabel")).to_have_text("0:02")

    cards.nth(1).locator(".lines").fill("hook line, louder")
    expect(lineage).to_contain_text("Changed: lyrics.")
    start_take(page)
    expect(page.locator(".take")).to_have_count(2, timeout=10_000)
    expect(lineage).to_be_hidden()
    expect(page.locator(".take").first.locator(".diff")).to_have_text("vs parent: lyrics changed")

    child = page.request.get(server.url + "/jobs", headers={"Authorization": f"Bearer {KEY}"}).json()[0]
    assert child["parent_job_id"] == root["id"] and child["experiment_id"] == root["id"]
    assert child["params"]["lyrics"] == "[Verse]\nfirst line\n\n[Chorus]\nhook line, louder"
    assert (child["params"]["seed"], child["params"]["description"], child["params"]["duration_s"]) == (11, "pop, warm", 2)


def test_edit_a_copy_asks_before_replacing_a_different_draft(page: Page, server):
    server.submit(lyrics="[Verse]\nfirst line", description="pop, warm", seed=11)
    unlocked(page, server)
    expect(page.locator(".take.succeeded")).to_have_count(1, timeout=10_000)
    fill_take(page)
    open_tab(page, "Player")
    copy = page.locator(".take").first.get_by_role("button", name="Edit a copy")
    prompts = []

    def answer(accept):
        def handler(dialog):
            prompts.append(dialog.message)
            dialog.accept() if accept else dialog.dismiss()
        return handler

    page.once("dialog", answer(False))
    copy.click()
    assert len(prompts) == 1 and "Take 1" in prompts[0]
    expect(page.locator("#panel-player")).to_be_visible()
    expect(page.locator("#lineage")).to_be_hidden()
    open_tab(page, "Lab")
    expect(page.locator("#lyrics")).to_have_value(LYRICS)
    expect(page.locator("#description")).to_have_value(STYLE)

    open_tab(page, "Player")
    page.once("dialog", answer(True))
    copy.click()
    assert len(prompts) == 2
    expect(page.locator("#lyrics")).to_have_value("[Verse]\nfirst line")
    expect(page.locator("#description")).to_have_value("pop, warm")
    expect(page.locator("#lineage")).to_contain_text("Copy of Take 1")

    open_tab(page, "Player")
    page.on("dialog", lambda d: prompts.append(d.message) or d.dismiss())
    copy.click()      # the draft now equals the take, so there is nothing to lose
    expect(page.locator("#panel-lab")).to_be_visible()
    assert len(prompts) == 2


def test_double_clicking_the_star_toggles_on_then_off(page: Page, server):
    server.submit()
    unlocked(page, server)
    expect(page.locator(".take.succeeded")).to_have_count(1, timeout=10_000)
    star = page.locator(".take").first.get_by_role("button", name="Favorite")
    patches = []
    page.on("response", lambda r: patches.append(r) if r.request.method == "PATCH" else None)
    star.dblclick()
    expect(star).to_have_attribute("aria-pressed", "false")
    deadline = time.monotonic() + 5
    while len(patches) < 2 and time.monotonic() < deadline:
        page.wait_for_timeout(50)
    assert [p.request.post_data_json for p in patches] == [{"favorite": True}, {"favorite": False}]
    page.wait_for_timeout(3500)      # a poll must not bring a stale value back
    expect(star).to_have_attribute("aria-pressed", "false")
    assert page.request.get(server.url + "/jobs", headers={"Authorization": f"Bearer {KEY}"}).json()[0]["favorite"] is False
    page.reload()
    expect(page.locator(".take").first.get_by_role("button", name="Favorite")).to_have_attribute("aria-pressed", "false")


def test_playing_take_that_leaves_the_list_plays_on_until_it_ends(page: Page, server):
    server.submit(duration_s=5)
    server.submit(duration_s=1)
    unlocked(page, server)
    expect(page.locator(".take.succeeded")).to_have_count(2, timeout=10_000)
    old = page.locator(".take").nth(1)
    title = old.get_by_label("Title")
    title.fill("Old song")
    with patched(page):
        title.press("Enter")
    old.get_by_role("button", name="Play").click()
    wait_playing(page, after=0.2)
    gone = playback(page)["take"]

    def without_playing(route):
        response = route.fetch()
        route.fulfill(response=response, json=[j for j in response.json() if j["id"] != gone])

    page.route(re.compile(r"/jobs\?limit=50"), without_playing)
    expect(page.locator(".take")).to_have_count(1, timeout=10_000)
    audio = page.locator("#audio")
    expect(audio).to_have_attribute("src", re.compile("^blob:"))
    expect(page.locator("#now-title")).to_have_text("Old song")
    assert not playback(page)["paused"] and playback(page)["take"] == gone

    expect(audio).not_to_have_attribute("src", re.compile("."), timeout=10_000)      # forgotten once it ends
    expect(page.locator("#now-title")).to_have_text("Nothing playing")


def test_ab_flip_keeps_the_playback_position(page: Page, server):
    a = server.submit(duration_s=6, seed=1)
    b = server.submit(duration_s=6, seed=2)
    unlocked(page, server)
    expect(page.locator(".take.succeeded")).to_have_count(2, timeout=10_000)
    audio_requests = []
    page.on("request", lambda r: audio_requests.append(r.url) if "/audio" in r.url else None)
    open_tab(page, "Lab")
    expect(page.locator("#ab")).to_be_hidden()
    for pick in (a, b, a):     # arrowing through a closed select fires change per option
        page.locator("#ab-a").select_option(pick["id"])
    page.locator("#ab-b").select_option(b["id"])
    expect(page.locator("#ab")).to_be_visible()
    page.wait_for_timeout(500)
    assert audio_requests == []      # a 23 MB WAV per option would burn the tunnel quota
    flip_a, flip_b = page.get_by_role("button", name="Switch to A"), page.get_by_role("button", name="Switch to B")

    flip_a.click()
    wait_playing(page, a["id"], after=1.0)
    assert len(audio_requests) == 1 and a["id"] in audio_requests[0]
    expect(flip_a).to_have_attribute("aria-pressed", "true")
    before = playback(page)["t"]
    flip_b.click()
    page.wait_for_function("id => { const a = document.querySelector('#audio'); return a.dataset.take === id && a.readyState >= 1; }", arg=b["id"])
    now = playback(page)
    assert not now["paused"]
    assert before - 0.05 <= now["t"] < before + 0.75, (before, now)
    expect(flip_b).to_have_attribute("aria-pressed", "true")
    expect(flip_a).to_have_attribute("aria-pressed", "false")

    page.locator("#audio").evaluate("a => a.pause()")
    held = playback(page)["t"]
    flip_a.click()
    page.wait_for_function("id => { const a = document.querySelector('#audio'); return a.dataset.take === id && a.readyState >= 1; }", arg=a["id"])
    back = playback(page)
    assert back["paused"] and back["t"] == pytest.approx(held, abs=0.05)


def test_section_editor_round_trips_to_the_lyrics_text(page: Page, server):
    unlocked(page, server)
    open_tab(page, "Lab")
    add = page.get_by_role("toolbar", name="Add section")
    for tag, line in (("Verse", "Morning light"), ("Chorus", "Softly the world"), ("Bridge", "Hold on")):
        add.get_by_role("button", name=tag, exact=True).click()
        page.keyboard.type(line)
    cards = page.locator(".section")
    tags = cards.locator(".tag")

    page.get_by_role("button", name="Move section 3, Bridge up").click()
    expect(page.get_by_role("button", name="Move section 2, Bridge up")).to_be_focused()
    expect(tags.nth(1)).to_have_value("Bridge")
    expect(tags.nth(2)).to_have_value("Chorus")
    page.get_by_role("button", name="Remove section 2, Bridge").click()
    expect(cards).to_have_count(2)
    tags.nth(0).fill("[Verse 1]")
    expect(tags.nth(0)).to_have_value("Verse 1")

    page.get_by_label("Text", exact=True).check()
    expect(page.locator("#lyrics")).to_have_value("[Verse 1]\nMorning light\n[Chorus]\nSoftly the world")

    pasted = "intro words\n[Verse]\na\nb\n\n[Outro]\nbye\n"
    page.locator("#lyrics").fill(pasted)
    page.get_by_label("Sections", exact=True).check()
    expect(cards).to_have_count(3)
    expect(tags.nth(0)).to_have_value("")
    expect(cards.nth(1).locator(".lines")).to_have_value("a\nb")
    expect(tags.nth(2)).to_have_value("Outro")
    page.get_by_label("Text", exact=True).check()
    expect(page.locator("#lyrics")).to_have_value(pasted)


LYRIC_CASES = [
    "",
    "\n",
    "[Verse]",
    "[Verse]\nMorning light\n[Chorus]\nSoftly",
    "intro words\n[Verse]\na\n\n\n[Chorus]\nb\n",
    "[Intro]\n\n[Verse]\n  [not a tag]\n[ spaced ]\n[Verse]\r\nline",
    "[Verse]\n[Chorus]\n\n",
]


def test_lyrics_style_and_search_helpers(page: Page, server):
    page.goto(server.url)
    got = page.evaluate("""async (cases) => {
        const m = await import(document.querySelector("script[type=module]").src);
        const job = {title: "Night drive", status: "succeeded", params: {description: "warm folk", lyrics: "[Verse]\\nriver", seed: 42}};
        return {
            roundTrips: cases.map((c) => m.composeLyrics(m.parseLyrics(c))),
            parsed: m.parseLyrics("lead\\n[Verse]\\na\\nb\\n\\n[Chorus]\\nc"),
            built: m.composeLyrics([{tag: "Verse", lines: ["x"], gap: 1}, {tag: "", lines: ["loose"], gap: 0}, {tag: "Chorus", lines: [], gap: 0}]),
            styles: [
                m.toggleStyle("", "pop", ["pop", "rock"]),
                m.toggleStyle("pop, warm", "rock", ["pop", "rock"]),
                m.toggleStyle("Pop ,warm", "pop", ["pop", "rock"]),
                m.toggleStyle("A warm song, with strings", "piano"),
                m.toggleStyle("A warm song, with strings, piano", "PIANO"),
            ],
            words: [...m.styleWords(" Pop, warm ,, mid-tempo ")],
            matches: ["night 42", "night 43", "", "SUCCEEDED river", "folk drive"].map((q) => m.matches(job, q)),
            changed: m.changedParams({lyrics: "a", description: "b", seed: 1, duration_s: 9}, {lyrics: "a", description: "c", seed: 2, duration_s: 9}),
        };
    }""", LYRIC_CASES)
    assert got["roundTrips"] == LYRIC_CASES
    assert got["parsed"] == [
        {"tag": "", "lines": ["lead"], "gap": 0},
        {"tag": "Verse", "lines": ["a", "b"], "gap": 1},
        {"tag": "Chorus", "lines": ["c"], "gap": 0},
    ]
    assert got["built"] == "[Verse]\nx\n\nloose\n[Chorus]"
    assert got["styles"] == ["pop", "rock, warm", "warm", "A warm song, with strings, piano", "A warm song, with strings"]
    assert got["words"] == ["pop", "warm", "mid-tempo"]
    assert got["matches"] == [True, False, True, True, True]
    assert got["changed"] == ["description", "seed"]


def test_preset_chips_compose_the_description(page: Page, server):
    unlocked(page, server)
    open_tab(page, "Lab")

    def chip(group, name):
        return page.get_by_role("toolbar", name=group).get_by_role("button", name=name, exact=True)

    description = page.locator("#description")
    for group, name in (("Genre", "pop"), ("Mood", "warm"), ("Instruments", "piano"), ("Instruments", "strings")):
        chip(group, name).click()
    expect(description).to_have_value("pop, warm, piano, strings")
    expect(chip("Genre", "pop")).to_have_attribute("aria-pressed", "true")
    chip("Genre", "rock").click()
    expect(description).to_have_value("rock, warm, piano, strings")
    expect(chip("Genre", "pop")).to_have_attribute("aria-pressed", "false")
    chip("Instruments", "piano").click()
    expect(description).to_have_value("rock, warm, strings")

    description.fill("Rock, mid-tempo, a slow build into a wide chorus")
    expect(chip("Genre", "rock")).to_have_attribute("aria-pressed", "true")
    expect(chip("Tempo", "mid-tempo")).to_have_attribute("aria-pressed", "true")
    expect(chip("Mood", "warm")).to_have_attribute("aria-pressed", "false")
    chip("Tempo", "upbeat").click()
    expect(description).to_have_value("Rock, upbeat, a slow build into a wide chorus")

    chip("Genre", "pop").focus()
    page.keyboard.press("ArrowRight")
    expect(chip("Genre", "rock")).to_be_focused()
    page.keyboard.press("End")
    expect(chip("Genre", "cinematic")).to_be_focused()
    page.keyboard.press("ArrowRight")
    expect(chip("Genre", "pop")).to_be_focused()
    page.keyboard.press("Space")
    expect(description).to_have_value("pop, upbeat, a slow build into a wide chorus")


def test_seed_lock_keeps_the_seed_for_the_next_takes(page: Page, server):
    unlocked(page, server)
    fill_take(page)
    lock, seed = page.get_by_label("Lock seed"), page.locator("#seed")
    expect(lock).not_to_be_checked()
    lock.check()
    start_take(page)
    expect(page.locator(".take")).to_have_count(1)
    expect(seed).to_have_value(re.compile(r"^\d+$"))
    picked = seed.input_value()
    start_take(page)
    expect(page.locator(".take")).to_have_count(2)
    expect(page.locator(".take .seed")).to_have_text([f"seed {picked}"] * 2)

    lock.uncheck()
    expect(seed).to_have_value("")
    start_take(page)
    expect(page.locator(".take")).to_have_count(3)
    expect(page.locator(".take").first.locator(".seed")).not_to_have_text(f"seed {picked}")
    seed.fill("77")
    expect(lock).to_be_checked()


def test_mobile_both_tabs_have_no_horizontal_overflow(page: Page, server):
    page.set_viewport_size({"width": 390, "height": 844})
    long_take = server.submit(description="y" * 400, lyrics="[Verse]\n" + "z" * 300 + "\n[Chorus]\nla")
    other = server.submit()
    unlocked(page, server)
    expect(page.locator(".take.succeeded")).to_have_count(2, timeout=10_000)
    page.locator(".take").nth(1).get_by_label("Title").fill("A very long title that keeps going " * 4)
    page.locator(".take").first.get_by_role("button", name="Play").click()
    expect(page.locator("#now-title")).to_have_text("Take 2")
    assert horizontal_overflow(page) <= 0

    page.locator(".take").nth(1).get_by_role("button", name="Edit a copy").click()
    expect(page.locator(".section")).to_have_count(2)
    page.locator("#ab-a").select_option(long_take["id"])
    page.locator("#ab-b").select_option(other["id"])
    assert horizontal_overflow(page) <= 0
    add = page.get_by_role("toolbar", name="Add section")
    for control in (page.get_by_role("button", name="Start take"), page.get_by_role("button", name="Switch to B"),
                    add.get_by_role("button").last, page.get_by_role("toolbar", name="Instruments").get_by_role("button").last):
        control.scroll_into_view_if_needed()
        box = control.bounding_box()
        assert 0 <= box["x"] and box["x"] + box["width"] <= 390, control


def test_mobile_focus_scrolls_controls_above_the_dock_and_the_data_cap_warning_shows(page: Page, server):
    page.set_viewport_size({"width": 390, "height": 844})
    unlocked(page, server)
    open_tab(page, "Lab")
    expect(page.locator(".tally .more")).to_be_visible()
    expect(page.locator(".tally .more")).to_contain_text("monthly 1 GB")
    assert horizontal_overflow(page) <= 0
    dock = page.locator(".dock")
    page.locator("#description").focus()
    for target in (page.locator("#duration"), page.locator("#seed"), page.locator("#seedlock"), page.get_by_role("button", name="Start take")):
        page.keyboard.press("Tab")
        expect(target).to_be_focused()
        page.wait_for_timeout(100)
        box, dock_box = target.bounding_box(), dock.bounding_box()
        assert box["y"] + box["height"] <= dock_box["y"], (target, box, dock_box)
