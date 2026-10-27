// Tunecast page: one shared state behind two tabs. The Player lists and plays takes, the Lab writes them.
// The only <audio> sits in the dock outside both panels, and hiding a panel keeps its DOM, so switching
// tabs never interrupts playback or drops a draft.

const KEY = "tunecast_key";
const $ = (id) => document.getElementById(id);
const TAG_LINE = /^\[([^[\]]+)\]$/;
const DIFF_KEYS = ["lyrics", "description", "seed", "duration_s"];
const STYLE_GROUPS = [
  { name: "Genre", one: true, options: ["pop", "rock", "folk", "hip-hop", "electronic", "jazz", "R&B", "cinematic"] },
  { name: "Mood", one: true, options: ["warm", "uplifting", "melancholic", "dreamy", "dark"] },
  { name: "Tempo", one: true, options: ["slow tempo", "mid-tempo", "upbeat"] },
  { name: "Vocals", one: true, options: ["female vocals", "male vocals", "duet", "choir"] },
  { name: "Instruments", one: false, options: ["acoustic guitar", "piano", "synths", "strings", "drums", "bass"] },
];

// ---- pure helpers, exported for the browser tests ------------------------------------------------

export const fmt = (s) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;

/** Lyrics text -> [{tag, lines, gap}]. A tag is a whole line such as [Chorus]; text before the first tag is an
 *  untagged section. Blank lines that end a section are its `gap`, kept out of the editor but written back,
 *  so composeLyrics(parseLyrics(text)) === text for any text. */
export function parseLyrics(text) {
  const sections = [];
  if (!text) return sections;
  for (const line of text.split("\n")) {
    const tag = TAG_LINE.exec(line);
    if (tag || !sections.length) sections.push({ tag: tag ? tag[1] : "", lines: [], gap: 0 });
    if (!tag) sections.at(-1).lines.push(line);
  }
  for (const s of sections) while (s.lines.length && s.lines.at(-1) === "") { s.lines.pop(); s.gap++; }
  return sections;
}

export const composeLyrics = (sections) =>
  sections.flatMap((s) => [...(s.tag ? [`[${s.tag}]`] : []), ...s.lines, ...Array(s.gap).fill("")]).join("\n");

/** Adds or removes one preset phrase in comma-separated style text. A phrase from a single-choice group
 *  replaces its rival in place. The rest of the text stays; only the spacing around commas is normalised. */
export function toggleStyle(text, phrase, rivals = []) {
  const parts = text.split(",").map((s) => s.trim()).filter(Boolean);
  const same = (a, b) => a.toLowerCase() === b.toLowerCase();
  const at = parts.findIndex((s) => same(s, phrase));
  if (at >= 0) parts.splice(at, 1);
  else {
    const rival = parts.findIndex((s) => rivals.some((r) => same(r, s)));
    if (rival >= 0) parts[rival] = phrase;
    else parts.push(phrase);
  }
  return parts.join(", ");
}

export const styleWords = (text) => new Set(text.split(",").map((s) => s.trim().toLowerCase()).filter(Boolean));

/** Every whitespace-separated term must appear in the title, style, lyrics, seed or status. */
export function matches(job, query) {
  const hay = [job.title, job.params.description, job.params.lyrics, job.params.seed, job.status].join("\n").toLowerCase();
  return query.toLowerCase().split(/\s+/).filter(Boolean).every((term) => hay.includes(term));
}

export const changedParams = (a, b) => DIFF_KEYS.filter((k) => a[k] !== b[k]).map((k) => k.replace("_s", ""));

// ---- shared state ---------------------------------------------------------------------------------

const rows = new Map();      // job id -> reel row
const byId = new Map();      // job id -> job from the last poll
const numbers = new Map();   // job id -> take number shown in the reel
const loads = new Map();     // job id -> promise of an object URL holding its WAV
const ab = { a: null, b: null };
let jobs = [];               // the last poll, newest first
let generation = 0;          // bumped by a label edit, so a poll that started before it cannot undo it
let current = null;          // job id loaded in the dock
let cueing = null;           // job id whose audio is on its way to the dock; the latest request wins
let sections = null;         // the Sections view's model while it is shown; #lyrics always holds the text
let parentId = null;         // set by Edit a copy: the next take is recorded as a child of this one
let pending = null;          // {id, text} of a submission whose outcome is not known yet; a retry reuses its ID
let pollTimer = null;
const favChain = new Map();  // job id -> tail of its favorite PATCHes, so fast clicks reach the server in click order

const nameOf = (id) => byId.get(id)?.title || `Take ${numbers.get(id) ?? ""}`.trim();
const plural = (n) => `${n} take${n === 1 ? "" : "s"}`;
const secs = (s) => (s < 60 ? s.toFixed(1) + "s" : fmt(s));
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const newRequestId = () => Array.from(crypto.getRandomValues(new Uint8Array(16)), (b) => b.toString(16).padStart(2, "0")).join("");

// ---- key, connection and requests -----------------------------------------------------------------

const keybox = $("keybox"), main = $("main"), signout = $("signout");
const CFG = { pollMs: 3000, capMs: 30000, offlineAfter: 3, requestMs: 10000, ...window.__tunecast };     // the override is for the browser tests
let unlocked = false;        // a key was accepted and the takes are being polled
let ticking = false;

// A remembered key lives in localStorage, a session-only one in sessionStorage; either storage may throw.
const STORES = ["localStorage", "sessionStorage"];
function key() { for (const s of STORES) try { const v = window[s].getItem(KEY); if (v) return v; } catch {} return ""; }
function setKey(v, remember) { forgetKey(); try { window[remember ? "localStorage" : "sessionStorage"].setItem(KEY, v); } catch {} }
function forgetKey() { for (const s of STORES) try { window[s].removeItem(KEY); } catch {} }

async function api(path, opts = {}) {
  const headers = Object.assign({
    "Authorization": "Bearer " + key(),
    "ngrok-skip-browser-warning": "1",
  }, opts.headers || {});
  const r = await fetch(path, Object.assign({}, opts, { headers }));
  if (r.status === 401) { lock("That key was refused. Enter the API key set on the pod."); throw new Error("unauthorized"); }
  return r;
}

function showKeybox(msg) {
  if (msg) $("keymsg").textContent = msg;
  unlocked = false;
  keybox.hidden = false; main.hidden = true; signout.hidden = true;
  keybox.querySelector("input").focus();
}

// Forgets the key and everything loaded with it. The draft in the Lab stays: it is the user's text, not server data.
function lock(msg) {
  forgetKey();
  link.okAt = 0; generation++; pending = null; parentId = null; cueing = null;
  for (const id of [...loads.keys()]) forget(id);
  ab.a = ab.b = null;
  setJobs([]);
  $("gpus").textContent = ""; reelnote.textContent = "";
  showKeybox(msg);
}

async function unlock() {
  const r = await api("/info", { signal: AbortSignal.timeout(CFG.requestMs) });
  if (!r.ok) throw new Error("info " + r.status);
  renderInfo(await r.json());
  unlocked = true;
  keybox.hidden = true; main.hidden = false; signout.hidden = false;
  await poll();
}

$("keyform").addEventListener("submit", async (e) => {
  e.preventDefault();
  const data = new FormData(e.target);
  setKey(data.get("key").trim(), data.has("remember"));
  e.target.reset();
  try { await unlock(); }
  catch (err) { if (err.message !== "unauthorized") $("keymsg").textContent = "Could not reach the server. Retrying…"; }
});
signout.addEventListener("click", () => lock("Signed out. Enter the API key to continue."));

function renderInfo(info) {
  $("gpus").textContent = (info.gpus || []).map((g) =>
    `GPU${g.index} ${(g.used_mb / 1024).toFixed(1)}/${(g.total_mb / 1024).toFixed(0)} GB`).join("  ")
    || (info.stub ? "stub mode" : "");
}

// online -> reconnecting (a poll or readiness check failed) -> offline (CFG.offlineAfter failures in a row) -> online on success.
// Failures also back the polling off, 3 s -> 6 -> 12 -> 24 s, then capped at 30 s, and mark the takes list stale.
const link = { state: "online", fails: 0, okAt: 0 };
const linkEl = $("link-state"), retry = $("retry");

function note(ok) {
  link.fails = ok ? 0 : link.fails + 1;
  link.state = !link.fails ? "online" : link.fails < CFG.offlineAfter ? "reconnecting" : "offline";
  if (!ok) setLamps();
  if (linkEl.textContent !== link.state) linkEl.textContent = link.state;     // it sits in an aria-live region: do not re-announce every poll
  linkEl.classList.toggle("on", link.state === "online");
  linkEl.classList.toggle("warn", link.state === "reconnecting");
  retry.hidden = link.state === "online";
  const stale = unlocked && link.state !== "online";
  $("reel").classList.toggle("stale", stale);
  $("stale").hidden = !stale;
  renderAge();
}

function renderAge() {
  const s = Math.max(0, Math.round((Date.now() - link.okAt) / 1000));
  $("link-age").textContent = unlocked && link.okAt ? `updated ${s < 60 ? s + "s" : Math.floor(s / 60) + "m"} ago` : "";
}

async function tick() {
  if (ticking) return;
  ticking = true;
  try {
    if (unlocked) await poll();
    else if (key()) await unlock();
    else note(await ready());
  } catch (err) { if (err.message !== "unauthorized") note(false); }
  finally {
    ticking = false;
    pollTimer = setTimeout(tick, Math.min(CFG.pollMs * 2 ** link.fails, CFG.capMs));
  }
}

retry.addEventListener("click", () => { clearTimeout(pollTimer); tick(); });

function setLamps(model, tunnel) {
  for (const [id, on] of [["lamp-model", model], ["lamp-tunnel", tunnel]]) {
    const lamp = $(id);
    lamp.classList.toggle("on", on === true);
    lamp.classList.toggle("unknown", on === undefined);
    lamp.title = on === undefined ? "unknown: server unreachable" : on ? "ready" : "not ready";
  }
}

// /ready answers 503 with a JSON body while something is not up yet; that is a reachable server, not a failure.
async function ready() {
  try {
    const r = await fetch("/ready", { headers: { "ngrok-skip-browser-warning": "1" }, signal: AbortSignal.timeout(CFG.requestMs) });
    if (r.status !== 200 && r.status !== 503) return false;
    const j = await r.json();
    setLamps(!!j.model, !!j.tunnel);
    return true;
  } catch { return false; }
}

async function poll() {
  const gen = generation;
  let list;
  try {
    const r = await api("/jobs?limit=50", { signal: AbortSignal.timeout(CFG.requestMs) });
    if (!r.ok) return note(false);
    list = await r.json();
  } catch (err) { if (err.message !== "unauthorized") note(false); return; }
  if (gen === generation) { setJobs(list); link.okAt = Date.now(); }
  note(await ready());
}

function setJobs(list) {
  const left = current !== null && !audio.ended && !list.some((j) => j.id === current) ? byId.get(current) : undefined;
  const leftNo = numbers.get(current);
  jobs = list;
  byId.clear(); numbers.clear();
  list.forEach((job, i) => { byId.set(job.id, job); numbers.set(job.id, list.length - i); });
  const reel = $("reel");
  list.forEach((job, i) => {
    let row = rows.get(job.id);
    if (!row) { row = makeRow(job.id); rows.set(job.id, row); }
    updateRow(row, job);
    if (reel.children[i] !== row) reel.insertBefore(row, reel.children[i] || null);
  });
  for (const [id, row] of rows) if (!byId.has(id)) { row.remove(); rows.delete(id); }
  if (left) { byId.set(left.id, left); numbers.set(left.id, leftNo); }     // the dock keeps naming a loaded take that left the list
  sweep();
  filter(); fillCompare(); renderTransport(); renderLineage();
}

// ---- tabs -----------------------------------------------------------------------------------------

const tablist = document.querySelector("[role=tablist]");
const labTab = $("tab-lab");

function selectTab(tab) {
  for (const t of tablist.querySelectorAll("[role=tab]")) {
    const on = t === tab;
    t.setAttribute("aria-selected", on);
    t.tabIndex = on ? 0 : -1;
    $(t.getAttribute("aria-controls")).hidden = !on;
  }
}

// Arrow keys, Home and End move focus through a tablist or toolbar; only one of its buttons is a tab stop.
function rove(group, onMove) {
  const items = () => [...group.querySelectorAll(":scope > button")];
  items().forEach((el, i) => { el.tabIndex = i ? -1 : 0; });
  group.addEventListener("focusin", (e) => {
    if (items().includes(e.target)) for (const el of items()) el.tabIndex = el === e.target ? 0 : -1;
  });
  group.addEventListener("keydown", (e) => {
    const list = items(), i = list.indexOf(e.target);
    const to = { ArrowRight: i + 1, ArrowLeft: i - 1, Home: 0, End: list.length - 1 }[e.key];
    if (i < 0 || to === undefined) return;
    e.preventDefault();
    const next = list[(to + list.length) % list.length];
    next.focus();
    onMove?.(next);
  });
}

rove(tablist, selectTab);
tablist.addEventListener("click", (e) => { const tab = e.target.closest("[role=tab]"); if (tab) selectTab(tab); });
$("to-lab").addEventListener("click", () => { selectTab(labTab); labTab.focus(); });

// ---- Player: the reel -----------------------------------------------------------------------------

const search = $("search"), favonly = $("favonly"), reelnote = $("reelnote");

function makeRow(id) {
  const li = $("take-row").content.firstElementChild.cloneNode(true);
  const q = (sel) => li.querySelector(sel);
  q(".play").addEventListener("click", () => toggle(id));
  q(".save").addEventListener("click", (e) => save(id, e));
  q(".copy").addEventListener("click", () => editCopy(id));
  q(".exp").addEventListener("click", (e) => { e.preventDefault(); exportExperiment(byId.get(id).experiment_id); });
  q(".del").addEventListener("click", (e) => del(id, e.currentTarget));
  q(".fav").addEventListener("click", () => setFavorite(id));
  const title = q(".title");
  title.addEventListener("keydown", (e) => {
    if (e.key === "Enter") title.blur();
    if (e.key === "Escape") { title.value = byId.get(id)?.title || ""; title.blur(); }
  });
  title.addEventListener("change", () => setTitle(id, title));
  return li;
}

function updateRow(row, job) {
  const q = (sel) => row.querySelector(sel);
  const p = job.params, pr = job.progress, t = job.timings || {}, n = numbers.get(job.id);
  row.className = "take " + job.status;
  row.classList.toggle("current", job.id === current);
  q(".num b").textContent = n;
  const title = q(".title");
  title.placeholder = "Take " + n;
  if (document.activeElement !== title) title.value = job.title || "";
  if (!favChain.has(job.id)) q(".fav").setAttribute("aria-pressed", job.favorite);     // clicks still in flight own the star

  let state = job.status;
  if (job.status === "queued") state = job.queue_position > 0 ? `queued, ${job.queue_position} ahead` : "queued, next up";
  q(".state").textContent = state;
  q(".len").textContent = job.audio_duration_s != null ? fmt(Math.round(job.audio_duration_s)) : "up to " + fmt(p.duration_s);
  q(".seed").textContent = "seed " + p.seed;
  q(".when").textContent = new Date(job.created_at).toLocaleTimeString();
  q(".desc").textContent = p.description;

  const lit = job.status === "succeeded" ? 30 : Math.round((pr.fraction || 0) * 30);
  q(".meter").style.setProperty("--lit", (lit / 30) * 100 + "%");
  q(".prog").textContent = job.status === "running"
    ? `${fmt(pr.elapsed_s)} of about ${fmt(pr.estimated_total_s)} (estimate, ${job.estimate.confidence} confidence)` : "";
  q(".stats").textContent = [
    t.queue_wait_s != null && "queued " + secs(t.queue_wait_s),
    t.inference_s != null && "inference " + secs(t.inference_s),
    job.real_time_factor != null && "RTF " + job.real_time_factor.toFixed(2) + "x",
  ].filter(Boolean).join(" · ");
  q(".diff").textContent = parentDiff(job);
  q(".exp").hidden = !job.experiment_id;

  const err = q(".err");
  err.hidden = !job.error; err.textContent = job.error || "";

  q(".play").hidden = q(".save").hidden = job.status !== "succeeded";
  const delBtn = q(".del");
  delBtn.dataset.verb = job.status === "queued" ? "Cancel" : job.status === "running" ? "Can't cancel" : "Delete";
  if (delBtn.dataset.armed !== "1") delBtn.textContent = delBtn.dataset.verb;
  delBtn.disabled = job.status === "running";
  delBtn.title = job.status === "running" ? "A running take cannot be cancelled: the model server has no cancel. Delete it once it finishes." : "";
}

function parentDiff(job) {
  if (!job.parent_job_id) return "";
  const parent = byId.get(job.parent_job_id);
  if (!parent) return "copied from a take that is no longer listed";
  const changed = changedParams(job.params, parent.params);
  return "vs parent: " + (changed.length ? changed.join(", ") + " changed" : "nothing changed");
}

function filter() {
  let shown = 0;
  for (const job of jobs) {
    const ok = (!favonly.checked || job.favorite) && matches(job, search.value);
    rows.get(job.id).hidden = !ok;
    shown += ok;
  }
  $("empty").hidden = jobs.length > 0;
  $("nomatch").hidden = !jobs.length || shown > 0;
  $("count").textContent = !jobs.length ? "" : shown === jobs.length ? plural(shown) : `${shown} of ${plural(jobs.length)}`;
}
search.addEventListener("input", filter);
favonly.addEventListener("change", filter);

function setFavorite(id) {
  const btn = rows.get(id).querySelector(".fav");
  const favorite = btn.getAttribute("aria-pressed") !== "true";
  btn.setAttribute("aria-pressed", favorite);
  generation++;      // a poll already in flight carries the old value
  const run = (favChain.get(id) ?? Promise.resolve()).then(() => relabel(id, { favorite })).then(() => {
    if (favChain.get(id) !== run) return;
    favChain.delete(id);
    const row = rows.get(id), job = byId.get(id);
    if (row && job) updateRow(row, job);     // settle on what the server has
  });
  favChain.set(id, run);
}

function setTitle(id, input) {
  const title = input.value.trim(), job = byId.get(id);
  if (!job || title === (job.title || "")) { input.value = job?.title || ""; return; }
  return relabel(id, { title });
}

async function relabel(id, fields) {
  reelnote.textContent = "";
  try {
    const r = await api(`/jobs/${id}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(fields) });
    if (r.ok) {
      const job = await r.json();
      generation++;
      setJobs(jobs.map((j) => (j.id === id ? job : j)));
      return;
    }
    reelnote.textContent = r.status === 404 ? "That take is gone." : `Not saved (HTTP ${r.status}).`;
  } catch (err) {
    if (err.message !== "unauthorized") reelnote.textContent = "Not saved: the server did not answer.";
  }
  const row = rows.get(id), job = byId.get(id);
  if (row && job) updateRow(row, job);     // back to what the server has
}

async function del(id, btn) {
  if (btn.dataset.armed !== "1") {
    btn.dataset.armed = "1"; btn.textContent = "Really " + btn.dataset.verb.toLowerCase();
    setTimeout(() => { btn.dataset.armed = ""; btn.textContent = btn.dataset.verb; }, 4000);
    return;
  }
  try {
    const r = await api(`/jobs/${id}`, { method: "DELETE" });
    if (r.status === 204) { forget(id); await poll(); }
    else btn.textContent = r.status === 409 ? "Now running, can't cancel" : "Delete failed";
  } catch {}
}

function download(url, name) {
  const a = Object.assign(document.createElement("a"), { href: url, download: name });
  document.body.append(a); a.click(); a.remove();
}

async function exportExperiment(experimentId) {
  try {
    const r = await api(`/experiments/${encodeURIComponent(experimentId)}/export?format=json`);
    if (!r.ok) throw new Error("export " + r.status);
    const url = URL.createObjectURL(await r.blob());
    download(url, `experiment-${experimentId}.json`);
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  } catch (err) { if (err.message !== "unauthorized") reelnote.textContent = "Export failed."; }
}

// ---- dock: the one audio element ------------------------------------------------------------------

const audio = $("audio"), pp = $("playpause"), scrub = $("scrub");

const dock = document.querySelector(".dock");     // its height feeds scroll-padding-bottom, so focus scrolls above it
new ResizeObserver(() => document.documentElement.style.setProperty("--dock-h", dock.offsetHeight + "px")).observe(dock);

function audioUrl(id) {
  if (!loads.has(id)) {
    const load = api(`/jobs/${id}/audio`).then(async (r) => {
      if (!r.ok) throw new Error("audio " + r.status);
      const url = URL.createObjectURL(await r.blob());
      if (loads.get(id) !== load) { URL.revokeObjectURL(url); throw new Error("gone"); }   // deleted while loading
      return url;
    });
    load.catch(() => { if (loads.get(id) === load) loads.delete(id); });
    loads.set(id, load);
  }
  return loads.get(id);
}

function forget(id) {
  loads.get(id)?.then((url) => URL.revokeObjectURL(url), () => {});
  loads.delete(id);
  if (id === current) { current = null; audio.removeAttribute("src"); audio.load(); }
}

// A take that left the list (pruned, or pushed past the 50 polled) keeps its audio until it is no longer the one loaded.
function sweep() {
  for (const id of loads.keys()) if (!jobs.some((j) => j.id === id) && (id !== current || audio.ended)) forget(id);
}

function setSave(id, url) {
  const link = rows.get(id)?.querySelector(".save");
  if (link) { link.href = url; link.download = `${id}.wav`; }
}

async function save(id, e) {
  const link = e.currentTarget;
  if (link.download) return;      // already points at the loaded WAV
  e.preventDefault();
  try { setSave(id, await audioUrl(id)); link.click(); }
  catch (err) { if (err.message !== "unauthorized") reelnote.textContent = "Could not fetch that take's WAV. Try again."; }
}

// Loads a take into the dock. keep: carry the position and the play/pause state over, as A/B needs.
async function cue(id, keep = false) {
  cueing = id;
  renderTransport();
  let url;
  try { url = await audioUrl(id); }
  catch (err) {
    if (cueing === id) { cueing = null; renderTransport(); }
    if (err.message !== "unauthorized" && err.message !== "gone") reelnote.textContent = "Could not load that take's audio. Try again.";
    return;
  }
  if (cueing !== id) return;
  cueing = null;
  const carry = keep && current !== null;
  const at = carry ? audio.currentTime : 0, play = !carry || !audio.paused;
  current = id;
  audio.src = url;
  audio.dataset.take = id;
  audio.currentTime = at;     // before metadata this is the start position the element seeks to
  if (play) audio.play().catch(() => {});
  setSave(id, url);
  sweep();
  renderTransport();
}

function toggle(id) {
  if (id !== current) return cue(id);
  if (audio.paused) audio.play().catch(() => {}); else audio.pause();
}

function flip(side) {
  const id = ab[side];
  if (id && id !== current) cue(id, current === ab.a || current === ab.b);
}

function renderTransport() {
  const job = byId.get(current), playing = current !== null && !audio.paused;
  pp.disabled = current === null;
  pp.classList.toggle("playing", playing);
  pp.setAttribute("aria-label", playing ? "Pause" : "Play");
  $("now-title").textContent = job ? nameOf(current) : "Nothing playing";
  $("now-meta").textContent = cueing && byId.has(cueing) ? `Loading ${nameOf(cueing)}…`
    : job ? `seed ${job.params.seed} · ${job.params.description}` : "Pick a take in the Player.";
  const d = Number.isFinite(audio.duration) ? audio.duration : 0, t = current === null ? 0 : audio.currentTime;
  scrub.disabled = current === null;
  scrub.max = d;
  scrub.value = t;
  scrub.style.setProperty("--pos", d ? (t / d) * 100 + "%" : "0%");
  scrub.setAttribute("aria-valuetext", `${fmt(t)} of ${fmt(d)}`);
  $("clock").textContent = `${fmt(t)} / ${fmt(d)}`;
  for (const [id, row] of rows) {
    row.classList.toggle("current", id === current);
    row.querySelector(".play").textContent = id === cueing ? "Loading…" : id === current && playing ? "Pause" : "Play";
  }
  $("flip-a").setAttribute("aria-pressed", current !== null && current === ab.a);
  $("flip-b").setAttribute("aria-pressed", current !== null && current === ab.b);
}

for (const ev of ["play", "pause", "ended", "timeupdate", "durationchange", "emptied"]) audio.addEventListener(ev, renderTransport);
audio.addEventListener("ended", sweep);
pp.addEventListener("click", () => { if (current !== null) toggle(current); });
scrub.addEventListener("input", () => { audio.currentTime = +scrub.value; });
$("flip-a").addEventListener("click", () => flip("a"));
$("flip-b").addEventListener("click", () => flip("b"));

function fillCompare() {
  const done = jobs.filter((j) => j.status === "succeeded");
  const options = done.map((j) => [j.id, [nameOf(j.id), j.audio_duration_s != null && fmt(Math.round(j.audio_duration_s)), "seed " + j.params.seed].filter(Boolean).join(", ")]);
  const signature = JSON.stringify(options);
  for (const side of ["a", "b"]) {
    const select = $("ab-" + side);
    if (!done.some((j) => j.id === ab[side])) ab[side] = null;
    if (select.dataset.signature !== signature) {     // rebuilt only on change, so an open dropdown is left alone
      select.replaceChildren(new Option("Choose a take", ""), ...options.map(([id, text]) => new Option(text, id)));
      select.dataset.signature = signature;
    }
    select.value = ab[side] || "";
  }
  $("ab").hidden = !(ab.a && ab.b);
}

for (const side of ["a", "b"]) {
  $("ab-" + side).addEventListener("change", (e) => {
    ab[side] = e.target.value || null;
    $("ab").hidden = !(ab.a && ab.b);
    renderTransport();
  });
}

// ---- Lab: lyrics sections, style presets, seed lock, lineage, submit ------------------------------

const form = $("takeform"), lyrics = $("lyrics"), description = $("description");
const duration = $("duration"), seed = $("seed"), seedlock = $("seedlock"), formnote = $("formnote");
const sheetList = $("sections"), adder = document.querySelector(".adder"), presets = $("presets");

function setNote(text, error = false) {
  formnote.textContent = text;
  formnote.classList.toggle("error", error);
}

function showLyrics() {
  const asText = form.elements.lyricsview.value === "text";
  lyrics.hidden = !asText;
  $("sheet").hidden = asText;
  sections = asText ? null : parseLyrics(lyrics.value);
  if (sections) renderSections();
}
for (const radio of form.elements.lyricsview) radio.addEventListener("change", showLyrics);

const kindOf = (tag) => (/pre/i.test(tag) ? "" : /chorus|hook/i.test(tag) ? "chorus" : /verse/i.test(tag) ? "verse" : /bridge/i.test(tag) ? "bridge" : "");

function labelCard(li, i) {
  const s = sections[i], name = `section ${i + 1}${s.tag ? ", " + s.tag : ""}`;
  const q = (sel) => li.querySelector(sel);
  li.dataset.kind = kindOf(s.tag);
  q(".tag").setAttribute("aria-label", `Tag of ${name}`);
  q(".lines").setAttribute("aria-label", `Lines of ${name}`);
  q(".up").setAttribute("aria-label", `Move ${name} up`);
  q(".down").setAttribute("aria-label", `Move ${name} down`);
  q(".remove").setAttribute("aria-label", `Remove ${name}`);
  q(".up").disabled = i === 0;
  q(".down").disabled = i === sections.length - 1;
}

function renderSections() {
  sheetList.replaceChildren(...sections.map((s) => {
    const li = $("section-card").content.firstElementChild.cloneNode(true);
    li.querySelector(".tag").value = s.tag;
    li.querySelector(".lines").value = s.lines.join("\n");
    return li;
  }));
  [...sheetList.children].forEach(labelCard);
  $("sheet-empty").hidden = sections.length > 0;
}

function writeLyrics() { lyrics.value = composeLyrics(sections); }

const cardIndex = (el) => [...sheetList.children].indexOf(el.closest(".section"));

sheetList.addEventListener("input", (e) => {
  const i = cardIndex(e.target), s = sections[i];
  if (e.target.classList.contains("tag")) {
    const clean = e.target.value.replace(/[[\]]/g, "");     // a bracket would break the tag line
    if (clean !== e.target.value) e.target.value = clean;
    s.tag = clean;
    labelCard(sheetList.children[i], i);
  } else s.lines = e.target.value === "" ? [] : e.target.value.split("\n");
  writeLyrics();
});

sheetList.addEventListener("keydown", (e) => {     // Enter in a tag moves on instead of submitting the take
  if (e.key === "Enter" && e.target.classList.contains("tag")) { e.preventDefault(); e.target.closest(".section").querySelector(".lines").focus(); }
});

sheetList.addEventListener("click", (e) => {
  const btn = e.target.closest("button");
  if (!btn) return;
  const i = cardIndex(btn);
  let at, focus;
  if (btn.classList.contains("remove")) {
    sections.splice(i, 1);
    at = Math.min(i, sections.length - 1); focus = ".lines";
  } else {
    at = btn.classList.contains("up") ? i - 1 : i + 1;
    [sections[i], sections[at]] = [sections[at], sections[i]];
    focus = btn.classList.contains("up") ? ".up" : ".down";
  }
  renderSections(); writeLyrics(); renderLineage();
  const card = sheetList.children[at], target = card?.querySelector(focus);
  const fallback = { ".up": ".down", ".down": ".up" }[focus] || ".lines";     // a move button turns disabled at either end
  (target && !target.disabled ? target : card?.querySelector(fallback) ?? adderStop()).focus();
});

const adderStop = () => adder.querySelector("button[tabindex='0']") || adder.querySelector("button");

adder.addEventListener("click", (e) => {
  const tag = e.target.closest("button")?.dataset.tag;
  if (!tag) return;
  const last = sections.at(-1);
  if (last && sections.some((s) => s.gap)) last.gap ||= 1;    // the sheet separates sections with blank lines
  sections.push({ tag, lines: [], gap: 0 });
  renderSections(); writeLyrics(); renderLineage();
  sheetList.lastElementChild.querySelector(".lines").focus();
});
rove(adder);

for (const group of STYLE_GROUPS) {
  const bar = Object.assign(document.createElement("div"), { className: "chips" });
  bar.setAttribute("role", "toolbar");
  bar.setAttribute("aria-label", group.name);
  const label = Object.assign(document.createElement("span"), { className: "chips-label", textContent: group.name });
  label.setAttribute("aria-hidden", "true");
  bar.append(label, ...group.options.map((phrase) => {
    const chip = Object.assign(document.createElement("button"), { type: "button", className: "chip", textContent: phrase });
    chip.addEventListener("click", () => {
      description.value = toggleStyle(description.value, phrase, group.one ? group.options : []);
      syncChips(); renderLineage();
    });
    return chip;
  }));
  presets.append(bar);
  rove(bar);
}

function syncChips() {
  const words = styleWords(description.value);
  for (const chip of presets.querySelectorAll(".chip")) chip.setAttribute("aria-pressed", words.has(chip.textContent.toLowerCase()));
}
description.addEventListener("input", syncChips);

duration.addEventListener("input", () => { $("durlabel").textContent = fmt(+duration.value); });
seed.addEventListener("input", () => { seedlock.checked = seed.value !== ""; });
seedlock.addEventListener("change", () => { if (!seedlock.checked) seed.value = ""; renderLineage(); });

function draft() {
  const body = { lyrics: lyrics.value, description: description.value, duration_s: +duration.value };
  if (seed.value !== "") body.seed = +seed.value;
  return body;
}

function renderLineage() {
  $("lineage").hidden = !parentId;
  if (!parentId) return;
  const parent = byId.get(parentId);
  const changed = parent ? changedParams(draft(), parent.params) : [];
  $("lineage-text").textContent = !parent ? "Copy of a take that is no longer listed. Detach to start a new experiment."
    : `Copy of ${nameOf(parentId)}: the next take is recorded as its child. ${changed.length ? "Changed: " + changed.join(", ") + "." : "Nothing changed yet."}`;
}
form.addEventListener("input", renderLineage);
$("detach").addEventListener("click", () => { parentId = null; renderLineage(); });

function editCopy(id) {
  const job = byId.get(id);
  if (!job) return;
  const p = job.params;
  if ((lyrics.value || description.value) && (lyrics.value !== p.lyrics || description.value !== p.description)
    && !confirm(`Replace the lyrics and style in the Lab with a copy of ${nameOf(id)}?`)) return;
  lyrics.value = p.lyrics; description.value = p.description;
  duration.value = p.duration_s; $("durlabel").textContent = fmt(p.duration_s);
  seed.value = p.seed; seedlock.checked = true;
  parentId = id;
  if (sections) { sections = parseLyrics(lyrics.value); renderSections(); }
  syncChips(); renderLineage(); setNote("");
  selectTab(labTab);
  (sections ? sheetList.querySelector(".lines") ?? adderStop() : lyrics).focus();
}

function accepted(job) {
  parentId = null; renderLineage();
  if (seedlock.checked) seed.value = job.params.seed;
}

// The job for this request ID, null when the server confirms there is none, undefined when it could not be asked.
async function findByRequestId(id) {
  for (let i = 0; i < 5; i++) {
    if (i) await sleep(500 * 2 ** (i - 1));
    try {
      const r = await api("/jobs?request_id=" + id);
      if (r.ok) return (await r.json())[0] || null;
    } catch (err) { if (err.message === "unauthorized") return; }
  }
}

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  const body = draft();
  if (!body.lyrics.trim()) {
    setNote("Not started: write some lyrics first.", true);
    (sections ? sheetList.querySelector(".lines") ?? adderStop() : lyrics).focus();
    return;
  }
  if (parentId) body.parent_job_id = parentId;
  const text = JSON.stringify(body);
  if (!pending || pending.text !== text) pending = { id: newRequestId(), text };
  const submit = $("submit");
  submit.disabled = true; setNote("");
  try {
    const r = await api("/jobs", { method: "POST", headers: { "Content-Type": "application/json", "Idempotency-Key": pending.id }, body: text });
    if (r.status >= 500 && !r.headers.get("Retry-After")) throw new Error("server error");   // Retry-After: the server refused admission, so nothing was created
    pending = null;
    if (r.ok) { accepted(await r.json()); setNote(r.status === 202 ? "Queued." : "Already submitted."); await poll(); }
    else {
      const d = await r.json().catch(() => ({})), wait = r.headers.get("Retry-After");
      if (typeof d.detail === "string" && !wait) { parentId = null; renderLineage(); }
      setNote("Not started: " + (typeof d.detail === "string" ? d.detail : d.detail ? JSON.stringify(d.detail) : r.status) + (wait ? `. Your draft is kept, try again in ${wait} s.` : ""), true);
    }
  } catch (err) {
    if (err.message === "unauthorized") { pending = null; setNote("Not started: unauthorized"); return; }
    setNote("Submission status unknown, checking…");
    const job = await findByRequestId(pending.id);
    if (job) { pending = null; accepted(job); setNote("Queued."); await poll(); }
    else {
      // pending stays: retrying the same take resends the same ID, so it cannot be queued twice
      setNote(job === null ? "Not submitted. Your draft is kept, try again." : "Submission status unknown. Check the Player, then try again; resending is safe.", true);
    }
  } finally { submit.disabled = false; }
});

// ---- start ----------------------------------------------------------------------------------------

showLyrics();
syncChips();
renderTransport();
setInterval(renderAge, 1000);
if (!key()) showKeybox();
tick();
