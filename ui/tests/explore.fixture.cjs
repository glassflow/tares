/*
 * What Explore (/explore, TR-1395) is shown, shared by explore.e2e.cjs and explore.shots.cjs.
 * The daemon is real; /api/entities and POST /read are mocked so the picker and the timeline hold
 * known values, and /api/projects gains a project "Shop" beside the daemon's own.
 */
const json = (route, status, body) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });

const ENTITIES = {
  labels: [
    { label: "key", primary: true, sources: ["gh-shop"], values: [
      { value: "acme/shop", events: 1234, last_ingest: null },
      { value: "acme/billing", events: 40, last_ingest: null },
    ] },
    { label: "service", primary: true, sources: ["pg-logs"], values: [
      { value: "checkout", events: 300, last_ingest: null },
      { value: "payments", events: 12, last_ingest: null },
    ] },
    { label: "user", primary: false, high_cardinality: true, sources: ["gh-shop"], values: [
      { value: "kim", events: 50, last_ingest: null },
    ] },
    { label: "region", primary: false, sources: ["pg-logs"], values: [] },
  ],
};

const ROWS = [
  { offset: "T-180s", source: "gh-shop", text: "push to main by kim", labels: { key: "acme/shop", user: "kim" } },
  { offset: "T-7200s", source: "pg-logs", text: "ERROR: deadlock detected\n  at checkout", labels: { service: "checkout" } },
];

/** The read's answer for a request: one row when narrowed by a second term, nothing for a project or
 *  over 30 days, both rows otherwise. */
function answer({ selector, window, project }) {
  const rows = project || window === "30d" ? [] : Object.keys(selector).length > 1 ? ROWS.slice(0, 1) : ROWS;
  const sources = [...new Set(rows.map((r) => r.source))];
  const head = Object.entries(selector).map(([k, v]) => `${k}=${v}`).join(" AND ");
  return { count: rows.length, sources, rows,
    payload: `# ${head}, last ${window}\n` + rows.map((r) => `${r.offset} ${r.source} ${r.text}`).join("\n") };
}

// opts: slow (ms /api/entities waits), fail (it answers 500 until `fail.until` calls have failed),
// readFail (POST /read answers 500), empty (no labels), real (leave /api/entities and /read alone).
async function mockExplore(page, { slow = 0, fail = false, readFail = false, empty = false, real = false } = {}) {
  const calls = { entities: 0, reads: [] };
  await page.route((u) => u.pathname === "/api/projects", async (route) => {
    const r = await route.fetch();
    const body = await r.json();
    const like = body.projects[0] ?? {};
    return json(route, 200, { ...body, projects: [...body.projects, { ...like, id: "shop", name: "Shop", hidden: false }] });
  });
  if (real) return calls;
  await page.route((u) => u.pathname === "/api/entities", async (route) => {
    calls.entities++;
    if (slow) await new Promise((r) => setTimeout(r, slow));
    if (fail && calls.entities <= fail.until) return json(route, 500, { detail: "entities failed" });
    return json(route, 200, empty ? { labels: [] } : ENTITIES);
  });
  await page.route((u) => u.pathname === "/read", async (route) => {
    const body = JSON.parse(route.request().postData() || "{}");
    calls.reads.push(body);
    if (readFail) return json(route, 500, { detail: "the store is busy" });
    return json(route, 200, answer(body));
  });
  return calls;
}

module.exports = { mockExplore, ENTITIES, ROWS };
