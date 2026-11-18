/*
 * The network around /sources/claude-code (TR-1394), shared by source-claude-code.e2e.cjs and
 * source-claude-code.shots.cjs. The daemon is real; whether the cell needs a key, and whether a Claude Code
 * connection exists yet, are mocked at the network.
 */
const json = (route, status, body) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });

const CONNECTION = { name: "claude_code", type: "push", connector: "claude_code", poll: "", config: {}, paused: false, health: null };
const OTHER = { ...CONNECTION, name: "shop-web", type: "pull", connector: "docker_logs", poll: "10s" };

// opts: auth (true: the cell needs a key; false: open; "fails": health can't be read),
// connected (a Claude Code connection exists; a function is asked on every poll), name (its name).
async function mockClaudeCode(page, { auth = false, connected = false, name = "claude_code" } = {}) {
  if (auth === true) await page.addInitScript(() => localStorage.setItem("tares_token", "e2e"));
  await page.route((u) => u.pathname === "/health", async (route) => {
    if (auth === "fails") return json(route, 503, { detail: "starting" });
    const r = await route.fetch();
    return json(route, r.status(), { ...(await r.json()), auth_required: auth === true });
  });
  await page.route((u) => u.pathname === "/api/sources", async (route) => {
    if (route.request().method() !== "GET") return route.fallback();
    const on = typeof connected === "function" ? connected() : connected;
    // Another kind of connection is always there, so "connected" has to mean Claude Code, not any connection.
    return json(route, 200, [OTHER, ...(on ? [{ ...CONNECTION, name }] : [])]);
  });
}

module.exports = { mockClaudeCode };
