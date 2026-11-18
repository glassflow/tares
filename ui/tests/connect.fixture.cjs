/*
 * What Connect (/connect, TR-1395) is shown, shared by connect.e2e.cjs and connect.shots.cjs.
 * The daemon is real (with or without --auth); the MCP probe, /api/mcp/tools and /api/triggers are
 * mocked so the endpoint's state, the tool list and the first rule hold known values.
 */
const json = (route, status, body) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });

const TOOLS = [
  { name: "read", description: "One correlated timeline for a key over a window." },
  { name: "query", description: "Search events by text across every connection." },
  { name: "catalog_list", description: "What this Tares receives: connections, rules and projects." },
  { name: "remember", description: "Write a conclusion back, attached to the next timeline." },
];
const RULES = [{ name: "error_spike", kind: "alert" }, { name: "deploy_failed", kind: "alert" }];

/**
 * opts:
 *   mcp: "same" (a proxy serves /mcp on this origin), "port" (only :8788 answers), "none" (nothing does)
 *   tools: an array, "fail" (500 until `toolsFailUntil` calls failed), or "slow" (waits 4 s)
 *   rules: an array (default RULES), or "fail" (500 every time)
 * Returns counters the suite reads: { toolCalls, probes: [url…] }.
 */
async function mockConnect(page, { mcp = "same", tools = TOOLS, toolsFailUntil = Infinity, rules = RULES } = {}) {
  const calls = { toolCalls: 0, probes: [] };
  await page.route((u) => u.pathname === "/mcp", async (route) => {
    const u = new URL(route.request().url());
    calls.probes.push(u.port || "same");
    const local = u.port === "8788";
    if (mcp === "same" && !local) return route.fulfill({ status: 200, contentType: "text/event-stream", body: "" });
    if (mcp === "port" && local) return route.fulfill({ status: 200, contentType: "application/json", body: "{}" });
    if (!local) return route.fulfill({ status: 200, contentType: "text/html", body: "<!doctype html><title>Tares</title>" });
    return route.abort("connectionrefused");
  });
  await page.route("**/api/mcp/tools", async (route) => {
    calls.toolCalls++;
    if (tools === "slow") { await new Promise((r) => setTimeout(r, 4000)); return json(route, 200, TOOLS); }
    if (tools === "fail") return calls.toolCalls <= toolsFailUntil ? json(route, 500, { detail: "the MCP server did not answer" }) : json(route, 200, TOOLS);
    return json(route, 200, tools);
  });
  await page.route("**/api/triggers", (route) =>
    rules === "fail" ? json(route, 500, { detail: "database is locked" }) : json(route, 200, rules));
  return calls;
}

module.exports = { mockConnect, TOOLS, RULES };
