/*
 * The network around /sources/new (TR-1394), shared by source-new.e2e.cjs and source-new.shots.cjs. The
 * daemon is real; what a test needs to steer is mocked at the network: whether the cell requires auth,
 * whether Docker is reachable, what creating a connection, minting its key and importing YAML answer.
 * Every write lands in `calls` as "METHOD /path" with its body.
 */
const json = (route, status, body) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });

const SECRET = "tares_ing_example_fixture"; // not a key: what the mocked mint answers
const SHOP = { id: "p-shop", name: "Shop" };

// opts: auth (cell requires keys), docker (false: Docker unreachable), keyFails (minting fails),
// imported ({sources, triggers, agents, mcp_servers} names, or an Error message string to refuse),
// project (serve SHOP in /api/projects), calls (array to record writes).
async function mockNew(page, { auth = false, docker = true, keyFails = false, imported, project = false, calls = [] } = {}) {
  // A cell that requires keys sends the console to its login form without a token; the daemon under
  // test is open, so any token passes.
  if (auth) await page.addInitScript(() => localStorage.setItem("tares_token", "e2e"));
  await page.route("**/health", async (route) => {
    const r = await route.fetch();
    return json(route, r.status(), { ...(await r.json()), auth_required: auth });
  });
  await page.route("**/api/capabilities", async (route) => {
    const r = await route.fetch();
    return json(route, r.status(), { ...(await r.json()), discover_docker: docker });
  });
  if (project) {
    await page.route("**/api/projects", async (route) => {
      if (route.request().method() !== "GET") return route.fallback();
      const r = await route.fetch();
      const body = await r.json();
      return json(route, 200, { ...body, projects: [...(body.projects ?? []), { ...(body.projects?.[0] ?? {}), ...SHOP }] });
    });
  }
  await page.route("**/api/sources", (route) => {
    if (route.request().method() !== "POST") return route.fallback();
    const body = JSON.parse(route.request().postData() || "{}");
    calls.push({ call: "POST /api/sources", body });
    return json(route, 200, { ok: true, name: body.name, ingest_key: `k-${body.name}` });
  });
  await page.route("**/api/keys", (route) => {
    if (route.request().method() !== "POST") return route.fallback();
    const body = JSON.parse(route.request().postData() || "{}");
    calls.push({ call: "POST /api/keys", body });
    if (keyFails) return json(route, 500, { detail: "the key store is read-only" });
    return json(route, 200, { id: "key-1", name: body.name, scopes: body.scopes, secret: SECRET, person: null });
  });
  await page.route("**/api/catalog/import", (route) => {
    const body = JSON.parse(route.request().postData() || "{}");
    calls.push({ call: "POST /api/catalog/import", body });
    if (typeof imported === "string") return json(route, 400, { detail: imported });
    const names = { sources: [], triggers: [], agents: [], mcp_servers: [], ...(imported ?? {}) };
    return json(route, 200, { sources: names.sources.length, triggers: names.triggers.length,
                              agents: names.agents.length, mcp_servers: names.mcp_servers.length, names });
  });
  return calls;
}

// The kind picker on the old page and the new one: a card button holding the key, or a row with data-kind.
const kindButton = (page, key) => page.locator(`[data-testid=connection-kind][data-kind="${key}"]`)
  .or(page.locator("button.connector-card", { has: page.locator(".name", { hasText: new RegExp(`^${key}$`) }) }));

module.exports = { SECRET, SHOP, kindButton, mockNew };
