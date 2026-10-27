// Offline demo: the real page, with its API answered from fixtures.json in this tab's memory.
// Loaded before app.js (see scripts/build_demo.py). Nothing here reaches a server; a reload restores the fixtures.
(() => {
  const here = document.currentScript.src;
  const realFetch = window.fetch.bind(window);
  const fixtures = realFetch(new URL("fixtures.json", here)).then((r) => r.json());
  try { sessionStorage.setItem("tunecast_key", "demo"); } catch {}     // pre-unlocked: the demo has no key

  const reply = (body, status = 200, headers = {}) =>
    new Response(body === null ? null : JSON.stringify(body), { status, headers: { "Content-Type": "application/json", ...headers } });

  let jobs = null;     // newest first, edited in place by label changes and deletes
  const find = (id) => jobs.find((j) => j.id === id);

  window.fetch = async (input, init = {}) => {
    const url = new URL(typeof input === "string" ? input : input.url, location.href);
    const method = (init.method || "GET").toUpperCase();
    const route = url.pathname.match(/^\/(ready|info|jobs)(?:\/([^/]+))?(\/audio)?$/);
    if (!route) return realFetch(input, init);
    const [, root, id, audio] = route;
    const data = await fixtures;
    jobs ??= structuredClone(data.jobs);

    if (root === "ready") return reply({ model: false, tunnel: false }, 503);     // no model is running, say so
    if (root === "info") return reply(data.info);
    if (!id) {
      if (method === "POST") return reply({ detail: "Offline demo: nothing is generated here." }, 503, { "Retry-After": "30" });
      return reply(url.searchParams.has("request_id") ? [] : jobs);
    }
    const job = find(id);
    if (!job) return reply({ detail: "job not found" }, 404);
    if (audio) return realFetch(new URL("../song-client.wav", here));     // the real pod output, one copy at the repo root
    if (method === "PATCH") {
      const fields = JSON.parse(init.body);
      if ("title" in fields) job.title = fields.title.trim() || null;
      if ("favorite" in fields) job.favorite = fields.favorite;
      return reply(job);
    }
    if (method === "DELETE") { jobs = jobs.filter((j) => j !== job); return reply(null, 204); }
    return reply(job);
  };

  document.addEventListener("DOMContentLoaded", () => {
    const start = document.getElementById("submit");
    start.disabled = true;
    start.setAttribute("aria-describedby", "demo-why");
    const why = Object.assign(document.createElement("span"), {
      id: "demo-why", className: "note",
      textContent: "Disabled in the offline demo: there is no model to run. You can still edit a take’s lyrics and style.",
    });
    start.after(why);
  });
})();
