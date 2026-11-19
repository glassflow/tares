/*
 * Browser e2e for decision folders (TR-1454) and the repo card fixes (TR-1463) on a factory cell.
 *  - Unit checks of the pure rules (a typed folder, the commit link, the Decisions line, the history words).
 *  - Connect a repo: ticking a repo asks for its folders once; what Tares found is ticked, with its file count
 *    and "found by Tares"; the big-repo and GitHub-did-not-answer lines; a typed folder joins the list ticked,
 *    a bad one is refused in words; Connect sends {repos, folders} with only the ticked folders.
 *  - A Ready card: the commit links to GitHub; "Updated ... ago, N files changed. N files." on its own line;
 *    "Decisions: 21 from docs/adr" links to the Decisions page for that repo, "from 3 folders", or "none" (and
 *    for an admin, to pick a folder). A read key sees History but no Decision folders.
 *  - Decision folders: the chosen folders with who and when, the ones found, one typed; Save sends the full
 *    list with PUT; a 409 says the repo is being removed, a 400 the cell's words; Cancel sends nothing.
 *  - History: the first index and each update, newest first, with commit links and a failed one's reason;
 *    empty; a failed read.
 *  - No sideways scroll at 390. The words keep the words list.
 * The brain's API is answered by the test; the daemon is real (tests/licensed.cjs).
 * Run:  npm run build && TARES_E2E_PORT=8896 node tests/brain-folders.e2e.cjs
 */
const fs = require("fs");
const http = require("http");
const path = require("path");
const vm = require("vm");

const UI_DIR = path.resolve(__dirname, "..");
const { chromium } = require("./browser.cjs");
const ts = require(path.join(UI_DIR, "node_modules", "typescript"));
const { checkWords } = require("./words.cjs");
const { startDaemon } = require("./licensed.cjs");

const ROOT = "root-token-brain-folders-e2e";
const PORT = Number(process.env.TARES_E2E_PORT || 8896);
const SHOTS = process.env.SHOTS_DIR;
if (SHOTS) fs.mkdirSync(SHOTS, { recursive: true });

let pass = 0, fail = 0;
const ck = (label, cond, detail = "") => {
  cond ? pass++ : fail++;
  console.log((cond ? "  ok   " : "  FAIL ") + label + (cond ? "" : `   ${detail}`));
};
const eq = (label, got, want) => ck(label, JSON.stringify(got) === JSON.stringify(want), `got ${JSON.stringify(got)}, want ${JSON.stringify(want)}`);

function call(base, method, url, body, headers = {}) {
  const data = body === undefined ? "" : JSON.stringify(body);
  return new Promise((resolve, reject) => {
    const r = http.request(base + url, { method, headers: { "content-type": "application/json", "content-length": Buffer.byteLength(data), ...headers } }, (res) => {
      let d = ""; res.on("data", (c) => (d += c));
      res.on("end", () => { let j = null; try { j = JSON.parse(d); } catch { /* not json */ } resolve({ status: res.statusCode, json: j }); });
    });
    r.on("error", reject); r.write(data); r.end();
  });
}

function loadTs(file, requires = {}) {
  const src = fs.readFileSync(path.join(UI_DIR, "src", file), "utf8");
  const js = ts.transpileModule(src, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 } }).outputText;
  const mod = { exports: {} };
  vm.runInNewContext(js, {
    module: mod, exports: mod.exports, Date, Number, Math, Object, JSON, Promise, RegExp, Array, String, Set, Intl, encodeURIComponent,
    require: (name) => { if (name in requires) return requires[name]; throw new Error(`unexpected import ${name}`); },
  });
  return mod.exports;
}
const timeMod = loadTs(path.join("lib", "time.ts"));
const numberMod = loadTs(path.join("lib", "number.ts"));
const F = loadTs(path.join("editions", "factory", "brainFoldersText.ts"), { "../../lib/time": timeMod, "../../lib/number": numberMod });

const NOW = Date.now();
const MIN = 60_000;
const iso = (ago) => new Date(NOW - ago).toISOString();
const SHA = "8c41d2e9a7b0c3d4e5f60718293a4b5c6d7e8f90";
const READY = (repo, o = {}) => ({
  repo, state: "ready", files_done: 1507, files_total: 1507, indexed_commit: SHA, last_synced: iso(22 * MIN), error: null, place: null,
  sync_failures: 0, sync_error: null, added_by: null, added_by_name: "Ada Lovelace", added_at: iso(300 * MIN), removing: false, remove_error: null,
  folders: [], decisions: 0, last_update: null, ...o,
});
const UPDATES = [
  { at: iso(22 * MIN), kind: "update", ok: true, indexed_commit: SHA, files_changed: 3, files_deleted: 1, files_total: 1507, error: null },
  { at: iso(95 * MIN), kind: "update", ok: false, indexed_commit: null, files_changed: 0, files_deleted: 0, files_total: 1508, error: "GitHub did not answer when the brain asked for the latest push." },
  { at: iso(300 * MIN), kind: "index", ok: true, indexed_commit: "1a2b3c4d5e6f708192a3b4c5d6e7f8091a2b3c4d", files_changed: 1508, files_deleted: 0, files_total: 1508, error: null },
];

function brainMock(rows, extra = {}) {
  const m = {
    rows, available: [], scans: {}, chosen: {}, updates: {}, posts: [], puts: [], folderReads: [], updateReads: [], putAnswer: null, updatesStatus: 200, ...extra,
  };
  m.handle = async (route) => {
    const req = route.request();
    const p = new URL(req.url()).pathname;
    const repoOf = (a, b) => `${decodeURIComponent(a)}/${decodeURIComponent(b)}`;
    if (req.method() === "GET" && p === "/api/brain") return route.fulfill({ status: 200, json: { configured: true, repos: m.rows } });
    if (req.method() === "GET" && p === "/api/brain/repos/available") return route.fulfill({ status: 200, json: { github: true, repos: m.available } });
    let x = p.match(/^\/api\/brain\/repos\/available\/([^/]+)\/([^/]+)\/folders$/);
    if (req.method() === "GET" && x) {
      const repo = repoOf(x[1], x[2]); m.folderReads.push(repo);
      return route.fulfill({ status: 200, json: { repo, folders: [], suggested: [], partial: false, scanned: true, scan_error: null, ...(m.scans[repo] || {}) } });
    }
    if (req.method() === "POST" && p === "/api/brain/repos") {
      const body = JSON.parse(req.postData() || "null"); m.posts.push(body);
      return route.fulfill({ status: 201, json: { repos: body.repos.map((repo) => READY(repo, { state: "queued" })) } });
    }
    x = p.match(/^\/api\/brain\/repos\/([^/]+)\/([^/]+)\/folders$/);
    if (x && req.method() === "GET") {
      const repo = repoOf(x[1], x[2]); m.folderReads.push(repo);
      return route.fulfill({ status: 200, json: { repo, folders: m.chosen[repo] || [], suggested: [], partial: false, scanned: true, scan_error: null, ...(m.scans[repo] || {}) } });
    }
    if (x && req.method() === "PUT") {
      const repo = repoOf(x[1], x[2]); const body = JSON.parse(req.postData() || "null"); m.puts.push({ repo, ...body });
      if (m.putAnswer) return route.fulfill(m.putAnswer);
      m.rows = m.rows.map((r) => (r.repo === repo ? { ...r, folders: body.folders } : r));
      return route.fulfill({ status: 200, json: { repo, folders: body.folders.map((path) => ({ path })), added: [], removed: [] } });
    }
    x = p.match(/^\/api\/brain\/repos\/([^/]+)\/([^/]+)\/updates$/);
    if (x && req.method() === "GET") {
      const repo = repoOf(x[1], x[2]); m.updateReads.push(repo);
      if (m.updatesStatus !== 200) return route.fulfill({ status: m.updatesStatus, json: { detail: "nope" } });
      return route.fulfill({ status: 200, json: { repo, updates: m.updates[repo] || [] } });
    }
    return route.fulfill({ status: 404, json: { detail: "unexpected" } });
  };
  return m;
}
const isBrainApi = (u) => u.pathname === "/api/brain" || u.pathname.startsWith("/api/brain/");

(async () => {
  // ---- the pure rules
  {
    eq("checkFolder: a plain path, slashes at the ends dropped", F.checkFolder(" /docs/adr/ ", []), { path: "docs/adr" });
    eq("checkFolder: refused in words", [F.checkFolder("", []), F.checkFolder("../x", []), F.checkFolder("a//b", []), F.checkFolder("a\\b", []), F.checkFolder("x".repeat(201), [])].map((r) => r.problem),
       ["Write a folder, like docs/adr.", "Write a folder inside the repo, like docs/adr.", "Write a folder inside the repo, like docs/adr.", "Write a folder inside the repo, like docs/adr.", "Keep the folder to 200 characters."]);
    eq("checkFolder: a repeat, and the 51st", [F.checkFolder("docs/adr", ["docs/adr"]).problem, F.checkFolder("z", Array.from({ length: 50 }, (_, i) => `f${i}`)).problem],
       ["docs/adr is already in the list.", "Choose at most 50 folders for a repo."]);
    eq("commitHref: a commit on GitHub, or nothing", [F.commitHref("acme/web", SHA), F.commitHref("acme/web", null), F.commitHref("javascript:x", SHA), F.commitHref("acme/web", "not-a-sha")],
       [`https://github.com/acme/web/commit/${SHA}`, "", "", ""]);
    eq("decisionsWhere: one folder, several, none", [F.decisionsWhere(["docs/adr"]), F.decisionsWhere(["a", "b", "c"]), F.decisionsWhere([])], ["docs/adr", "3 folders", ""]);
    eq("decisionsHref: the Decisions page for that repo, origin GitHub repos", F.decisionsHref("acme/web"), "/brain/decisions?where=repo_folder&repo=acme%2Fweb");
    eq("updateWords", UPDATES.map((u) => F.updateWords(u)), ["Update, 3 files changed, 1 deleted", "Update did not finish", "First index, 1,508 files"]);
    eq("filesWords and foundWords", [F.filesWords(1), F.filesWords(1200), F.filesWords(null), F.foundWords(12)], ["1 file", "1,200 files", "", "12 files, found by Tares"]);
    ck("the fixed words have no em dash", !Object.values(F).filter((v) => typeof v === "string").join(" ").includes("—"));
  }

  let d;
  try { d = await startDaemon({ port: PORT, edition: "factory", licence: "valid", seats: 50, auth: ROOT }); }
  catch (e) { console.log("FAIL daemon did not start: " + e.message); process.exit(1); }
  process.on("exit", () => { d.stop(); });
  const root = { authorization: `Bearer ${ROOT}` };
  const admin = await call(d.base, "POST", "/api/keys", { name: "ada-laptop", scopes: ["admin"], person: { email: "ada@example.com", name: "Ada Lovelace" } }, root);
  const reader = await call(d.base, "POST", "/api/keys", { name: "grace-laptop", scopes: ["read"], person: { email: "grace@example.com", name: "Grace Hopper" } }, root);
  ck("keys made", admin.status === 201 && reader.status === 201, `${admin.status} ${reader.status}`);

  const browser = await chromium.launch(process.env.TARES_E2E_CHROME ? { executablePath: process.env.TARES_E2E_CHROME } : {});
  const errors = [];
  const open = async ({ token = admin.json.secret, width = 1280, mock, url = "/brain", wait = "brain-repos" } = {}) => {
    const ctx = await browser.newContext({ viewport: { width, height: 900 } });
    await ctx.addInitScript((t) => { try { localStorage.setItem("tares_token", t); } catch { /* none */ } }, token);
    const page = await ctx.newPage();
    page.on("pageerror", (e) => errors.push(String(e)));
    await page.route(isBrainApi, mock.handle);
    await page.goto(d.base + url);
    await page.getByTestId(wait).waitFor({ timeout: 15000 });
    return { ctx, page, T: (id) => page.getByTestId(id) };
  };
  const text = async (loc) => (await loc.innerText()).replace(/\s+/g, " ").trim();
  const until = async (fn, ms = 5000) => { const end = Date.now() + ms; while (Date.now() < end) { if (await fn()) return true; await new Promise((r) => setTimeout(r, 50)); } return false; };
  const fits = async (page, label) => {
    const over = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    ck(`${label}: no sideways scroll`, over <= 0, String(over));
  };
  const shot = async (page, name) => { if (SHOTS) await page.screenshot({ path: path.join(SHOTS, name), fullPage: true }); };
  const card = (page, repo) => page.locator(`[data-testid="brain-card"][data-repo="${repo}"]`);

  try {
    // ---- Connect a repo: the folders under a ticked repo
    {
      const mock = brainMock([], {
        available: [{ repo: "acme/web", private: true, connected: false }, { repo: "acme/api", private: false, connected: false }, { repo: "acme/big", private: false, connected: false }, { repo: "acme/far", private: false, connected: false }],
        scans: {
          "acme/web": { suggested: [{ path: "docs/adr", files: 12, why: "known", chosen: false }, { path: "docs/design", files: 4, why: "numbered", chosen: false }] },
          "acme/big": { partial: true, suggested: [{ path: "adr", files: 3, why: "known", chosen: false }] },
          "acme/far": { scanned: false, scan_error: "GitHub did not answer in time." },
        },
      });
      const { ctx, page, T } = await open({ mock, url: "/brain/connect", wait: "brain-connect-rows" });
      ck("nothing is asked for before a repo is ticked", mock.folderReads.length === 0);
      const row = (repo) => page.locator(`[data-testid="brain-connect-row"][data-repo="${repo}"]`);
      await row("acme/web").getByRole("checkbox").first().click();
      const under = row("acme/web").getByTestId("brain-connect-folders");
      await under.getByTestId("brain-connect-folders-row").first().waitFor();
      eq("ticking asks for that repo's folders once", mock.folderReads, ["acme/web"]);
      eq("what Tares found shows, with the file count and who found it", (await under.getByTestId("brain-connect-folders-row").allInnerTexts()).map((t) => t.replace(/\s+/g, " ").trim()),
         ["docs/adr 12 files, found by Tares", "docs/design 4 files, found by Tares"]);
      ck("... and is ticked to start with", (await under.getByRole("checkbox", { checked: true }).count()) === 2);
      await under.getByRole("checkbox").nth(1).click();
      await under.getByTestId("brain-connect-folders-add").fill("../secrets");
      await under.getByTestId("brain-connect-folders-add-button").click();
      ck("a bad folder is refused in words", (await text(under)).includes("Write a folder inside the repo, like docs/adr."), await text(under));
      await under.getByTestId("brain-connect-folders-add").fill("docs/decisions/");
      await under.getByTestId("brain-connect-folders-add").press("Enter");
      await until(async () => (await under.getByTestId("brain-connect-folders-row").count()) === 3);
      eq("a typed folder joins the list, ticked", [await under.getByTestId("brain-connect-folders-row").nth(2).getAttribute("data-path"), await under.getByRole("checkbox", { checked: true }).count()], ["docs/decisions", 2]);
      await row("acme/web").getByRole("checkbox").first().click();   // untick and tick again: no second read, the choice kept
      await row("acme/web").getByRole("checkbox").first().click();
      ck("ticking again does not ask again, and keeps the choice", mock.folderReads.length === 1 && (await row("acme/web").getByTestId("brain-connect-folders").getByRole("checkbox", { checked: true }).count()) === 2);
      await row("acme/big").getByRole("checkbox").first().click();
      await row("acme/big").getByTestId("brain-connect-folders-partial").waitFor();
      eq("a big repo says only the usual names were checked", await text(row("acme/big").getByTestId("brain-connect-folders-partial")), "This repo is big, so Tares only checked the usual folder names.");
      await row("acme/far").getByRole("checkbox").first().click();
      await row("acme/far").getByTestId("brain-connect-folders-scan-error").waitFor();
      eq("GitHub did not answer: its words, and the add field still there", [await text(row("acme/far").getByTestId("brain-connect-folders-scan-error")), await row("acme/far").getByTestId("brain-connect-folders-add").count()],
         ["GitHub did not answer in time.", 1]);
      await row("acme/far").getByRole("checkbox").first().click();
      await row("acme/api").getByRole("checkbox").first().click();
      await row("acme/api").getByTestId("brain-connect-folders-none").waitFor();
      await checkWords(page, ck, "connect with folders");
      await shot(page, "connect-folders-1280.png");
      await T("brain-connect-submit").click();
      await until(() => mock.posts.length === 1);
      eq("Connect sends the repos and only the ticked folders of each", mock.posts[0], { repos: ["acme/web", "acme/api", "acme/big"], folders: { "acme/web": ["docs/adr", "docs/decisions"], "acme/big": ["adr"] } });
      await ctx.close();
    }
    {
      const mock = brainMock([], { available: [{ repo: "acme/web", private: true, connected: false }], scans: { "acme/web": { suggested: [{ path: "docs/adr", files: 12, why: "known", chosen: false }] } } });
      const { ctx, page } = await open({ mock, url: "/brain/connect", wait: "brain-connect-rows", width: 390 });
      await page.locator('[data-testid="brain-connect-row"]').first().getByRole("checkbox").first().click();
      await page.getByTestId("brain-connect-folders-row").first().waitFor();
      await fits(page, "connect with folders at 390");
      await shot(page, "connect-folders-390.png");
      await ctx.close();
    }

    // ---- a Ready card: commit link, the updated line, the Decisions line
    {
      const mock = brainMock([
        READY("acme/web", { folders: ["docs/adr"], decisions: 21, last_update: UPDATES[0] }),
        READY("acme/api", { folders: ["a", "b", "c"], decisions: 9 }),
        READY("acme/none"),
      ]);
      const { ctx, page } = await open({ mock });
      const web = card(page, "acme/web");
      eq("the commit links to GitHub", await web.getByTestId("brain-commit").getAttribute("href"), `https://github.com/acme/web/commit/${SHA}`);
      eq("the updated line: when, what the last update changed, the file count", await text(web.getByTestId("brain-updated")), "Updated 22 minutes ago, 3 files changed. 1,507 files. History");
      eq("Decisions: the count and the folder", await text(web.getByTestId("brain-decisions")), "Decisions: 21 from docs/adr");
      eq("the count opens the Decisions page for that repo", await web.getByTestId("brain-decisions-link").getAttribute("href"), "/brain/decisions?where=repo_folder&repo=acme%2Fweb");
      eq("several folders", await text(card(page, "acme/api").getByTestId("brain-decisions")), "Decisions: 9 from 3 folders");
      eq("no folders, for an admin", await text(card(page, "acme/none").getByTestId("brain-decisions")), "Decisions: none. Pick a folder if your decisions live in this repo.");
      ck("no grep line anywhere", !/grep/i.test(await text(page.locator("main"))));
      await checkWords(page, ck, "ready cards");
      await shot(page, "repos-1280.png");
      await ctx.close();
      const ro = await open({ mock, token: reader.json.secret });
      eq("a read key: none, without the admin sentence", await text(card(ro.page, "acme/none").getByTestId("brain-decisions")), "Decisions: none.");
      eq("a read key: History, no Decision folders", await card(ro.page, "acme/web").getByRole("button").allInnerTexts(), ["History"]);
      await ro.ctx.close();
      const narrow = await open({ mock, width: 390 });
      await fits(narrow.page, "repos at 390");
      await shot(narrow.page, "repos-390.png");
      await narrow.ctx.close();
    }

    // ---- Decision folders on a Ready card
    {
      const mock = brainMock([READY("acme/web", { folders: ["docs/adr"], decisions: 21 })], {
        chosen: { "acme/web": [{ path: "docs/adr", chosen_by: "p1", chosen_by_name: "Ada Lovelace", chosen_at: iso(3 * 24 * 60 * MIN), files: 21 }] },
        scans: { "acme/web": { suggested: [{ path: "docs/adr", files: 21, why: "known", chosen: true }, { path: "docs/design", files: 4, why: "numbered", chosen: false }] } },
      });
      const { ctx, page, T } = await open({ mock });
      await card(page, "acme/web").getByTestId("brain-folders").click();
      const dlg = T("brain-folders-dialog");
      await dlg.getByTestId("brain-folders-list-row").first().waitFor();
      eq("the chosen folder with who chose it and when, then the one found", (await dlg.getByTestId("brain-folders-list-row").allInnerTexts()).map((t) => t.replace(/\s+/g, " ").trim()),
         ["docs/adr 21 files, Chosen by Ada Lovelace 3 days ago", "docs/design 4 files, found by Tares"]);
      ck("the chosen one is ticked, the found one is not", (await dlg.getByRole("checkbox", { checked: true }).count()) === 1);
      await T("brain-folders-cancel").click();
      ck("Cancel sends nothing", mock.puts.length === 0 && (await T("brain-folders-dialog").count()) === 0);
      await card(page, "acme/web").getByTestId("brain-folders").click();
      await T("brain-folders-dialog").getByTestId("brain-folders-list-row").first().waitFor();
      await T("brain-folders-dialog").getByRole("checkbox").nth(1).click();
      await T("brain-folders-list-add").fill("rfcs");
      await T("brain-folders-list-add-button").click();
      await T("brain-folders-save").click();
      await until(() => mock.puts.length === 1);
      eq("Save sends the full new list", mock.puts[0], { repo: "acme/web", folders: ["docs/adr", "docs/design", "rfcs"] });
      await until(async () => (await T("brain-folders-dialog").count()) === 0);
      await until(async () => (await text(card(page, "acme/web").getByTestId("brain-decisions"))).includes("3 folders"));
      eq("the card reads again: from 3 folders", await text(card(page, "acme/web").getByTestId("brain-decisions")), "Decisions: 21 from 3 folders");
      mock.putAnswer = { status: 409, json: { detail: "acme/web is being removed" } };
      await card(page, "acme/web").getByTestId("brain-folders").click();
      await T("brain-folders-dialog").getByTestId("brain-folders-list-row").first().waitFor();
      await T("brain-folders-save").click();
      await T("brain-folders-failed").waitFor();
      eq("a 409: being removed", await text(T("brain-folders-failed")), "This repo is being removed.");
      mock.putAnswer = { status: 400, json: { detail: "rfcs is in the list twice" } };
      await T("brain-folders-save").click();
      await until(async () => (await text(T("brain-folders-failed"))) === "Rfcs is in the list twice.");
      eq("a 400: the cell's words", await text(T("brain-folders-failed")), "Rfcs is in the list twice.");
      await checkWords(page, ck, "folders dialog");
      await shot(page, "folders-dialog-1280.png");
      await ctx.close();
    }

    // ---- History
    {
      const mock = brainMock([READY("acme/web", { last_update: UPDATES[0] }), READY("acme/new")], { updates: { "acme/web": UPDATES } });
      const { ctx, page, T } = await open({ mock, token: reader.json.secret });
      await card(page, "acme/web").getByTestId("brain-history").click();
      await T("brain-history-list").waitFor();
      const rows = T("brain-history-row");
      eq("every entry, newest first", (await rows.allInnerTexts()).map((t) => t.split("\n")[0].trim()), ["Update, 3 files changed, 1 deleted", "Update did not finish", "First index, 1,508 files"]);
      eq("a commit links to GitHub", await rows.nth(0).locator("a").getAttribute("href"), `https://github.com/acme/web/commit/${SHA}`);
      ck("a failed one says why", (await text(rows.nth(1))).includes("GitHub did not answer when the brain asked for the latest push."), await text(rows.nth(1)));
      ck("anyone can read it: a read key did", mock.updateReads.includes("acme/web"));
      await checkWords(page, ck, "history");
      await shot(page, "history-1280.png");
      await T("brain-history-close").click();
      await card(page, "acme/new").getByTestId("brain-history").click();
      await T("brain-history-empty").waitFor();
      eq("nothing yet", await text(T("brain-history-empty")), "Nothing yet. The first index shows here once it ends.");
      await T("brain-history-close").click();
      mock.updatesStatus = 502;
      await card(page, "acme/web").getByTestId("brain-history").click();
      await T("brain-history-failed").waitFor();
      eq("a failed read", await text(T("brain-history-failed")), "The history did not load. Try again.");
      await ctx.close();
    }
  } catch (e) {
    ck("the suite ran to its end", false, e.stack || String(e));
  } finally {
    await browser.close();
  }
  ck("no page errors", errors.length === 0, errors.join(" | "));
  console.log(`${pass} passed, ${fail} failed`);
  process.exit(fail ? 1 : 0);
})();
