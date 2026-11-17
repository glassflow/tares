/*
 * The network around /sources/import (TR-1394), shared by catalog-import.e2e.cjs and
 * catalog-import.shots.cjs. The daemon is real and so is the import, unless `importStatus` asks for
 * an answer; every import call is recorded, and whether the key may write is mocked at whoami.
 */
const json = (route, status, body) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });

// A file the real daemon takes: two connections and a rule over one of them.
const GOOD = [
  "sources:",
  "  - name: shop-web",
  "    connector: docker_logs",
  "    poll: 30s",
  "    config: {container: shop-web-1}",
  "  - name: shop-db",
  "    connector: docker_logs",
  "    poll: 30s",
  "    config: {container: shop-db-1}",
  "triggers:",
  "  - name: shop-errors",
  "    sources: [shop-web]",
  "    key_field: container",
  "    condition: {aggregate: count, predicate: \"> 0\", window: 1m}",
  "",
].join("\n");
// One the daemon refuses as a whole: the second connection's kind does not exist.
const BAD = GOOD.replace("connector: docker_logs\n    poll: 30s\n    config: {container: shop-db-1}", "connector: no_such_kind");

// opts: importStatus (403 refuses, any other number fails with a reason, unset goes to the daemon),
// readOnly (the key can read but not write), calls (an array each import body is pushed onto),
// slow (ms the import waits before answering), edition ("sre") answers whoami as that edition.
async function mockImport(page, { importStatus, readOnly = false, calls = [], slow = 0, edition } = {}) {
  if (readOnly) await page.addInitScript(() => localStorage.setItem("tares_token", "e2e"));
  if (readOnly || edition) {
    await page.route((u) => u.pathname === "/api/whoami", async (route) => {
      const r = await route.fetch();
      return json(route, 200, { ...(await r.json()), ...(edition ? { edition } : {}),
                                ...(readOnly ? { id: "e2e-read", name: "read key", scopes: ["read"] } : {}) });
    });
  }
  await page.route((u) => u.pathname === "/api/catalog/import", async (route) => {
    calls.push(JSON.parse(route.request().postData() || "{}"));
    if (slow) await new Promise((r) => setTimeout(r, slow));
    if (importStatus === 403) return json(route, 403, { detail: "this credential lacks the 'admin' scope" });
    if (importStatus) return json(route, importStatus, { detail: "import failed: disk full" });
    return route.fallback();
  });
}

module.exports = { mockImport, GOOD, BAD };
