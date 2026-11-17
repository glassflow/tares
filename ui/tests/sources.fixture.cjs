/*
 * The Connections page's fixed list (TR-1394), shared by tests/sources.e2e.cjs and tests/sources.shots.cjs:
 * four connections, one per health state, two projects, and the routes that serve them. A connection's page
 * (tests/source-detail.e2e.cjs, tests/source-detail.shots.cjs) adds its labels, sampled fields and recent events.
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

// A connection's page. ORDERS fails, carries three labels (one keyed, one a constant, one that rewrites its
// values and is missing from the sample) and fourteen other fields, more than the twelve shown at first.
const ORDERS = {
  ...LIST[2],
  config: {
    table: "orders",
    labels: [
      { name: "table", field: "table", primary: true },
      { name: "env", const: "prod" },
      { name: "status", field: "row.status", pattern: "^(\\w+)", map: { pending: "open" } },
    ],
  },
  health: { ...LIST[2].health, consecutive_errors: 3, polls: 120, events_since_start: 4 },
};
const value = (v, events) => ({ value: v, events });
const FIELDS = {
  sampled: 50,
  labels: [
    { name: "table", is_key: true, coverage: 50, distinct: 1, values: [value("orders", 50)] },
    { name: "env", is_key: false, coverage: 50, distinct: 1, values: [value("prod", 50)] },
    { name: "status", is_key: false, coverage: 0, distinct: 0, values: [] },
  ],
  fields: [
    { name: "table", help: "", primary_default: true, coverage: 50, distinct: 1, is_key: true, values: [value("orders", 50)] },
    { name: "row.id", help: "", primary_default: false, coverage: 50, distinct: 50, values: [value("1001", 1), value("1002", 1)] },
    { name: "row.amount", help: "", primary_default: false, coverage: 40, distinct: 31, values: [value("19.99", 6)] },
    { name: "row.note", help: "", primary_default: false, coverage: 0, distinct: 0, values: [] },
    ...Array.from({ length: 11 }, (_, i) => ({ name: `row.col_${String.fromCharCode(97 + i)}`, help: "", primary_default: false,
      coverage: 30 - i, distinct: 2, values: [value("yes", 20 - i), value("no", 10)] })),
  ],
};
const EVENTS = [
  { source: "pg_orders", key: "orders/1002", event_type: "row", text: "order 1002 paid, 19.99 EUR\ncustomer: c-77", event_time: ago(600), ingest_time: ago(600) },
  { source: "pg_orders", key: "orders/1001", event_type: "row", text: "order 1001 created", event_time: ago(601), ingest_time: ago(601) },
];
// A push connection: an endpoint to post to, no interval, nothing received yet.
const ALERTS = { name: "am_alerts", type: "push", connector: "alertmanager", poll: "10s", config: {}, paused: false,
  ingest_key: "k-am", projects: [], health: health("push") };

// The page of `source`. fields, events: what the daemon samples (a status code fails it). dependents: what a
// delete takes along. del: the status the delete answers. calls: every write the page makes, as "METHOD path?query".
async function mockDetail(page, { source = ORDERS, fields = FIELDS, events = EVENTS, dependents = [], del = 200, calls = [] } = {}) {
  await mock(page);
  const base = `/api/sources/${source.name}`;
  await page.route((u) => u.pathname === base || u.pathname.startsWith(base + "/"), (route) => {
    const req = route.request();
    const u = new URL(req.url());
    const rest = u.pathname.slice(base.length);
    if (req.method() !== "GET") calls.push(`${req.method()} ${rest || "/"}${u.search}`);
    if (rest === "/fields") return typeof fields === "number" ? json(route, fields, { detail: "sampling failed" }) : json(route, 200, fields);
    if (rest === "/events") return typeof events === "number" ? json(route, events, { detail: "read failed" }) : json(route, 200, events);
    if (rest === "/pause" || rest === "/resume") return json(route, 200, { ok: true });
    if (req.method() === "DELETE") return del === 200 ? json(route, 200, { ok: true, purged_events: 0, deleted: [source.name] })
      : json(route, del, { detail: "it is in use by a running agent" });
    if (req.method() === "PUT") return json(route, 200, { ok: true });
    return json(route, 200, source);
  });
  await page.route("**/api/catalog/dependents*", (route) => json(route, 200, { dependents }));
}

module.exports = { LIST, PROJECTS, mock, ORDERS, FIELDS, EVENTS, ALERTS, mockDetail };
