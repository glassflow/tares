/*
 * The Connections page's fixed list (TR-1394), shared by tests/sources.e2e.cjs and tests/sources.shots.cjs:
 * four connections, one per health state, two projects, and the routes that serve them.
 */
const json = (route, status, body) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });

const ago = (min) => new Date(Date.now() - min * 60_000).toISOString();
const health = (status, extra = {}) => ({ status, last_poll_at: null, last_ok_at: null, last_error: null, consecutive_errors: 0,
  polls: 0, events_since_start: 0, events_total: 0, last_ingest: null, ...extra });
const LIST = [
  { name: "gh_shop", type: "poll", connector: "github", poll: "60s", config: { repo: "acme/shop" }, paused: false,
    projects: ["p-shop"], health: health("ok", { events_total: 12840, last_ingest: ago(3) }) },
  { name: "claude_code", type: "push", connector: "claude_code", poll: "10s", config: { push: true }, paused: false,
    projects: [], health: health("push", { events_total: 77 }) },
  { name: "pg_orders", type: "poll", connector: "postgres", poll: "5m", config: { table: "orders" }, paused: false,
    projects: ["p-shop", "p-billing"], health: health("error", { last_error: "password authentication failed for user \"reader\"", events_total: 4, last_ingest: ago(600) }) },
  { name: "loki_api", type: "poll", connector: "loki", poll: "30s", config: {}, paused: true,
    projects: ["p-billing"], health: health("paused") },
];
const PROJECTS = [{ id: "p-shop", name: "Shop" }, { id: "p-billing", name: "Billing" }];

// list: the rows /api/sources answers, or a status code to fail with. docker: what capabilities say.
// A page closing mid-fetch rejects the two pass-through routes; the caller has moved on, so that is ignored.
async function mock(page, { list = LIST, docker = true } = {}) {
  await page.route("**/api/capabilities", async (route) => {
    try { const r = await route.fetch(); await json(route, 200, { ...(await r.json()), discover_docker: docker }); } catch { /* closed */ }
  });
  await page.route("**/api/sources", (route) => typeof list === "number"
    ? json(route, list, { detail: "the database is locked" }) : json(route, 200, list));
  await page.route("**/api/projects", async (route) => {
    try {
      const real = await (await route.fetch()).json();
      await json(route, 200, { ...real, projects: [...(real.projects ?? []), ...PROJECTS.map((p) => ({ ...(real.projects?.[0] ?? {}), ...p, hidden: false }))] });
    } catch { /* closed */ }
  });
}

module.exports = { LIST, PROJECTS, mock };
