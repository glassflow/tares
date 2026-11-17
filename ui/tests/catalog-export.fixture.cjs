/*
 * The network around /sources/export (TR-1394), shared by catalog-export.e2e.cjs and
 * catalog-export.shots.cjs. The daemon is real; the connections list, the export answer and whether
 * the key may write are mocked at the network, and every export call is recorded.
 */
const json = (route, status, body) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });

const REDACTED = "••••••••";
const BASE = { type: "pull", poll: "30s", paused: false, health: null };
const CONNECTIONS = [
  { ...BASE, name: "shop-web", connector: "docker_logs", config: { container: "shop-web-1" } },
  { ...BASE, name: "gh-main", connector: "github", config: { repo: "acme/shop", token: REDACTED } },
  { ...BASE, name: "metrics", connector: "prometheus", config: { url: "http://prom:9090" } },
];

// opts: connections (the list; "fails" answers 500), exportStatus (the export's status; 200 answers a
// file naming what was asked), readOnly (the key can read but not write), calls (an array each export
// call's query is pushed onto), slow (ms the export waits before answering),
// edition ("sre") answers whoami as that edition.
async function mockExport(page, { connections = CONNECTIONS, exportStatus = 200, readOnly = false, calls = [], slow = 0, edition } = {}) {
  if (readOnly) await page.addInitScript(() => localStorage.setItem("tares_token", "e2e"));
  if (readOnly || edition) {
    await page.route((u) => u.pathname === "/api/whoami", async (route) => {
      const r = await route.fetch();
      return json(route, 200, { ...(await r.json()), ...(edition ? { edition } : {}),
                                ...(readOnly ? { id: "e2e-read", name: "read key", scopes: ["read"] } : {}) });
    });
  }
  await page.route((u) => u.pathname === "/api/sources", (route) => {
    if (route.request().method() !== "GET") return route.fallback();
    if (connections === "fails") return json(route, 500, { detail: "the store is not reachable" });
    return json(route, 200, connections);
  });
  await page.route((u) => u.pathname === "/api/catalog/export", async (route) => {
    const q = new URL(route.request().url()).searchParams;
    calls.push({ sources: q.get("sources"), include_secrets: q.get("include_secrets") });
    if (slow) await new Promise((r) => setTimeout(r, slow));
    if (exportStatus === 403) return json(route, 403, { detail: "this credential lacks the 'admin' scope" });
    if (exportStatus !== 200) return json(route, exportStatus, { detail: "export failed: disk full" });
    const names = q.get("sources") ? q.get("sources").split(",") : CONNECTIONS.map((c) => c.name);
    const body = ["sources:", ...names.map((n) => `  - name: ${n}`),
                  ...(q.get("include_secrets") ? ["    config: {token: ghp_secret}"] : []), "triggers: []", ""].join("\n");
    return route.fulfill({ status: 200, contentType: "application/yaml", body });
  });
}

module.exports = { mockExport, CONNECTIONS };
