/*
 * The network around the old addresses (/agents/<name>, /triggers/<name>, /dispatches/<id>) and the
 * not-found page (TR-1394), shared by redirects.e2e.cjs and redirects.shots.cjs. The daemon is real;
 * the three lookups each address makes are mocked, so a test can say what Tares knows.
 */
const json = (route, status, body) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });

// What Tares knows by default: one agent and one rule in project "shop", one notification of that rule.
const KNOWN = {
  agents: [{ name: "watcher", project: "shop" }, { name: "loose", project: null }],
  triggers: [{ name: "shop-errors", project: "shop" }],
  dispatches: { "d-1": { id: "d-1", trigger: "shop-errors" } },
};

// opts: known (overrides KNOWN), slow (ms each lookup waits), fail (every lookup answers 500),
// edition ("sre") answers whoami as that edition.
async function mockLookups(page, { known = {}, slow = 0, fail = false, edition } = {}) {
  const k = { ...KNOWN, ...known };
  const wait = () => (slow ? new Promise((r) => setTimeout(r, slow)) : null);
  if (edition) {
    await page.route((u) => u.pathname === "/api/whoami", async (route) => {
      const r = await route.fetch();
      return json(route, 200, { ...(await r.json()), edition });
    });
  }
  await page.route((u) => u.pathname === "/api/agents/builtin", async (route) => {
    await wait();
    if (fail) return json(route, 500, { detail: "lookup failed" });
    const r = await route.fetch();
    return json(route, 200, { ...(await r.json()), agents: k.agents });
  });
  await page.route((u) => u.pathname === "/api/triggers", async (route) => {
    await wait();
    return fail ? json(route, 500, { detail: "lookup failed" }) : json(route, 200, k.triggers);
  });
  await page.route((u) => u.pathname.startsWith("/api/activity/dispatches/"), async (route) => {
    await wait();
    if (fail) return json(route, 500, { detail: "lookup failed" });
    const id = decodeURIComponent(new URL(route.request().url()).pathname.split("/").pop());
    const d = k.dispatches[id];
    return d ? json(route, 200, d) : json(route, 404, { detail: "no such dispatch" });
  });
}

module.exports = { mockLookups, KNOWN };
