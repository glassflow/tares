/*
 * Browser e2e for the Second Brain Decisions page (TR-1444) on a factory cell.
 *  - Unit checks of the page's pure rules (tab labels, where a decision lives, who and when, the add and
 *    delete errors, links), run on the TypeScript as written.
 *  - Knowledge, Decisions opens /brain/decisions, not Not found. The page reads GET /api/brain/decisions once
 *    (expectOneCall) and never again while it sits idle.
 *  - Empty: the line says what fills the page. Filled: each row has its title (a link to GitHub in a new tab, a
 *    project doc in the console, none for one added by hand), where it lives, who decided and when; a project
 *    row without a kind never says "null". Three dropdowns, Origin (with the cell's counts), Repo and Project, ask
 *    with `where`, `repo` and `project`; a repo or project sets its origin, and Clear filters goes back.
 *  - Previous and Next move a page at a time with the cursor the cell gave, and say "1 to 50 of 238".
 *  - Record a decision (top right) opens /brain/decisions/new; it posts {title, text, decided_by} and goes back to
 *    the list with the new row at the top, and the counts are read again. A file
 *    over 5 MB is refused before anything is sent; a 400 shows the cell's sentence; a 501 says PDF files can't
 *    be read; a file is sent as base64 with its name.
 *  - Delete shows only where can_delete is true, asks first, then the row leaves the list.
 *  - A read-only key: no Record a decision, no Delete; the form's address says why. No brain (404): "Not set up". A failed read:
 *    a banner with Try again. `?repo=owner/name` asks for that repo and says so, with a way back.
 *  - No sideways scroll at 390, and the page's words keep the words list.
 * The brain's API is answered by the test; the daemon is real (tests/licensed.cjs), so the console, the keys,
 * the people and the shell are real.
 * Run:  npm run build && TARES_E2E_PORT=8890 node tests/brain-decisions.e2e.cjs
 */
const fs = require("fs");
const http = require("http");
const path = require("path");
const vm = require("vm");

const UI_DIR = path.resolve(__dirname, "..");
const { chromium } = require("./browser.cjs");
const ts = require(path.join(UI_DIR, "node_modules", "typescript"));
const { checkWords, expectOneCall } = require("./words.cjs");
const { startDaemon } = require("./licensed.cjs");

const ROOT = "root-token-brain-decisions-e2e";
const PORT = Number(process.env.TARES_E2E_PORT || 8890);
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
    const r = http.request(base + url, {
      method,
      headers: { "content-type": "application/json", "content-length": Buffer.byteLength(data), ...headers },
    }, (res) => {
      let d = ""; res.on("data", (c) => (d += c));
      res.on("end", () => { let j = null; try { j = JSON.parse(d); } catch { /* not json */ } resolve({ status: res.statusCode, json: j }); });
    });
    r.on("error", reject); r.write(data); r.end();
  });
}

// ---- the TypeScript as written, loaded without a browser
function loadTs(file, requires = {}) {
  const src = fs.readFileSync(path.join(UI_DIR, "src", file), "utf8");
  const js = ts.transpileModule(src, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 } }).outputText;
  const mod = { exports: {} };
  vm.runInNewContext(js, {
    module: mod, exports: mod.exports, Date, Number, Math, Object, JSON, Promise, RegExp, Array, String, Set, Intl,
    require: (name) => { if (name in requires) return requires[name]; throw new Error(`unexpected import ${name}`); },
  });
  return mod.exports;
}
const timeMod = loadTs(path.join("lib", "time.ts"));
const numberMod = loadTs(path.join("lib", "number.ts"));
const T = loadTs(path.join("editions", "factory", "brainDecisionsText.ts"), { "../../lib/time": timeMod, "../../lib/number": numberMod });

// ---- decisions, as GET /api/brain/decisions gives them
const NOW = Date.now();
const iso = (daysAgo) => new Date(NOW - daysAgo * 86_400_000).toISOString();
const day = (daysAgo) => iso(daysAgo).slice(0, 10);
const base = {
  summary: null, where: null, decided_by: [], changed_by: [], decided_at: null, changed_at: null, link: null, repo: null,
  project: null, project_name: null, doc_kind: null, added_by: null, can_delete: false, source: "internal",
};
const D = (o) => ({ ...base, ...o });
const SPEC = D({ id: "project:p1:doc:d1", title: "Code index runs as its own service", where: "factory_project", project: "p1", project_name: "software-second-brain", doc_kind: "spec", decided_by: ["Ada Lovelace"], decided_at: day(0), link: "/projects/p1/docs?doc=d1" });
const OLD_PLAN = D({ id: "project:p2:doc:d2", title: "Cloud cells keep state in Postgres", where: "factory_project", project: "p2", project_name: "state-store", doc_kind: null, decided_by: ["Ada Lovelace"], decided_at: day(14), link: "/projects/p2/docs?doc=d2" });
const HAND = D({ id: "hand:11111111-1111-4111-8111-111111111111", title: "Enterprise code lives in ee/", summary: "Source-visible in the public repo under its own licence.", where: "by_hand", decided_by: ["Ada Lovelace"], added_by: "Ada Lovelace", decided_at: iso(8), can_delete: true });
const HAND_OTHER = D({ id: "hand:22222222-2222-4222-8222-222222222222", title: "Ship on Tuesdays", summary: "Fewer weekend pages.", where: "by_hand", decided_by: ["Grace Hopper"], added_by: "Grace Hopper", decided_at: iso(3), can_delete: false });
const ADR = D({ id: "glassflow/tares-cloud:docs/adr/0007-alembic.md", title: "0007 Control plane migrations use Alembic", where: "repo_folder", repo: "glassflow/tares-cloud", decided_by: ["Ada Lovelace"], changed_by: ["Kiran"], decided_at: iso(120), changed_at: day(90), link: "https://github.com/glassflow/tares-cloud/blob/HEAD/docs/adr/0007-alembic.md" });
const ROWS = [SPEC, HAND, HAND_OTHER, OLD_PLAN, ADR];
const COUNTS = { all: 5, repo_folder: 1, factory_project: 2, by_hand: 2 };

// ---- the brain API, answered here
function brainMock(extra = {}) {
  const m = {
    rows: ROWS.slice(), counts: { ...COUNTS }, filters: { repos: ["glassflow/tares-cloud"], projects: [{ id: "p1", name: "software-second-brain" }, { id: "p2", name: "state-store" }] }, status: 200, pageSize: 50, reads: [], posts: [], deletes: [], post: null, del: null, ...extra,
  };
  m.handle = async (route) => {
    const req = route.request();
    const u = new URL(req.url());
    const p = u.pathname;
    if (req.method() === "GET" && p === "/api/brain") {
      return route.fulfill({ status: 200, json: { configured: true, repos: [{ repo: "glassflow/tares-cloud", state: "ready", files_done: 1, files_total: 1, removing: false }] } });
    }
    if (req.method() === "GET" && p === "/api/brain/decisions") {
      const q = Object.fromEntries(u.searchParams);
      m.reads.push(q);
      if (m.status !== 200) return route.fulfill({ status: m.status, json: { detail: m.status === 404 ? "Not Found" : "The brain did not answer." } });
      let rows = m.rows.filter((r) => (!q.where || r.where === q.where) && (!q.repo || (r.repo || "").toLowerCase() === q.repo.toLowerCase())
        && (!q.project || (r.where === "factory_project" && r.project === q.project)));
      const start = q.cursor ? Number(q.cursor) : 0;
      const page = rows.slice(start, start + m.pageSize);
      const next = start + m.pageSize < rows.length ? String(start + m.pageSize) : null;
      return route.fulfill({ status: 200, json: { decisions: page, next_cursor: next, counts: m.counts, filters: m.filters } });
    }
    if (req.method() === "POST" && p === "/api/brain/decisions") {
      const body = JSON.parse(req.postData() || "{}");
      m.posts.push(body);
      if (m.post) return route.fulfill(await m.post(body, m));
      const row = D({ id: "hand:33333333-3333-4333-8333-333333333333", title: body.title, summary: (body.text || "").split(".")[0] + ".", where: "by_hand", decided_by: ["Grace Hopper"], added_by: "Ada Lovelace", decided_at: new Date().toISOString(), can_delete: true });
      m.rows.unshift(row);
      m.counts = { ...m.counts, all: m.counts.all + 1, by_hand: m.counts.by_hand + 1 };
      return route.fulfill({ status: 201, json: row });
    }
    if (req.method() === "DELETE" && p.startsWith("/api/brain/decisions/")) {
      const id = decodeURIComponent(p.slice("/api/brain/decisions/".length));
      m.deletes.push(id);
      if (m.del) return route.fulfill(await m.del(id, m));
      m.rows = m.rows.filter((r) => r.id !== id);
      m.counts = { ...m.counts, all: m.counts.all - 1, by_hand: m.counts.by_hand - 1 };
      return route.fulfill({ status: 200, json: { ok: true, removed: id } });
    }
    return route.fulfill({ status: 404, json: { detail: "unexpected" } });
  };
  return m;
}
const isBrainApi = (u) => u.pathname === "/api/brain" || u.pathname.startsWith("/api/brain/");

(async () => {
  // ---- 0. the pure rules
  {
    eq("tabOf: known origins, else All", [T.tabOf("by_hand"), T.tabOf("repo_folder"), T.tabOf("nope"), T.tabOf(null)], ["by_hand", "repo_folder", "all", "all"]);
    eq("originLabel: with the cell's counts", [T.originLabel("all", COUNTS), T.originLabel("repo_folder", COUNTS), T.originLabel("by_hand", { ...COUNTS, by_hand: 1200 })], ["All (5)", "GitHub repos (1)", "Second Brain (1,200)"]);
    eq("originLabel: no counts yet", [T.originLabel("factory_project", null), T.originLabel("all", undefined)], ["Factory projects", "All"]);
    eq("emptyWords: one project", T.emptyWords("factory_project", null, true, "p1"), "No decisions for this project yet.");
    eq("joinNames", [T.joinNames([]), T.joinNames(["Ana"]), T.joinNames(["Ana", "Kiran"]), T.joinNames(["Ana", " ", "Kiran", "Lee"])], ["", "Ana", "Ana and Kiran", "Ana, Kiran and Lee"]);
    eq("whoParts: decided, and updated by someone else", [T.whoParts(ADR), T.whoParts(SPEC), T.whoParts({ decided_by: ["A"], changed_by: ["A"], added_by: null })],
       [{ decided: "Ada Lovelace", updated: "Updated by Kiran" }, { decided: "Ada Lovelace", updated: "" }, { decided: "A", updated: "" }]);
    eq("whoParts: falls back to who added it, or says only who updated it", [T.whoParts({ decided_by: [], changed_by: [], added_by: "Lee" }), T.whoParts({ decided_by: [], changed_by: ["Kiran"], added_by: null })],
       [{ decided: "Lee", updated: "" }, { decided: "", updated: "Updated by Kiran" }]);
    eq("timeOf: a bare day is midnight UTC", T.timeOf("2026-11-12"), Date.parse("2026-11-12T00:00:00Z"));
    ck("timeOf: none is NaN", Number.isNaN(T.timeOf(null)) && Number.isNaN(T.timeOf("")));
    eq("whenWords: a bare day today says today", T.whenWords({ decided_at: day(0), changed_at: null }, NOW), "today");
    eq("whenWords: the last change wins", T.whenWords(ADR, NOW), "90 days ago");
    eq("whenWords: none", T.whenWords({ decided_at: null, changed_at: null }, NOW), "");
    eq("originOf: a GitHub repo, linked to the repo", T.originOf(ADR), { label: "GitHub repo", name: "glassflow/tares-cloud", href: "https://github.com/glassflow/tares-cloud" });
    eq("originOf: a factory project, linked to the project", T.originOf(SPEC), { label: "Factory project", name: "software-second-brain", href: "/projects/p1" });
    eq("originOf: Second Brain, and none", [T.originOf(HAND), T.originOf(D({ id: "x" }))], [{ label: "Second Brain" }, null]);
    eq("repoHref: only owner/name", [T.repoHref("a/b"), T.repoHref("a/b/c"), T.repoHref("javascript:x"), T.repoHref(null)], ["https://github.com/a/b", "", "", ""]);
    eq("safeLink: http, https, the cell; nothing else", [T.safeLink(ADR.link), T.safeLink("/projects/p1/docs"), T.safeLink("javascript:alert(1)"), T.safeLink("//evil.example"), T.safeLink("/\\evil"), T.safeLink(null)],
       [ADR.link, "/projects/p1/docs", "", "", "", ""]);
    eq("titleOf", [T.titleOf({ title: " A " }), T.titleOf({ title: null })], ["A", "Untitled"]);
    eq("formProblem", [
      T.formProblem({ title: " ", text: "x", file: null }), T.formProblem({ title: "x".repeat(201), text: "x", file: null }),
      T.formProblem({ title: "t", text: "  ", file: null }), T.formProblem({ title: "t", text: "x", file: null }),
      T.formProblem({ title: "t", text: "", file: { name: "a.docx", size: 10 } }), T.formProblem({ title: "t", text: "", file: { name: "a.PDF", size: 6 * 1024 * 1024 } }),
      T.formProblem({ title: "t", text: "", file: { name: "a.md", size: 0 } }), T.formProblem({ title: "t", text: "", file: { name: "a.markdown", size: 10 } }),
    ], ["Give the decision a title.", "Keep the title to 200 characters.", "Paste the decision, or upload a file.", "", "Upload a Markdown, text or PDF file.",
        "That file is over 5 MB. Upload a smaller one, or paste the text.", "That file is empty.", ""]);
    eq("addErrorWords", [T.addErrorWords({ status: 400, message: "decided_by: nobody on the cell is called Zed" }), T.addErrorWords({ status: 413 }), T.addErrorWords({ status: 501 }), T.addErrorWords({ status: 502 }), T.addErrorWords({ status: 403 }), T.addErrorWords(null)],
       ["Decided_by: nobody on the cell is called Zed.", T.TOO_BIG_WORDS, "This cell can't read PDF files yet. Paste the text or upload a Markdown file.", "Couldn't save it. Try again.", T.NO_WRITE_WORDS, "Couldn't save it. Try again."]);
    eq("rangeWords", [T.rangeWords(1, 50, 238), T.rangeWords(51, 100, 238), T.rangeWords(5, 5, 5), T.rangeWords(51, 100, null), T.rangeWords(1, 1200, 1500), T.rangeWords(3, 2, 9)],
       ["1 to 50 of 238", "51 to 100 of 238", "5 of 5", "51 to 100", "1 to 1,200 of 1,500", ""]);
    eq("deleteErrorWords", [T.deleteErrorWords({ status: 404 }), T.deleteErrorWords({ status: 403 }), T.deleteErrorWords({ status: 502 })], [T.ALREADY_GONE_WORDS, T.NOT_YOURS_WORDS, T.DELETE_FAILED_WORDS]);
    eq("emptyWords: the whole cell, a tab, a repo", [T.emptyWords("all", null, true), T.emptyWords("repo_folder", null, true), T.emptyWords("by_hand", "a/b", true)],
       [T.EMPTY_ALL, "Pick a decisions folder on a repo's card to see its files here.", "No decisions for this repo yet."]);
    ck("emptyWords: a read-only key is not pointed at the form", !/below/.test(T.emptyWords("all", null, false)));
    const copy = Object.values(T).filter((v) => typeof v === "string").join(" ");
    ck("the page's fixed words have no em dash", !copy.includes("—"));
  }

  // ---- the daemon: licensed, with an admin key and a read-only key, each a person
  let d;
  try {
    d = await startDaemon({ port: PORT, edition: "factory", licence: "valid", seats: 50, auth: ROOT });
  } catch (e) { console.log("FAIL daemon did not start: " + e.message); process.exit(1); }
  process.on("exit", () => { d.stop(); });
  const root = { authorization: `Bearer ${ROOT}` };
  const admin = await call(d.base, "POST", "/api/keys", { name: "ada-laptop", scopes: ["admin"], person: { email: "ada@example.com", name: "Ada Lovelace" } }, root);
  const reader = await call(d.base, "POST", "/api/keys", { name: "grace-laptop", scopes: ["read"], person: { email: "grace@example.com", name: "Grace Hopper" } }, root);
  ck("keys made", admin.status === 201 && reader.status === 201, `${admin.status} ${reader.status}`);
  const people = (await call(d.base, "GET", "/api/people", undefined, root)).json.people;
  const grace = people.find((p) => p.name === "Grace Hopper");
  const ada = people.find((p) => p.name === "Ada Lovelace");
  ck("people known", Boolean(grace && ada), JSON.stringify(people));

  const browser = await chromium.launch(process.env.TARES_E2E_CHROME ? { executablePath: process.env.TARES_E2E_CHROME } : {});
  const errors = [];
  const T_ = (page) => (id) => page.getByTestId(id);
  const open = async (opts = {}) => {
    const { token = admin.json.secret, width = 1280, mock = brainMock(), url = "/brain/decisions", wait = "brain-decision-row", one: countOne = true, label = "the page's read" } = opts;
    const ctx = await browser.newContext({ viewport: { width, height: 900 } });
    await ctx.addInitScript((t) => { try { if (t) localStorage.setItem("tares_token", t); } catch { /* none */ } }, token);
    const page = await ctx.newPage();
    page.on("pageerror", (e) => errors.push(String(e)));
    await page.route(isBrainApi, mock.handle);
    const one = expectOneCall(page, "/api/brain/decisions");
    await page.goto(d.base + url);
    if (wait) await page.getByTestId(wait).first().waitFor({ timeout: 15000 });
    if (wait && countOne) await page.waitForLoadState("networkidle");
    return { ctx, page, mock, one, T: T_(page) };
  };
  const text = async (loc) => (await loc.innerText()).replace(/\s+/g, " ").trim();
  const lines = async (loc) => (await loc.innerText()).split("\n").map((l) => l.trim()).filter(Boolean);
  const shot = async (page, name) => { if (SHOTS) await page.screenshot({ path: path.join(SHOTS, name), fullPage: true }); };
  const fits = async (page, label) => {
    const over = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    ck(`${label}: no sideways scroll`, over <= 0, String(over));
  };
  const until = async (fn, ms = 5000) => { const end = Date.now() + ms; while (Date.now() < end) { if (await fn()) return true; await new Promise((r) => setTimeout(r, 50)); } return false; };

  try {
    // ---- the menu row opens the page; empty
    {
      const mock = brainMock({ rows: [], counts: { all: 0, repo_folder: 0, factory_project: 0, by_hand: 0 } });
      const { ctx, page, T } = await open({ mock, url: "/projects", wait: null });
      await T("menu-brain-decisions").waitFor({ timeout: 15000 });
      await page.waitForLoadState("networkidle");   // the Projects page has finished its own reads
      const oneRead = expectOneCall(page, "/api/brain/decisions");
      await T("menu-brain-decisions").click();
      await T("brain-decisions-empty").waitFor({ timeout: 15000 });
      ck("Knowledge, Decisions opens /brain/decisions", new URL(page.url()).pathname === "/brain/decisions", page.url());
      eq("the heading", await text(page.locator("h1")), "Decisions");
      ck("the line under it", (await text(T("brain-decisions"))).includes("Why things are the way they are, and who decided. The crew reads these before it changes the code they describe."));
      eq("the empty line says what fills the page", await text(T("brain-decisions-empty")), "No decisions yet. They show here from the decisions folders you pick in GitHub repos, from specs and plans in shared factory projects, and from ones you record here.");
      ck("an admin sees Record a decision", (await T("brain-decision-record").count()) === 1);
      await page.waitForLoadState("networkidle");
      await oneRead(ck, "from the menu");
      await checkWords(page, ck, "empty");
      await ctx.close();
    }

    // ---- filled
    {
      const { ctx, page, mock, T, one } = await open();
      await one(ck, "filled at 1280");
      eq("one read, no filter, 25 to a page", mock.reads, [{ limit: "25" }]);
      eq("every row shows", await T("brain-decision-row").count(), 5);
      ck("no tabs", (await page.getByRole("tab").count()) === 0);
      eq("the three dropdowns, all off", [await text(T("brain-decisions-origin")), await text(T("brain-decisions-repo")), await text(T("brain-decisions-project"))], ["All (5)", "All repos", "All projects"]);
      await T("brain-decisions-origin").click();
      eq("Origin offers every origin with the cell's counts", (await page.getByRole("option").allInnerTexts()).map((t) => t.trim()), ["All (5)", "Factory projects (2)", "GitHub repos (1)", "Second Brain (2)"]);
      await page.keyboard.press("Escape");
      await T("brain-decisions-project").click();
      eq("Project offers the projects with decisions", (await page.getByRole("option").allInnerTexts()).map((t) => t.trim()), ["All projects", "software-second-brain", "state-store"]);
      await page.keyboard.press("Escape");
      ck("no Clear filters while none is on", (await T("brain-decisions-clear").count()) === 0);
      const spec = T("brain-decision-row").nth(0);
      eq("a project doc: its origin, the name on a second line", await lines(spec.getByTestId("brain-decision-origin")), ["Factory project", "software-second-brain"]);
      eq("the project name opens the project", await spec.getByTestId("brain-decision-origin-link").getAttribute("href"), "/projects/p1");
      eq("a spec: who and when", [await text(spec.getByTestId("brain-decision-who")), await text(spec.getByTestId("brain-decision-when"))], ["Ada Lovelace", "today"]);
      eq("a spec's title opens the project doc in the console", await spec.getByTestId("brain-decision-title").getAttribute("href"), "/projects/p1/docs?doc=d1");
      const hand = T("brain-decision-row").nth(1);
      eq("by hand: its origin is Second Brain, and its summary shows", [await text(hand.getByTestId("brain-decision-origin")), await text(hand.getByTestId("brain-decision-summary"))], ["Second Brain", HAND.summary]);
      ck("Second Brain: the title is not a link", (await hand.locator("a").count()) === 0);
      ck("Delete shows where the row allows it", (await hand.getByTestId("brain-decision-delete").count()) === 1);
      ck("Delete does not show on someone else's row", (await T("brain-decision-row").nth(2).getByTestId("brain-decision-delete").count()) === 0);
      const old = T("brain-decision-row").nth(3);
      eq("another project's origin", await text(old.getByTestId("brain-decision-origin")), "Factory project state-store");
      ck("the page never says null", !/\bnull\b/.test(await text(T("brain-decisions"))));
      const adr = T("brain-decision-row").nth(4);
      eq("a repo file: its origin, the repo on a second line", await lines(adr.getByTestId("brain-decision-origin")), ["GitHub repo", "glassflow/tares-cloud"]);
      ck("the repo name opens the repo on GitHub in a new tab", (await adr.getByTestId("brain-decision-origin-link").getAttribute("href")) === "https://github.com/glassflow/tares-cloud" && (await adr.getByTestId("brain-decision-origin-link").getAttribute("target")) === "_blank");
      eq("a repo file: who decided, then who updated it on the next line", [await text(adr.getByTestId("brain-decision-who")), await text(adr.getByTestId("brain-decision-updated"))], ["Ada Lovelace", "Updated by Kiran"]);
      const a = adr.getByTestId("brain-decision-title");
      ck("a repo file's title opens GitHub in a new tab", (await a.getAttribute("href")) === ADR.link && (await a.getAttribute("target")) === "_blank");
      await checkWords(page, ck, "filled");
      await shot(page, "decisions-1280.png");

      // idle: no read by itself
      await new Promise((r) => setTimeout(r, 3000));
      eq("idle for 3 seconds: no more reads", mock.reads.length, 1);

      // Origin asks with where
      const pickOption = async (id, name) => { await T(id).click(); await page.getByRole("option", { name }).click(); };
      await pickOption("brain-decisions-origin", /^Second Brain/);
      await until(() => mock.reads.length === 2);
      await until(async () => (await T("brain-decision-row").count()) === 2);
      eq("Origin Second Brain asks with where=by_hand", mock.reads[1], { where: "by_hand", limit: "25" });
      eq("it shows only those", await T("brain-decision-row").count(), 2);
      ck("the origin is in the address", new URL(page.url()).searchParams.get("where") === "by_hand", page.url());
      // a project sets its origin
      await pickOption("brain-decisions-project", "state-store");
      await until(() => mock.reads.length === 3);
      eq("a project asks with project and its origin", mock.reads[2], { where: "factory_project", project: "p2", limit: "25" });
      await until(async () => (await T("brain-decision-row").count()) === 1);
      eq("only that project's decisions", await text(T("brain-decision-row").first().getByTestId("brain-decision-title")), OLD_PLAN.title);
      eq("Origin follows: Factory projects", await text(T("brain-decisions-origin")), "Factory projects (2)");
      // a repo sets its origin and clears the project
      await pickOption("brain-decisions-repo", "glassflow/tares-cloud");
      await until(() => mock.reads.length === 4);
      eq("a repo asks with repo and its origin, and the project goes", mock.reads[3], { where: "repo_folder", repo: "glassflow/tares-cloud", limit: "25" });
      eq("Project is back to all", await text(T("brain-decisions-project")), "All projects");
      // an origin that rules the repo out clears it
      await pickOption("brain-decisions-origin", /^Second Brain/);
      await until(() => mock.reads.length === 5);
      eq("Second Brain clears the repo", mock.reads[4], { where: "by_hand", limit: "25" });
      await T("brain-decisions-clear").click();
      await until(() => mock.reads.length === 6);
      eq("Clear filters goes back to everything", mock.reads[5], { limit: "25" });
      ck("and the address has no filter", new URL(page.url()).search === "", page.url());
      await ctx.close();
    }

    // ---- pages: Previous and Next, with where the page is
    {
      const mock = brainMock({ pageSize: 2 });
      const { ctx, T } = await open({ mock });
      eq("a page of two", await T("brain-decision-row").count(), 2);
      eq("the first page says where it is", await text(T("brain-decisions-range")), "1 to 2 of 5");
      ck("Previous is off on the first page", await T("brain-decisions-prev").isDisabled());
      await T("brain-decisions-next").click();
      await until(async () => (await text(T("brain-decisions-range"))) === "3 to 4 of 5");
      eq("the second page says where it is", await text(T("brain-decisions-range")), "3 to 4 of 5");
      eq("Next asks with the cursor", mock.reads[1], { cursor: "2", limit: "25" });
      eq("and shows only the next page", (await T("brain-decision-row").allInnerTexts()).map((t) => t.split("\n")[0]), [HAND_OTHER.title, OLD_PLAN.title]);
      await T("brain-decisions-next").click();
      await until(async () => (await text(T("brain-decisions-range"))) === "5 of 5");
      eq("the last page says where it is", await text(T("brain-decisions-range")), "5 of 5");
      ck("Next is off on the last page", await T("brain-decisions-next").isDisabled());
      await T("brain-decisions-prev").click();
      await until(async () => (await text(T("brain-decisions-range"))) === "3 to 4 of 5");
      eq("Previous goes back with the cursor it had", mock.reads[mock.reads.length - 1], { cursor: "2", limit: "25" });
      await T("brain-decisions-prev").click();
      await until(async () => (await text(T("brain-decisions-range"))) === "1 to 2 of 5");
      eq("back on the first page, it says so", await text(T("brain-decisions-range")), "1 to 2 of 5");
      eq("back on the first page, no cursor", mock.reads[mock.reads.length - 1], { limit: "25" });
      await ctx.close();
      const one = await open();
      ck("one page only: no page bar", (await one.T("brain-decisions-pages").count()) === 0);
      await one.ctx.close();
    }

    // ---- record a decision: the button opens /brain/decisions/new, the form sends, the list has it at the top
    {
      const { ctx, page, mock, T } = await open();
      ck("the list page has no form", (await T("brain-decision-add").count()) === 0);
      eq("Record a decision is at the top right", await text(T("brain-decision-record")), "Record a decision");
      await T("brain-decision-record").click();
      await T("brain-decision-add-title").waitFor();
      ck("it opens /brain/decisions/new", new URL(page.url()).pathname === "/brain/decisions/new", page.url());
      eq("the form's heading", await text(page.locator("h1")), "Record a decision");
      ck("the menu still marks Decisions", (await T("menu-brain-decisions").getAttribute("aria-current")) === "page");
      const who = T("brain-decision-add-who");
      await until(async () => (await text(who)) === "Ada Lovelace (you)");
      eq("Decided by starts with you", await text(who), "Ada Lovelace (you)");
      await who.click();
      await page.getByRole("option", { name: "Grace Hopper" }).click();
      await T("brain-decision-add-title").fill("Use Postgres for the state store");
      await T("brain-decision-add-text").fill("We chose Postgres. It is what the cloud runs.");
      const readsBefore = mock.reads.length;
      await T("brain-decision-add-submit").click();
      await until(() => mock.posts.length === 1 && new URL(page.url()).pathname === "/brain/decisions");
      eq("it posts the title, the text and who decided", mock.posts[0], { title: "Use Postgres for the state store", text: "We chose Postgres. It is what the cloud runs.", decided_by: grace.id });
      ck("then it goes back to Decisions", new URL(page.url()).pathname === "/brain/decisions", page.url());
      await until(async () => (await T("brain-decision-row").count()) > 0 && (await text(T("brain-decision-row").first())).includes("Use Postgres"));
      const top = T("brain-decision-row").first();
      ck("the new row is at the top, from Second Brain, decided by that person",
         (await text(top)).includes("Use Postgres for the state store") && (await text(top.getByTestId("brain-decision-origin"))) === "Second Brain"
         && (await text(top.getByTestId("brain-decision-who"))) === "Grace Hopper", await text(top));
      await until(async () => (await text(T("brain-decisions-origin"))) === "All (6)");
      eq("the count went up", await text(T("brain-decisions-origin")), "All (6)");
      ck("the list was read again", mock.reads.length > readsBefore);
      await page.reload();
      await T("brain-decision-row").first().waitFor();
      ck("a reload does not add the row twice", (await T("brain-decision-row").filter({ hasText: "Use Postgres" }).count()) === 1);

      // Cancel goes back without sending
      await T("brain-decision-record").click();
      await T("brain-decision-add-cancel").click();
      await T("brain-decision-row").first().waitFor();
      ck("Cancel goes back to Decisions, nothing sent", new URL(page.url()).pathname === "/brain/decisions" && mock.posts.length === 1);
      await ctx.close();
    }

    // ---- the form's errors
    {
      const { ctx, page, mock, T } = await open({ url: "/brain/decisions/new", wait: "brain-decision-add-title" });
      // a file over 5 MB is refused before anything is sent
      await T("brain-decision-add-title").fill("A big one");
      await T("brain-decision-add-file").setInputFiles({ name: "big.pdf", mimeType: "application/pdf", buffer: Buffer.alloc(6 * 1024 * 1024, 1) });
      await T("brain-decision-add-error").waitFor();
      eq("a 6 MB file: plain words", await text(T("brain-decision-add-error")), "That file is over 5 MB. Upload a smaller one, or paste the text.");
      await T("brain-decision-add-submit").click();
      ck("nothing was sent", mock.posts.length === 0, String(mock.posts.length));
      await T("brain-decision-add-file-clear").click();

      mock.post = async () => ({ status: 501, json: { detail: "PDF support is not installed" } });
      await T("brain-decision-add-title").fill("From a PDF");
      await T("brain-decision-add-file").setInputFiles({ name: "a.pdf", mimeType: "application/pdf", buffer: Buffer.from("%PDF-1.4 x") });
      await T("brain-decision-add-submit").click();
      await until(async () => (await T("brain-decision-add-error").count()) === 1 && (await text(T("brain-decision-add-error"))).startsWith("This cell"));
      eq("a 501: this cell can't read PDF files", await text(T("brain-decision-add-error")), "This cell can't read PDF files yet. Paste the text or upload a Markdown file.");
      await T("brain-decision-add-file-clear").click();
      mock.post = async () => ({ status: 400, json: { detail: "decided_by: nobody on the cell is called that" } });
      await T("brain-decision-add-text").fill("Some text.");
      await T("brain-decision-add-submit").click();
      await until(async () => (await T("brain-decision-add-error").count()) === 1 && (await text(T("brain-decision-add-error"))).startsWith("Decided_by"));
      eq("a 400: the cell's own sentence", await text(T("brain-decision-add-error")), "Decided_by: nobody on the cell is called that.");
      mock.post = async () => ({ status: 502, json: { detail: "lore said no" } });
      await T("brain-decision-add-submit").click();
      await until(async () => (await text(T("brain-decision-add-error"))) === "Couldn't save it. Try again.");
      eq("a 502: try again", await text(T("brain-decision-add-error")), "Couldn't save it. Try again.");
      ck("an error keeps the person on the form", new URL(page.url()).pathname === "/brain/decisions/new");
      await checkWords(page, ck, "form errors");

      // a Markdown file is sent as base64 with its name
      mock.post = null;
      await T("brain-decision-add-text").fill("");
      await T("brain-decision-add-file").setInputFiles({ name: "adr-7.md", mimeType: "text/markdown", buffer: Buffer.from("# ADR 7\n\nWe chose it.\n") });
      ck("the file's name shows, and the text box is off", (await text(T("brain-decision-add-file-name"))).includes("adr-7.md") && await T("brain-decision-add-text").isDisabled());
      await T("brain-decision-add-submit").click();
      await until(() => mock.posts.filter((b) => b.file && b.file.name === "adr-7.md").length === 1);
      const sent = mock.posts.find((b) => b.file && b.file.name === "adr-7.md");
      ck("a file goes as {name, content_base64}, no text",
         Buffer.from(sent.file.content_base64, "base64").toString() === "# ADR 7\n\nWe chose it.\n" && !("text" in sent), JSON.stringify(sent).slice(0, 200));
      await shot(page, "decision-new-1280.png");
      await ctx.close();
    }

    // ---- delete
    {
      const { ctx, page, mock, T } = await open();
      await T("brain-decision-row").nth(1).getByTestId("brain-decision-delete").click();
      await T("brain-decision-delete-dialog").waitFor();
      ck("a confirm shows first, naming the decision", (await text(T("brain-decision-delete-dialog"))).includes("Enterprise code lives in ee/") && mock.deletes.length === 0);
      await T("brain-decision-delete-confirm").click();
      await until(() => mock.deletes.length === 1 && mock.reads.length === 2);
      eq("it deletes that id", mock.deletes, [HAND.id]);
      await until(async () => (await T("brain-decision-row").count()) === 4);
      ck("the row leaves the list", !(await text(T("brain-decisions-list"))).includes("Enterprise code lives in ee/"));
      await until(async () => (await text(T("brain-decisions-origin"))) === "All (4)");
      eq("the counts drop", await text(T("brain-decisions-origin")), "All (4)");
      ck("the dialog closed", (await T("brain-decision-delete-dialog").count()) === 0);
      await ctx.close();
    }

    // ---- a read-only key
    {
      const mock = brainMock({ rows: ROWS.map((r) => ({ ...r, can_delete: false })) });
      const { ctx, page, T } = await open({ token: reader.json.secret, mock });
      ck("a read-only key: no Record a decision", (await T("brain-decision-record").count()) === 0);
      ck("no Delete anywhere", (await T("brain-decision-delete").count()) === 0);
      await checkWords(page, ck, "read-only");
      await page.goto(d.base + "/brain/decisions/new");
      await T("brain-decision-add-read-only").waitFor();
      eq("the form's address says why, with no form", [await text(T("brain-decision-add-read-only")), await T("brain-decision-add").count()], ["Your key can only read, so it cannot record decisions.", 0]);
      await ctx.close();
    }

    // ---- no brain, a failed read
    {
      const none = await open({ mock: brainMock({ status: 404 }), wait: "brain-decisions-not-set-up" });
      eq("no brain: not set up", await text(none.T("brain-decisions-not-set-up")), "Not set up The second brain is not set up on this cell.");
      await none.ctx.close();
      const mock = brainMock({ status: 502 });
      const bad = await open({ mock, wait: "brain-decisions-error" });
      ck("a failed read: a banner", (await text(bad.T("brain-decisions-error"))).includes("Decisions did not load."));
      mock.status = 200;
      await bad.T("brain-decisions-retry").click();
      await bad.T("brain-decision-row").first().waitFor();
      eq("Try again reads again", await bad.T("brain-decision-row").count(), 5);
      await bad.ctx.close();
    }

    // ---- one repo
    {
      const { ctx, page, mock, T } = await open({ url: "/brain/decisions?repo=glassflow/tares-cloud" });
      eq("?repo asks for that repo", mock.reads[0], { repo: "glassflow/tares-cloud", limit: "25" });
      eq("only its rows", await T("brain-decision-row").count(), 1);
      eq("the Repo dropdown shows that repo", await text(T("brain-decisions-repo")), "glassflow/tares-cloud");
      await T("brain-decisions-clear").click();
      await until(async () => (await T("brain-decision-row").count()) === 5);
      ck("Clear filters goes back to the whole list", new URL(page.url()).searchParams.get("repo") === null);
      await ctx.close();
    }

    // ---- phone width
    {
      const { ctx, page, T } = await open({ width: 390 });
      await fits(page, "filled at 390");
      await shot(page, "decisions-390.png");
      await page.goto(d.base + "/brain/decisions/new");
      await T("brain-decision-add-title").waitFor();
      await fits(page, "the form at 390");
      await shot(page, "decision-new-390.png");
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
