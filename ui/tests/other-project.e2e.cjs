/*
 * Browser e2e for TR-1396: what a factory cell shows at /projects/:id for a project that is not a
 * factory project (editions/factory/OtherProject.tsx), and where an old ?view= address leads.
 *
 * A project with no kind (made over the API, or before the cell became a factory cell) gets one kit page:
 * its name and status, what it is for, what it holds, where its full setup lives (the platform edition),
 * Pause or Turn back on, and Delete behind a dialog. A draft goes to its setup, an address with no project
 * says so. An old ?view= address on a factory project lands on the factory page that holds the same thing.
 * The factory edition no longer reaches the old project page (pages/ProjectShell) at all.
 *
 * Seeds a scratch factory cell (tests/seed-factory-ticket.py: project-y a factory project, project-w with no
 * kind, project-z a draft), then runs the real daemon on it, serving the built console. The states the seed
 * cannot make (parts, a part deleted by hand, an error, the Default project, failed reads) answer from a
 * routed summary.
 * Run:  npm run test:other-project     (from ui/; builds first; tares installed in ../.venv or on PATH)
 * Set SHOTS_DIR to keep a screenshot of every state at 1440 and 390 px.
 */
const http = require("http");
const { spawn, execFileSync } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");

const UI_DIR = path.resolve(__dirname, "..");
const REPO = path.resolve(UI_DIR, "..");
const { chromium } = require("./browser.cjs");
const { checkWords } = require("./words.cjs");

const PORT = Number(process.env.TARES_E2E_PORT || 8861);
const BASE = `http://127.0.0.1:${PORT}`;
const SHOTS = process.env.SHOTS_DIR;
const THEME_MS = 600;

let pass = 0, fail = 0;
const ck = (label, cond, detail = "") => {
  cond ? pass++ : fail++;
  console.log((cond ? "  ok   " : "  FAIL ") + label + (cond ? "" : `   ${detail}`));
};

async function waitFor(url, tries = 100) {
  for (; tries > 0; tries--) {
    const ok = await new Promise((res) => http.get(url, (r) => { r.resume(); res(true); }).on("error", () => res(false)));
    if (ok) return true;
    await new Promise((r) => setTimeout(r, 200));
  }
  return false;
}

(async () => {
  const bin = path.join(REPO, ".venv/bin");
  const tares = fs.existsSync(path.join(bin, "tares")) ? path.join(bin, "tares") : "tares";
  const python = fs.existsSync(path.join(bin, "python")) ? path.join(bin, "python") : "python3";
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "tares-other-project-e2e-"));
  const dist = process.env.TARES_UI_DIST || path.join(UI_DIR, "dist");
  const ids = JSON.parse(execFileSync(python, [path.join(__dirname, "seed-factory-ticket.py"), home],
    { cwd: REPO, encoding: "utf8", env: { ...process.env, PYTHONPATH: REPO } }).trim().split("\n").pop());
  const y = ids["project-y"], w = ids["project-w"], z = ids["project-z"];
  ck("seeded the projects", !!y && !!w && !!z);

  const env = { HOME: process.env.HOME, PATH: process.env.PATH, PYTHONPATH: REPO, TARES_EDITION: "factory", TARES_HOME: home,
                TARES_PORT: String(PORT), TARES_MCP_PORT: String(PORT + 1), TARES_UI_DIST: dist, TARES_OTLP_GRPC_PORT: "off" };
  const daemon = spawn(tares, ["up", "--port", String(PORT), "--data-dir", home], { cwd: REPO, stdio: "ignore", env });
  let browser;
  try {
    ck("daemon up", await waitFor(`${BASE}/health`));
    browser = await chromium.launch(process.env.TARES_E2E_CHROME ? { executablePath: process.env.TARES_E2E_CHROME } : {});
    const newPage = async (width) => {
      const ctx = await browser.newContext({ viewport: { width, height: width < 600 ? 844 : 900 } });
      return { ctx, page: await ctx.newPage() };
    };
    const setTheme = (page, theme) => page.evaluate((t) => document.documentElement.setAttribute("data-theme", t), theme);
    const shot = async (page, n) => { if (SHOTS) await page.screenshot({ path: path.join(SHOTS, n), fullPage: true }); };
    const overflow = (page) => page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    const txt = async (loc) => (await loc.innerText()).replace(/\s+/g, " ").trim();
    const raw = async (loc) => (await loc.textContent()).replace(/\s+/g, " ").trim();
    const path_ = (page) => { const u = new URL(page.url()); return u.pathname + u.search; };
    const real = await (await fetch(`${BASE}/api/projects/${w}/summary`)).json();
    // the summary the page reads, with what the seed cannot make laid over the real one
    const routeSummary = (page, over, status = 200) => page.route(`**/api/projects/${w}/summary`, (r) =>
      r.fulfill({ status, contentType: "application/json", body: JSON.stringify(status === 200 ? { ...real, ...over } : { detail: "the store is locked" }) }));
    const PARTS = [
      { kind: "trigger", key: "t1", name: "on-new-alert", customized: false, missing: false, created_at: real.created_at },
      { kind: "source", key: "s1", name: "prod-logs", customized: false, missing: false, created_at: real.created_at },
      { kind: "mcp_server", key: "m1", name: "github", customized: false, missing: false, created_at: real.created_at },
      { kind: "agent", key: "a1", name: "triage-agent-with-a-very-long-name-that-has-no-break-in-it-at-all", customized: false, missing: true, created_at: real.created_at },
      { kind: "source", key: "s0", name: "app-events", customized: false, missing: false, created_at: real.created_at },
    ];

    // ── 1. the project with no kind, as the cell holds it ────────────────────────────────────────
    {
      const { ctx, page } = await newPage(1440);
      const T = (id) => page.getByTestId(id);
      await page.goto(`${BASE}/projects/${w}`, { waitUntil: "load" });
      await T("other-project").waitFor();
      ck("a project with no kind opens the kit page, not the old one", (await page.locator(".pagehead").count()) === 0
         && (await T("factory-project").count()) === 0 && path_(page) === `/projects/${w}`, path_(page));
      ck("its name, and its status as a word with its tone", (await txt(T("other-name"))) === "project-w"
         && (await raw(T("other-status"))) === "On" && (await T("other-status").getAttribute("data-tone")) === "ready");
      ck("it says what it is not, and which template made it", (await txt(T("other-what"))).startsWith("Not a software factory project: it has no tickets, crew or decisions.")
         && (await txt(T("other-what"))).includes("made from the"), await txt(T("other-what")));
      ck("it says when it was made, with the UTC stamp on hover (…Z)", /Made .*ago|Made just now/.test(await txt(T("other-what")))
         && /^\d{4}-\d\d-\d\d \d\d:\d\dZ$/.test((await T("other-what").locator("[title]").getAttribute("title")) ?? ""),
         `${await txt(T("other-what"))} | ${await T("other-what").locator("[title]").getAttribute("title")}`);
      ck("what it is for", (await txt(T("other-goal"))).includes("What it is for"));
      ck("it holds nothing, and says so in one line", (await T("other-part").count()) === 0
         && (await txt(T("other-empty"))) === "It holds no connections, rules, agents or MCP servers." && (await T("other-count").count()) === 0);
      ck("its full setup is the platform edition's, with a link to Settings, Edition",
         (await T("other-edition-link").getAttribute("href")) === "/settings?tab=edition");
      ck("Pause and Delete, nothing else", (await T("other-pause").count()) === 1 && (await T("other-resume").count()) === 0
         && (await T("other-delete").count()) === 1 && (await T("other-actions").getByRole("button").count()) === 2);
      await checkWords(page, ck, "the page");
      for (const theme of ["light", "dark"]) {
        await setTheme(page, theme); await page.waitForTimeout(THEME_MS);
        await shot(page, `other-1440-${theme}.png`);
      }
      await setTheme(page, "light");

      // an old address on it stays here: there is no factory page to send it to
      for (const view of ["sources", "ticket:T1", "settings:members"]) {
        await page.goto(`${BASE}/projects/${w}?view=${view}`, { waitUntil: "load" });
        await page.getByTestId("route-probe").waitFor({ state: "detached" });
        await T("other-project").waitFor();
        ck(`?view=${view} on it shows this page, at the same address`, path_(page) === `/projects/${w}?view=${view}`, path_(page));
      }
      await ctx.close();
    }

    // ── 2. an old ?view= address on a factory project lands on its factory page ─────────────────
    {
      const { ctx, page } = await newPage(1440);
      const cases = [
        ["docs", `/projects/${y}/docs`],
        ["doc:no-such-doc", `/projects/${y}/docs`],   // the docs page drops a document that is not the project's
        ["tickets", `/projects/${y}/tickets`],
        ["ticket:T2", `/projects/${y}/tickets/T2`],
        ["settings:members", `/projects/${y}/people`],
        ["settings:keys", `/projects/${y}/settings`],
        ["grants", `/projects/${y}/settings`],
        ["events", `/projects/${y}`],
        ["overview", `/projects/${y}`],
      ];
      for (const [view, want] of cases) {
        await page.goto(`${BASE}/projects/${y}?view=${view}`, { waitUntil: "load" });
        await page.waitForURL((u) => !u.search.includes("view="), { timeout: 10000 }).catch(() => {});
        await page.waitForLoadState("networkidle");
        ck(`?view=${view} on a factory project goes to ${want.replace(`/projects/${y}`, "") || "its page"}`,
           path_(page) === want && (await page.getByTestId("other-project").count()) === 0, path_(page));
      }
      {
        // a probe that fails for any other reason than 404 is not "another kind of project": the address goes
        // on to the factory page, which says what failed
        let probes = 0;
        await page.route((u) => u.pathname === `/api/factory/projects/${y}`, (r) => (probes++ === 0
          ? r.fulfill({ status: 500, contentType: "application/json", body: JSON.stringify({ detail: "the store is locked" }) })
          : r.continue()));
        await page.goto(`${BASE}/projects/${y}?view=tickets`, { waitUntil: "load" });
        await page.waitForURL((u) => !u.search.includes("view="), { timeout: 10000 }).catch(() => {});
        ck("a failed probe (500) still goes to the factory page, not this one",
           path_(page) === `/projects/${y}/tickets` && (await page.getByTestId("other-project").count()) === 0, path_(page));
        await page.unrouteAll();
      }
      ck("a bare factory project address still opens the factory page", await (async () => {
        await page.goto(`${BASE}/projects/${y}`, { waitUntil: "load" });
        await page.getByTestId("factory-project").waitFor({ timeout: 10000 }).catch(() => {});
        return (await page.getByTestId("factory-project").count()) === 1 && (await page.getByTestId("other-project").count()) === 0;
      })());
      await ctx.close();
    }

    // ── 3. a draft, and an address with no project ────────────────────────────────────────────────
    {
      const { ctx, page } = await newPage(1440);
      await page.goto(`${BASE}/projects/${z}`, { waitUntil: "load" });
      await page.waitForURL((u) => u.pathname.endsWith("/setup"), { timeout: 10000 }).catch(() => {});
      ck("a draft opens at its setup", new URL(page.url()).pathname === `/projects/${z}/setup`, page.url());
      await page.goto(`${BASE}/projects/uc_nosuchthing`, { waitUntil: "load" });
      await page.getByTestId("other-missing").waitFor();
      ck("an address with no project says so, with a way to every project",
         (await txt(page.getByTestId("other-missing"))).startsWith("No project at this address")
         && (await page.getByTestId("other-missing").getByRole("link").getAttribute("href")) === "/projects"
         && (await page.getByTestId("other-failed").count()) === 0);
      await checkWords(page, ck, "no project");
      await ctx.close();
    }

    // ── 4. what the seed cannot make: parts, a part deleted by hand, an error, a pause ──────────
    for (const width of [1440, 390]) {
      const { ctx, page } = await newPage(width);
      const T = (id) => page.getByTestId(id);
      await routeSummary(page, { objects: PARTS, status: "error", last_error: "agent triage-agent: the model provider refused the key",
                                 lead: { id: "p1", name: "Ada Example" }, goal: "Watch the production logs and open an incident when the error rate jumps." });
      await page.goto(`${BASE}/projects/${w}`, { waitUntil: "load" });
      await T("other-part").first().waitFor();
      const rows = await T("other-part").allInnerTexts();
      if (width === 1440) {
        ck("the parts read in the edition's words, grouped by kind and sorted by name",
           JSON.stringify(await T("other-part-kind").allInnerTexts()) === JSON.stringify(["Connection", "Connection", "Rule", "Agent", "MCP server"])
           && JSON.stringify(await T("other-part-name").allInnerTexts()) === JSON.stringify(["app-events", "prod-logs", "on-new-alert", PARTS[3].name, "github"]),
           JSON.stringify(rows));
        ck("the count beside the title", (await txt(T("other-count"))) === "5");
        ck("a part deleted by hand says so, on its own row only", (await T("other-part-missing").count()) === 1
           && (await txt(T("other-part").nth(3))).includes("deleted outside the project"));
        ck("a failing project says why, in the error tone", (await raw(T("other-status"))) === "Failing" && (await T("other-status").getAttribute("data-tone")) === "bad"
           && (await txt(T("other-error"))).includes("the model provider refused the key") && (await T("other-error").getAttribute("role")) === "alert");
        ck("who leads it", (await txt(T("other-what"))).includes("Led by Ada Example."));
        ck("the goal, as written", (await txt(T("other-goal"))).includes("Watch the production logs"));
        ck("its names and goal are marked as the person's own text", (await T("other-part-name").first().getAttribute("data-user-text")) !== null
           && (await T("other-goal").locator("p").getAttribute("data-user-text")) !== null && (await T("other-name").getAttribute("data-user-text")) !== null);
        await checkWords(page, ck, "with parts");
      }
      for (const theme of ["light", "dark"]) {
        await setTheme(page, theme); await page.waitForTimeout(THEME_MS);
        ck(`${width} ${theme}: no sideways scroll`, (await overflow(page)) <= 0, String(await overflow(page)));
        await shot(page, `other-parts-${width}-${theme}.png`);
      }
      if (width === 390) {
        const [a, b] = await Promise.all([T("other-part-kind").first().boundingBox(), T("other-part-name").first().boundingBox()]);
        ck("390: a row keeps its kind and name side by side", !!a && !!b && b.x > a.x + a.width - 1, JSON.stringify([a, b]));
      }
      await ctx.close();
    }
    {
      const { ctx, page } = await newPage(1440);
      const T = (id) => page.getByTestId(id);
      await routeSummary(page, { status: "paused" });
      await page.goto(`${BASE}/projects/${w}`, { waitUntil: "load" });
      await T("other-resume").waitFor();
      ck("a paused project offers to turn it back on, not to pause it", (await T("other-pause").count()) === 0
         && (await raw(T("other-status"))) === "Paused" && (await T("other-status").getAttribute("data-tone")) === "waiting");
      await page.unrouteAll();
      await routeSummary(page, { default: true, status: "active" });
      await page.goto(`${BASE}/projects/${w}`, { waitUntil: "load" });
      await T("other-project").waitFor();
      ck("the Default project is never paused or deleted", (await T("other-actions").count()) === 0);
      await page.unrouteAll();
      await routeSummary(page, { status: "done" });
      await page.goto(`${BASE}/projects/${w}`, { waitUntil: "load" });
      await T("other-project").waitFor();
      ck("a finished project offers Delete only", (await T("other-pause").count()) === 0 && (await T("other-resume").count()) === 0
         && (await T("other-delete").count()) === 1 && (await raw(T("other-status"))) === "Finished");
      await ctx.close();
    }

    // ── 5. reads that fail ────────────────────────────────────────────────────────────────────────
    {
      const { ctx, page } = await newPage(1440);
      const T = (id) => page.getByTestId(id);
      let broken = true;
      await page.route(`**/api/projects/${w}/summary`, (r) => (broken
        ? r.fulfill({ status: 500, contentType: "application/json", body: JSON.stringify({ detail: "the store is locked" }) })
        : r.continue()));
      await page.goto(`${BASE}/projects/${w}`, { waitUntil: "load" });
      await T("other-failed").waitFor();
      ck("a failed read says so, with the reason, and is not taken for a missing project",
         (await txt(T("other-failed"))).includes("Tares could not read this project") && (await txt(T("other-failed"))).includes("the store is locked")
         && (await T("other-missing").count()) === 0 && (await T("other-failed").getAttribute("role")) === "alert");
      await checkWords(page, ck, "failed read");
      broken = false;
      await T("other-failed").getByRole("button", { name: "Try again" }).click();
      await T("other-project").waitFor();
      ck("Try again reads it again", (await txt(T("other-name"))) === "project-w" && (await T("other-failed").count()) === 0);
      broken = true;
      await page.evaluate(() => document.dispatchEvent(new Event("visibilitychange")));
      await T("other-stale").waitFor({ timeout: 8000 }).catch(() => {});
      ck("a later read that fails keeps the page and says it is not the latest", (await T("other-stale").count()) === 1
         && (await T("other-name").count()) === 1, String(await T("other-stale").count()));
      await ctx.close();
    }

    // ── 6. pause, turn back on, delete (the real daemon) ──────────────────────────────────────────
    {
      const { ctx, page } = await newPage(390);
      const T = (id) => page.getByTestId(id);
      await page.goto(`${BASE}/projects/${w}`, { waitUntil: "load" });
      await T("other-pause").click();
      await T("other-resume").waitFor();
      const after = await (await fetch(`${BASE}/api/projects/${w}/summary`)).json();
      ck("Pause pauses it on the cell", after.status === "paused" && (await raw(T("other-status"))) === "Paused", after.status);
      await T("other-resume").click();
      await T("other-pause").waitFor();
      ck("Turn back on turns it on", (await (await fetch(`${BASE}/api/projects/${w}/summary`)).json()).status === "active");

      // a delete the cell refuses closes the dialog and says why
      await page.route(`**/api/projects/${w}?*`, (r) => (r.request().method() === "DELETE"
        ? r.fulfill({ status: 500, contentType: "application/json", body: JSON.stringify({ detail: "the store is locked" }) })
        : r.continue()));
      await T("other-delete").click();
      await T("other-delete-dialog").waitFor();
      ck("Delete asks first, naming the project and what goes with it",
         (await txt(T("other-delete-dialog"))).includes("Delete project-w?")
         && (await txt(T("other-delete-dialog"))).includes("Its rules, agents and MCP servers go with it."));
      await shot(page, "other-delete-390-light.png");
      await T("other-delete-dialog").getByRole("button", { name: "Keep it" }).click();
      await T("other-delete-dialog").waitFor({ state: "detached" });
      ck("Keep it closes the dialog and keeps the project", (await fetch(`${BASE}/api/projects/${w}/summary`)).status === 200);
      await T("other-delete").click();
      await T("other-delete-confirm").click();
      await T("other-problem").waitFor();
      ck("a refused delete closes the dialog and says why", (await T("other-delete-dialog").count()) === 0
         && (await txt(T("other-problem"))).includes("the store is locked") && path_(page) === `/projects/${w}`);
      await page.unrouteAll();
      await T("other-delete").click();
      await T("other-delete-confirm").click();
      await page.waitForURL((u) => u.pathname === "/projects", { timeout: 10000 }).catch(() => {});
      ck("Delete project deletes it and goes to Projects", new URL(page.url()).pathname === "/projects"
         && (await fetch(`${BASE}/api/projects/${w}/summary`)).status === 404, page.url());
      await ctx.close();
    }
  } finally {
    if (browser) await browser.close();
    daemon.kill();
    fs.rmSync(home, { recursive: true, force: true });
  }
  console.log(`\n${pass} passed, ${fail} failed`);
  process.exit(fail ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(1); });
