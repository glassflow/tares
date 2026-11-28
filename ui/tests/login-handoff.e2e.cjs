/*
 * Browser e2e for the cloud login handoff (TARES_LOGIN_URL). Runs the REAL daemon serving the
 * built console behind a stub "control plane", and drives the actual AuthGate with Playwright:
 *   1. /health advertises login_url when TARES_LOGIN_URL is set
 *   2. logged out + login_url present  → redirect to  {login_url}?return=<cell host>
 *   3. ?code= present                  → POST {origin}/exchange, store the real key, strip the URL,
 *                                        and authenticate against the daemon (token persists)
 *   4. self-hosted (`tares up --auth`, no login_url): the token form. A bad token says Invalid
 *      token; the root token and a read or ingest key each sign in and the shell loads.
 *      A second daemon on DAEMON_PORT + 3 (MCP + 4) serves it.
 *
 * No cloud, no release. The stub mints a real scoped key on the daemon (as the control plane does),
 * so the exchanged token actually works — the console stays authed instead of bouncing to login.
 *
 * Run:  node tests/login-handoff.e2e.cjs     (after a console build; npm run test:e2e does both)
 * The cell is new (no project, setup not done), so it lands signed in on Set up your factory.
 * Needs: the tares daemon importable (repo .venv or system python with tares installed).
 */
const http = require("http");
const { spawn } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");

const UI_DIR = path.resolve(__dirname, "..");        // <repo>/ui
const REPO = path.resolve(__dirname, "..", "..");    // <repo>
const { chromium } = require("./browser.cjs");

const DAEMON_PORT = 8808;
const CP_PORT = 8809;
const ROOT_TOKEN = "root-secret";
const LOGIN_URL = `http://localhost:${CP_PORT}/login`;
const SELF_PORT = DAEMON_PORT + 3;   // the self-hosted daemon: auth on, no login_url

let pass = 0, fail = 0;
const ck = (label, cond, detail = "") => {
  cond ? pass++ : fail++;
  console.log((cond ? "  ok   " : "  FAIL ") + label + (cond ? "" : `   ${detail}`));
};

// Mint a real scoped key on the daemon with the root token — exactly what the control plane's
// /exchange does. Returns a token the daemon will accept, so the console stays authenticated.
function mintKey(port = DAEMON_PORT, scopes = ["read", "ingest", "admin"]) {
  return new Promise((resolve, reject) => {
    const body = JSON.stringify({ name: `e2e-${scopes.join("-")}`, scopes });
    const r = http.request(`http://localhost:${port}/api/keys`, {
      method: "POST",
      headers: { "content-type": "application/json", authorization: `Bearer ${ROOT_TOKEN}`,
                 "content-length": Buffer.byteLength(body) },
    }, (res) => {
      let d = ""; res.on("data", (c) => (d += c));
      res.on("end", () => { try { const j = JSON.parse(d); j.secret ? resolve(j.secret) : reject(new Error(d)); } catch (e) { reject(e); } });
    });
    r.on("error", reject); r.write(body); r.end();
  });
}

// Stub control plane: GET /login (landing) + POST /exchange (mints a real key), with CORS.
const cp = http.createServer((req, res) => {
  const cors = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Access-Control-Allow-Headers": "content-type",
  };
  if (req.method === "OPTIONS") { res.writeHead(204, cors); return res.end(); }
  const u = new URL(req.url, `http://localhost:${CP_PORT}`);
  if (u.pathname === "/login") {
    res.writeHead(200, { "content-type": "text/html" });
    return res.end(`<h1>STUB LOGIN</h1><p>return=${u.searchParams.get("return") || ""}</p>`);
  }
  if (u.pathname === "/exchange" && req.method === "POST") {
    let b = ""; req.on("data", (c) => (b += c));
    return req.on("end", async () => {
      let code = null; try { code = JSON.parse(b).code; } catch {}
      if (!code) { res.writeHead(400, { "content-type": "application/json", ...cors }); return res.end('{"detail":"no code"}'); }
      try {
        const token = await mintKey();
        res.writeHead(200, { "content-type": "application/json", ...cors });
        res.end(JSON.stringify({ token }));
      } catch (e) {
        res.writeHead(500, { "content-type": "application/json", ...cors });
        res.end(JSON.stringify({ detail: String(e) }));
      }
    });
  }
  res.writeHead(404, cors); res.end("nope");
});

function waitFor(url, tries = 100) {
  return new Promise((resolve) => {
    const tick = () => http.get(url, (r) => { r.resume(); resolve(true); })
      .on("error", () => (--tries > 0 ? setTimeout(tick, 200) : resolve(false)));
    tick();
  });
}

(async () => {
  const py = fs.existsSync(path.join(REPO, ".venv/bin/python3"))
    ? path.join(REPO, ".venv/bin/python3") : "python3";
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "tares-e2e-"));
  const db = path.join(tmp, "handoff");
  const seed = path.join(tmp, "seed.yaml");
  fs.writeFileSync(seed, "sources:\n  - name: evt\n    connector: webhook\n    poll: 5s\n    config: {}\n");

  cp.listen(CP_PORT);
  // A clean environment: only what the shell needs, so no inherited TARES_*, model or Slack keys leak in.
  const base0 = {};
  for (const k of ["PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "VIRTUAL_ENV"]) if (process.env[k]) base0[k] = process.env[k];
  const env = { ...base0, TARES_HOME: db, TARES_CATALOG: seed, TARES_PORT: String(DAEMON_PORT),
                TARES_MCP_PORT: String(DAEMON_PORT + 2), TARES_OTLP_GRPC_PORT: "off",
                TARES_AUTH_TOKEN: ROOT_TOKEN, TARES_LOGIN_URL: LOGIN_URL,
                TARES_UI_DIST: path.join(UI_DIR, "dist"), PYTHONPATH: REPO };
  const daemon = spawn(py, ["-c", "from tares.cli import run_daemon; run_daemon()"], {
    cwd: REPO, env, stdio: "ignore",
  });
  // the self-hosted cell: the same, with auth on and no control plane to send a person to
  const selfEnv = { ...env, TARES_HOME: path.join(tmp, "self"), TARES_PORT: String(SELF_PORT),
                    TARES_MCP_PORT: String(SELF_PORT + 1) };
  delete selfEnv.TARES_LOGIN_URL;
  const selfDaemon = spawn(py, ["-c", "from tares.cli import run_daemon; run_daemon()"], {
    cwd: REPO, env: selfEnv, stdio: "ignore",
  });

  const base = `http://localhost:${DAEMON_PORT}`;
  ck("daemon up + serving", await waitFor(`${base}/health`));

  const health = await new Promise((res) =>
    http.get(`${base}/health`, (r) => { let d = ""; r.on("data", (c) => (d += c)); r.on("end", () => res(JSON.parse(d))); }));
  ck("/health advertises login_url", health.login_url === LOGIN_URL, JSON.stringify(health));

  const browser = await chromium.launch();
  try {
    // Test: logged out → redirect to the hosted login with ?return=<host>
    {
      const ctx = await browser.newContext();
      const page = await ctx.newPage();
      await page.goto(`${base}/`, { waitUntil: "domcontentloaded" });
      let redirected = true;
      try { await page.waitForURL(new RegExp(`localhost:${CP_PORT}/login`), { timeout: 8000 }); } catch { redirected = false; }
      ck("logged-out console redirects to login_url", redirected, "url=" + page.url());
      if (redirected) {
        const ret = new URL(page.url()).searchParams.get("return");
        ck("redirect carries ?return=<cell host>", ret === `localhost:${DAEMON_PORT}`, "return=" + ret);
      }
      await ctx.close();
    }

    // Test: ?code= → exchange for a real key, store it, strip the URL, stay authed
    {
      const ctx = await browser.newContext();
      const page = await ctx.newPage();
      await page.goto(`${base}/?code=ABC123`, { waitUntil: "domcontentloaded" });
      let token = null;
      try {
        await page.waitForFunction(() => !!localStorage.getItem("tares_token"), { timeout: 8000 });
        token = await page.evaluate(() => localStorage.getItem("tares_token"));
      } catch {}
      ck("?code= exchanged → real cell key stored", !!token && token.startsWith("nvf_"), "token=" + token);
      ck("?code= stripped from the URL", !page.url().includes("code="), "url=" + page.url());
      await page.waitForTimeout(2000); // authed API calls would clear a bad token; a real key persists
      const still = await page.evaluate(() => localStorage.getItem("tares_token"));
      ck("token persists (console authenticates, no clear)", !!still, "token=" + still);
      ck("stays on the cell (no redirect once authed)", page.url().startsWith(base), "url=" + page.url());
      // a new cell (no project, setup not done) opens Set up your factory once signed in
      ck("lands on /setup signed in", new URL(page.url()).pathname === "/setup", "url=" + page.url());
      ck("the app shows, not the login page", !(await page.content()).includes("STUB LOGIN") && (await page.locator("#root *").count()) > 3);
      await ctx.close();
    }

    // Test: self-hosted, the token form. A bad token is refused; the root token and a read or an
    // ingest key each sign in (the form checks the token with /api/whoami, which takes any scope).
    {
      const selfBase = `http://localhost:${SELF_PORT}`;
      ck("self-hosted daemon up", await waitFor(`${selfBase}/health`));
      const keys = [["the root token", ROOT_TOKEN],
                    ["a read key", await mintKey(SELF_PORT, ["read"])],
                    ["an ingest key", await mintKey(SELF_PORT, ["ingest"])]];
      const signIn = async (token) => {
        const ctx = await browser.newContext();
        const page = await ctx.newPage();
        await page.goto(`${selfBase}/`, { waitUntil: "domcontentloaded" });
        const field = page.locator(".login input[type=password]");
        await field.waitFor({ timeout: 8000 });
        await field.fill(token);
        await page.locator(".login button").click();
        return { ctx, page };
      };
      {
        const { ctx, page } = await signIn("not-a-real-token");
        let said = true;
        try { await page.locator(".login-err").waitFor({ timeout: 8000 }); } catch { said = false; }
        ck("self-hosted: a bad token says Invalid token",
           said && (await page.locator(".login-err").innerText()).trim() === "Invalid token.");
        ck("self-hosted: and keeps nothing", !(await page.evaluate(() => localStorage.getItem("tares_token"))));
        await ctx.close();
      }
      for (const [what, token] of keys) {
        const { ctx, page } = await signIn(token);
        let shell = true;
        try { await page.getByTestId("edition-menu").waitFor({ timeout: 8000 }); } catch { shell = false; }
        ck(`self-hosted: ${what} signs in and the shell loads`, shell && (await page.locator(".login").count()) === 0,
           (await page.locator(".login-err").count()) ? await page.locator(".login-err").innerText() : "no shell");
        ck(`self-hosted: ${what} is kept`, (await page.evaluate(() => localStorage.getItem("tares_token"))) === token);
        await ctx.close();
      }
    }
  } finally {
    await browser.close();
    selfDaemon.kill("SIGTERM");
    daemon.kill("SIGTERM");
    cp.close();
    try { fs.rmSync(tmp, { recursive: true, force: true }); } catch {}
  }

  console.log(`\n${pass} passed, ${fail} failed`);
  process.exit(fail ? 1 : 0);
})();
