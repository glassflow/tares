/*
 * What All resources (/resources, TR-1395) is shown, shared by resources.e2e.cjs and resources.shots.cjs.
 * The daemon is real; /api/resources is mocked so every kind and every state is on the page at once.
 * Times are relative to the moment the page asks, so "3 minutes ago" stays 3 minutes ago.
 */
const json = (route, status, body) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });

const SHOP = { id: "shop", name: "Shop" };
const BILLING = { id: "billing", name: "Billing" };
const ago = (ms) => new Date(Date.now() - ms).toISOString();
const MIN = 60_000, HOUR = 60 * MIN, DAY = 24 * HOUR;

// One builder, so each request gets fresh times.
const RESOURCES = () => ({
  sources: [
    { name: "gh-shop", title: "acme/shop", kind: "github", kind_label: "GitHub", what: "GitHub",
      state: "receiving", detail: "every 30s", last_event_at: ago(3 * MIN), used_by: [BILLING, SHOP] },
    { name: "pg-logs", kind: "postgres", kind_label: "Postgres", what: "Postgres",
      state: "error", detail: "Connection refused", last_event_at: null, used_by: [] },
  ],
  triggers: [
    { name: "shop-errors", what: "When acme/shop reports an error", state: "active", last_fired_at: ago(2 * HOUR), used_by: [SHOP] },
    { name: "old-rule", what: "When Postgres logs a deadlock", state: "paused", last_fired_at: null, used_by: [] },
  ],
  agents: [
    { name: "watcher", what: "Tares agent on the default model", state: "on", last_run_at: ago(DAY), last_run_status: "ok", used_by: [SHOP] },
    { name: "helper", what: "Tares agent on the default model", state: "on handoff", last_run_at: null, used_by: [] },
  ],
  tools: [
    { name: "tool-a", what: "MCP server at mcp.example.com", url: "https://mcp.example.com/sse", state: "set", used_by: [SHOP] },
    { name: "tool-b", what: "MCP server at other.example.com", url: "https://other.example.com/mcp", state: "no credentials", used_by: [] },
  ],
  skills: [],
  keys: [
    { name: "user:42", title: "Console sign-in for a workspace member", what: "Cell key: admin, read", prefix: "trs_ab",
      state: "used", last_used_at: ago(5 * MIN), used_by: [] },
    { name: "ci", what: "Project key: reads the project and records findings", prefix: "trs_cd",
      state: "never used", last_used_at: null, used_by: [SHOP] },
  ],
});

// opts: slow (ms /api/resources waits), fail (it answers 500 until `fail.until` calls have failed;
// true fails every call), edition ("sre") answers whoami as that edition, real (leave /api/resources alone).
async function mockResources(page, { slow = 0, fail = false, edition, real = false } = {}) {
  const calls = { resources: 0 };
  if (edition) {
    await page.route((u) => u.pathname === "/api/whoami", async (route) => {
      const r = await route.fetch();
      return json(route, 200, { ...(await r.json()), edition });
    });
  }
  if (!real) {
    await page.route((u) => u.pathname === "/api/resources", async (route) => {
      calls.resources++;
      if (slow) await new Promise((r) => setTimeout(r, slow));
      const failing = fail === true || (fail && calls.resources <= fail.until);
      return failing ? json(route, 500, { detail: "resources failed" }) : json(route, 200, RESOURCES());
    });
  }
  return calls;
}

module.exports = { mockResources, RESOURCES };
