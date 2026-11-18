/*
 * Browser e2e for the Second Brain Decisions page (TR-1444) on a factory cell.
 *  - Unit checks of the page's pure rules (tab labels, where a decision lives, who and when, the add and
 *    delete errors, links), run on the TypeScript as written.
 *  - Knowledge, Decisions opens /brain/decisions, not Not found. The page reads GET /api/brain/decisions once
 *    (expectOneCall) and never again while it sits idle.
 *  - Empty: the line says what fills the page. Filled: each row has its title (a link to GitHub in a new tab, a
 *    project doc in the console, none for one added by hand), where it lives, who decided and when; a project
 *    row without a kind never says "null". The tabs carry the cell's counts; a tab asks with `where`.
 *  - Show more asks with the cursor and adds the next page.
 *  - Add by hand: posts {title, text, decided_by}, the new row is at the top, the counts are read again. A file
 *    over 5 MB is refused before anything is sent; a 400 shows the cell's sentence; a 501 says PDF files can't
 *    be read; a file is sent as base64 with its name.
 *  - Delete shows only where can_delete is true, asks first, then the row leaves the list.
 *  - A read-only key: no add form, no Delete, one line saying why. No brain (404): "Not set up". A failed read:
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
    rows: ROWS.slice(), counts: { ...COUNTS }, status: 200, pageSize: 50, reads: [], posts: [], deletes: [], post: null, del: null, ...extra,
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
      let rows = m.rows.filter((r) => (!q.where || r.where === q.where) && (!q.repo || (r.repo || "").toLowerCase() === q.repo.toLowerCase()));
      const start = q.cursor ? Number(q.cursor) : 0;
      const page = rows.slice(start, start + m.pageSize);
      const next = start + m.pageSize < rows.length ? String(start + m.pageSize) : null;
      return route.fulfill({ status: 200, json: { decisions: page, next_cursor: next, counts: m.counts } });
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
    eq("tabOf: known tabs, else All", [T.tabOf("by_hand"), T.tabOf("repo_folder"), T.tabOf("nope"), T.tabOf(null)], ["by_hand", "repo_folder", "all", "all"]);
    eq("tabLabel: with counts", [T.tabLabel("all", COUNTS, true), T.tabLabel("repo_folder", COUNTS, true), T.tabLabel("by_hand", { ...COUNTS, by_hand: 1200 }, true)], ["All 5", "GitHub repos 1", "Second Brain 1,200"]);
    eq("tabLabel: without counts", [T.tabLabel("factory_project", COUNTS, false), T.tabLabel("all", null, true)], ["Factory projects", "All"]);
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
      eq("the empty line says what fills the page", await text(T("brain-decisions-empty")), "No decisions yet. They show here from the decisions folders you pick in GitHub repos, from specs and plans in shared factory projects, and from ones you add below.");
      ck("an admin sees the add form", (await T("brain-decision-add").count()) === 1);
      await page.waitForLoadState("networkidle");
      await oneRead(ck, "from the menu");
      await checkWords(page, ck, "empty");
      await ctx.close();
    }

    // ---- filled
    {
      const { ctx, page, mock, T, one } = await open();
      await one(ck, "filled at 1280");
      eq("one read, no filter, limit 50", mock.reads, [{ limit: "50" }]);
      eq("every row shows", await T("brain-decision-row").count(), 5);
      const tabs = await page.getByRole("tab").allInnerTexts();
      eq("the tabs carry the cell's counts and match the origins", tabs.map((t) => t.replace(/\s+/g, " ").trim()), ["All 5", "Factory projects 2", "GitHub repos 1", "Second Brain 2"]);
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

      // a tab asks with where
      await page.getByRole("tab", { name: /Second Brain/ }).click();
      await until(() => mock.reads.length === 2);
      await until(async () => (await T("brain-decision-row").count()) === 2);
      eq("the Second Brain tab asks with where=by_hand", mock.reads[1], { where: "by_hand", limit: "50" });
      eq("it shows only those", await T("brain-decision-row").count(), 2);
      ck("the tab is in the address", new URL(page.url()).searchParams.get("where") === "by_hand", page.url());
      await ctx.close();
    }

    // ---- show more
    {
      const mock = brainMock({ pageSize: 2 });
      const { ctx, T } = await open({ mock });
      eq("a page of two", await T("brain-decision-row").count(), 2);
      await T("brain-decisions-more").click();
      await until(async () => (await T("brain-decision-row").count()) === 4);
      eq("Show more asks with the cursor", mock.reads[1], { cursor: "2", limit: "50" });
      eq("and adds the next page", await T("brain-decision-row").count(), 4);
      await T("brain-decisions-more").click();
      await until(async () => (await T("brain-decision-row").count()) === 5);
      ck("Show more leaves on the last page", (await T("brain-decisions-more").count()) === 0);
      await ctx.close();
    }

    // ---- add by hand
    {
      const { ctx, page, mock, T } = await open();
      const who = T("brain-decision-add-who");
      eq("Decided by starts with you", await text(who), "Ada Lovelace (you)");
      await who.click();
      await page.getByRole("option", { name: "Grace Hopper" }).click();
      await T("brain-decision-add-title").fill("Use Postgres for the state store");
      await T("brain-decision-add-text").fill("We chose Postgres. It is what the cloud runs.");
      await T("brain-decision-add-submit").click();
      await until(() => mock.posts.length === 1 && mock.reads.length === 2);
      eq("it posts the title, the text and who decided", mock.posts[0], { title: "Use Postgres for the state store", text: "We chose Postgres. It is what the cloud runs.", decided_by: grace.id });
      await until(async () => (await text(T("brain-decision-row").first())).includes("Use Postgres"));
      const top = T("brain-decision-row").first();
      ck("the new row is at the top, from Second Brain, decided by that person",
         (await text(top)).includes("Use Postgres for the state store") && (await text(top.getByTestId("brain-decision-origin"))) === "Second Brain"
         && (await text(top.getByTestId("brain-decision-who"))) === "Grace Hopper", await text(top));
      await until(async () => (await page.getByRole("tab", { name: /Second Brain/ }).innerText()).includes("3"));
      ck("the Second Brain count went up", (await page.getByRole("tab", { name: /Second Brain/ }).innerText()).includes("3"));
      eq("the form is empty again", await T("brain-decision-add-title").inputValue(), "");

      // a file over 5 MB is refused before anything is sent
      await T("brain-decision-add-title").fill("A big one");
      await T("brain-decision-add-file").setInputFiles({ name: "big.pdf", mimeType: "application/pdf", buffer: Buffer.alloc(6 * 1024 * 1024, 1) });
      await T("brain-decision-add-error").waitFor();
      eq("a 6 MB file: plain words", await text(T("brain-decision-add-error")), "That file is over 5 MB. Upload a smaller one, or paste the text.");
      await T("brain-decision-add-submit").click();
      ck("nothing was sent", mock.posts.length === 1, String(mock.posts.length));
      await T("brain-decision-add-file-clear").click();

      // a Markdown file is sent as base64 with its name
      await T("brain-decision-add-file").setInputFiles({ name: "adr-7.md", mimeType: "text/markdown", buffer: Buffer.from("# ADR 7\n\nWe chose it.\n") });
      ck("the file's name shows, and the text box is off", (await text(T("brain-decision-add-file-name"))).includes("adr-7.md") && await T("brain-decision-add-text").isDisabled());
      await T("brain-decision-add-submit").click();
      await until(() => mock.posts.length === 2);
      const sent = mock.posts[1];
      ck("a file goes as {name, content_base64}, no text",
         sent.file && sent.file.name === "adr-7.md" && Buffer.from(sent.file.content_base64, "base64").toString() === "# ADR 7\n\nWe chose it.\n" && !("text" in sent), JSON.stringify(sent).slice(0, 200));

      // the cell's errors
      mock.post = async () => ({ status: 501, json: { detail: "PDF support is not installed" } });
      await T("brain-decision-add-title").fill("From a PDF");
      await T("brain-decision-add-file").setInputFiles({ name: "a.pdf", mimeType: "application/pdf", buffer: Buffer.from("%PDF-1.4 x") });
      await T("brain-decision-add-submit").click();
      await T("brain-decision-add-error").waitFor();
      eq("a 501: this cell can't read PDF files", await text(T("brain-decision-add-error")), "This cell can't read PDF files yet. Paste the text or upload a Markdown file.");
      await T("brain-decision-add-file-clear").click();
      mock.post = async () => ({ status: 400, json: { detail: "decided_by: nobody on the cell is called that" } });
      await T("brain-decision-add-text").fill("Some text.");
      await T("brain-decision-add-submit").click();
      await until(async () => (await text(T("brain-decision-add-error"))).startsWith("Decided_by"));
      eq("a 400: the cell's own sentence", await text(T("brain-decision-add-error")), "Decided_by: nobody on the cell is called that.");
      mock.post = async () => ({ status: 502, json: { detail: "lore said no" } });
      await T("brain-decision-add-submit").click();
      await until(async () => (await text(T("brain-decision-add-error"))) === "Couldn't save it. Try again.");
      eq("a 502: try again", await text(T("brain-decision-add-error")), "Couldn't save it. Try again.");
      await checkWords(page, ck, "add errors");
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
      await until(async () => (await page.getByRole("tab", { name: /Second Brain/ }).innerText()).includes("1"));
      ck("the counts drop", (await page.getByRole("tab", { name: /^All/ }).innerText()).includes("4"));
      ck("the dialog closed", (await T("brain-decision-delete-dialog").count()) === 0);
      await ctx.close();
    }

    // ---- a read-only key
    {
      const mock = brainMock({ rows: ROWS.map((r) => ({ ...r, can_delete: false })) });
      const { ctx, page, T } = await open({ token: reader.json.secret, mock });
      ck("a read-only key: no add form", (await T("brain-decision-add").count()) === 0);
      eq("one line says why", await text(T("brain-decision-add-read-only")), "Your key can only read, so it cannot add decisions.");
      ck("no Delete anywhere", (await T("brain-decision-delete").count()) === 0);
      await checkWords(page, ck, "read-only");
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
      eq("?repo asks for that repo", mock.reads[0], { repo: "glassflow/tares-cloud", limit: "50" });
      eq("only its rows", await T("brain-decision-row").count(), 1);
      ck("the page says which repo, and that the counts are the cell's", (await text(T("brain-decisions-repo-filter"))).includes("glassflow/tares-cloud"));
      await T("brain-decisions-repo-clear").click();
      await until(async () => (await T("brain-decision-row").count()) === 5);
      ck("Show every repo goes back to the whole list", new URL(page.url()).searchParams.get("repo") === null);
      await ctx.close();
    }

    // ---- phone width
    {
      const { ctx, page, T } = await open({ width: 390 });
      await fits(page, "filled at 390");
      await shot(page, "decisions-390.png");
      await T("brain-decision-add-title").fill("x");
      await fits(page, "the add form at 390");
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
