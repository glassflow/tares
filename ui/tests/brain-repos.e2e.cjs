/*
 * Browser e2e for the Second Brain Repos page (TR-1204) on a factory cell.
 *  - Unit checks of the page's pure rules (poll interval, card kind, words), run on the TypeScript as written.
 *  - The Repos row under Knowledge in the menu opens /brain.
 *  - Every card state shows its words: queued, cloning, indexing, ready, ready with failing updates,
 *    not indexed, removing, removal failed. The Decisions row and button are not there (TR-1213).
 *  - An admin sees Connect, Retry and Remove; a read key sees the cards and none of them.
 *  - Remove asks first, then DELETE once: 202 turns the card into "Removing", 200 takes it away, an error is
 *    said in words and the repo stays. Retry removal sends the same DELETE. Retry sends POST retry.
 *  - Empty, not set up (200 and 404), a failed read with Retry, the sample flag.
 *  - The read repeats every 10 s while a repo is moving and every 60 s otherwise.
 *  - No sideways scroll at 390. The page's words keep the words list.
 * The brain's own API is answered by the test (the cell here has no GitHub to clone from); the daemons are real
 * (tests/licensed.cjs) so the console, the keys and the 404 of a cell with no licence are real.
 * Screenshots of each state at 1280 and 390 go to $SHOTS_DIR when it is set.
 * Run:  npm run build && TARES_E2E_PORT=8880 node tests/brain-repos.e2e.cjs
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

const ROOT = "root-token-brain-repos-e2e";
const PORT = Number(process.env.TARES_E2E_PORT || 8880);
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
  const src = fs.readFileSync(path.join(UI_DIR, "src", "editions", file), "utf8");
  const js = ts.transpileModule(src, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 } }).outputText;
  const mod = { exports: {} };
  vm.runInNewContext(js, {
    module: mod, exports: mod.exports, Date, Number, Math, Object, JSON, Promise, RegExp, Array,
    require: (name) => { if (name in requires) return requires[name]; throw new Error(`unexpected import ${name}`); },
  });
  return mod.exports;
}
const wordsJson = JSON.parse(fs.readFileSync(path.join(UI_DIR, "src", "editions", "words.json"), "utf8"));
const wordsMod = loadTs("words.ts", { "./words.json": { default: wordsJson } });
const timeMod = loadTs(path.join("..", "lib", "time.ts"));
const numberMod = loadTs(path.join("..", "lib", "number.ts"));
const T = loadTs(path.join("factory", "brainText.ts"), { "../../lib/time": timeMod, "../../lib/number": numberMod });
const S = loadTs(path.join("factory", "brainSamples.ts"), {});

// ---- the cell's rows, as GET /api/brain gives them
const MIN = 60_000;
const iso = (agoMs) => new Date(Date.now() - agoMs).toISOString();
const row = (o) => ({
  repo: "glassflow/tares", state: "ready", files_done: 0, files_total: 0, indexed_commit: null, last_synced: null,
  error: null, place: null, sync_failures: 0, sync_error: null, added_by: "p1", added_by_name: "Ada Lovelace",
  added_at: iso(60 * MIN), removing: false, remove_error: null, ...o,
});
const READY = (repo, o = {}) => row({ repo, state: "ready", files_done: 640, files_total: 640, indexed_commit: "8c41d2e9a7b0c3d4e5f6", last_synced: iso(2 * MIN), added_at: iso(300 * MIN), ...o });
const allRows = () => [
  row({ repo: "acme/indexer", state: "indexing", files_done: 812, files_total: 1816, added_at: iso(14 * MIN) }),
  row({ repo: "acme/docs-site", state: "queued", place: 1, added_at: iso(9 * MIN) }),
  row({ repo: "acme/fetcher", state: "cloning", added_at: iso(3 * MIN) }),
  READY("acme/api"),
  READY("acme/web", { files_done: 212, files_total: 212, sync_failures: 3, sync_error: "GitHub did not answer when the brain asked for the latest push.", last_synced: iso(95 * MIN) }),
  row({ repo: "acme/legacy", state: "failed", error: "GitHub refused access while cloning. Give the connection read access to this repo, then try again.", added_at: iso(30 * MIN) }),
  READY("acme/old-prototype", { removing: true }),
  READY("acme/old-site", { remove_error: "Still indexing. Try again in a minute." }),
];

// ---- the brain API, answered here
function brainMock(rows, extra = {}) {
  const m = { rows, configured: true, readStatus: 200, reads: 0, deletes: [], retries: [], deleteAnswer: null, retryAnswer: null, ...extra };
  m.handle = async (route) => {
    const req = route.request();
    const p = new URL(req.url()).pathname;
    if (req.method() === "GET" && p === "/api/brain") {
      m.reads++;
      if (m.readStatus !== 200) return route.fulfill({ status: m.readStatus, json: { detail: "nope" } });
      return route.fulfill({ status: 200, json: { configured: m.configured, repos: m.rows } });
    }
    const repo = p.replace("/api/brain/repos/", "").replace(/\/retry$/, "");
    if (req.method() === "DELETE") {
      m.deletes.push(repo);
      const a = m.deleteAnswer ? await m.deleteAnswer(repo, m) : { status: 200, json: { ok: true, removed: true } };
      if (a.status === 200 || a.status === 404) m.rows = m.rows.filter((r) => r.repo !== repo);
      if (a.status === 202) m.rows = m.rows.map((r) => (r.repo === repo ? { ...r, removing: true, remove_error: null } : r));
      return route.fulfill(a);
    }
    if (req.method() === "POST" && p.endsWith("/retry")) {
      m.retries.push(repo);
      if (m.retryAnswer) { const a = await m.retryAnswer(repo, m); if (a.status === 404) m.rows = m.rows.filter((r) => r.repo !== repo); if (a.status !== 200) return route.fulfill(a); }
      m.rows = m.rows.map((r) => (r.repo === repo ? { ...r, state: "queued", error: null } : r));
      return route.fulfill({ status: 200, json: m.rows.find((r) => r.repo === repo) });
    }
    return route.fulfill({ status: 404, json: { detail: "unexpected" } });
  };
  return m;
}
const isBrainApi = (u) => u.pathname === "/api/brain" || u.pathname.startsWith("/api/brain/");

(async () => {
  // ---- 0. the pure rules
  {
    const r = (o) => row(o);
    eq("pollMs: no data reads every minute", T.pollMs(undefined), 60_000);
    eq("pollMs: no repos", T.pollMs({ configured: true, repos: [] }), 60_000);
    eq("pollMs: all ready", T.pollMs({ configured: true, repos: [READY("a/b"), READY("a/c")] }), 60_000);
    for (const s of ["queued", "cloning", "indexing"]) eq(`pollMs: a ${s} repo reads every 10 s`, T.pollMs({ configured: true, repos: [READY("a/b"), r({ repo: "a/c", state: s })] }), 10_000);
    eq("pollMs: a removal in progress reads every 10 s", T.pollMs({ configured: true, repos: [READY("a/b", { removing: true })] }), 10_000);
    eq("pollMs: not indexed waits for a person, a minute", T.pollMs({ configured: true, repos: [r({ repo: "a/b", state: "failed" })] }), 60_000);
    eq("pollMs: a removal that gave up waits for a person, a minute", T.pollMs({ configured: true, repos: [READY("a/b", { remove_error: "x" })] }), 60_000);
    eq("cardKind: each index state is its own card", ["queued", "cloning", "indexing", "ready", "failed"].map((s) => T.cardKind(r({ state: s }))), ["queued", "cloning", "indexing", "ready", "failed"]);
    eq("cardKind: removing comes first", T.cardKind(r({ state: "indexing", removing: true, remove_error: "x" })), "removing");
    eq("cardKind: a removal error is its own card", T.cardKind(r({ state: "ready", remove_error: "x" })), "remove_failed");
    eq("state words", Object.values(T.STATE_WORDS), ["Queued", "Cloning", "Indexing", "Ready", "Not indexed", "Removing", "Not removed"]);
    eq("shortCommit is 7 characters", T.shortCommit("8c41d2e9a7b0"), "8c41d2e");
    eq("shortCommit of nothing", T.shortCommit(null), "");
    eq("count has a thousands separator", [T.count(1816), T.count(0), T.count(undefined), T.count(812)], ["1,816", "0", "0", "812"]);
    const now = Date.parse("2026-11-12T10:00:00Z");
    eq("ago: minutes", T.ago("2026-11-12T09:51:00Z", now), "9 minutes ago");
    eq("ago: a time with no zone is UTC", T.ago("2026-11-12T09:51:00", now), "9 minutes ago");
    eq("ago: hours", T.ago("2026-11-12T07:00:00Z", now), "3 hours ago");
    eq("ago: none", T.ago(null, now), "");
    eq("ago: a time ahead of the clock does not go negative", T.ago("2026-11-12T10:05:00Z", now), "just now");
    const busy = r({ repo: "a/busy", state: "indexing" });
    const q = r({ repo: "a/q", state: "queued" });
    eq("queuedWords: waits for the repo being indexed", T.queuedWords(q, [busy, q]), { waitsFor: "a/busy", line: "finishes." });
    eq("queuedWords: waits for a repo being cloned", T.queuedWords(q, [r({ repo: "a/c", state: "cloning" }), q]).waitsFor, "a/c");
    eq("queuedWords: no repo busy", T.queuedWords(q, [q, READY("a/r")]), { waitsFor: null, line: "Starts when the brain has room." });
    eq("queuedWords: a repo being removed is not waited for", T.queuedWords(q, [r({ repo: "a/x", state: "indexing", removing: true }), q]).waitsFor, null);
    const place = (n) => r({ repo: "a/p", state: "queued", place: n });
    eq("queuedWords: place 1 keeps the wait for the busy repo", T.queuedWords(place(1), [busy, place(1)]), { waitsFor: "a/busy", line: "finishes." });
    eq("queuedWords: place 3 says where it stands, not a repo", T.queuedWords(place(3), [busy, place(3)]), { waitsFor: null, line: "Third in line." });
    eq("queuedWords: place 2, 10 and 11", [2, 10, 11].map((n) => T.queuedWords(place(n), [place(n)]).line), ["Second in line.", "Tenth in line.", "Number 11 in line."]);
    eq("queuedWords: no place, or a place that is not a count, keeps the general line", [null, 0, 1.5, "3"].map((n) => T.queuedWords(place(n), [place(n)]).line), Array(4).fill("Starts when the brain has room."));
    ck("placeWords: no em dash", ![2, 3, 12].some((n) => T.placeWords(n).includes("\u2014")));
    eq("actionError: a 404 is a plain sentence", [T.actionError({ message: "Not Found", status: 404 }, "x"), T.actionError({ message: "acme/api is not connected to the brain", status: 404 }, "x")], Array(2).fill("This repo is no longer connected."));
    eq("actionError: the cell's own words otherwise", [T.actionError({ message: "still indexing", status: 409 }, "x"), T.actionError({ status: 502 }, "Fallback."), T.actionError(null, "Fallback.")], ["Still indexing.", "Fallback.", "Fallback."]);
    eq("percent", [T.percent(r({ files_done: 812, files_total: 1816 })), T.percent(r({ files_done: 0, files_total: 0 })), T.percent(r({ files_done: 9, files_total: 3 }))], [45, 0, 100]);
    eq("plainError: capital and full stop", T.plainError("still indexing, try again", "x"), "Still indexing, try again.");
    eq("plainError: keeps a full stop", T.plainError("Done.", "x"), "Done.");
    eq("plainError: falls back", T.plainError("  ", "Fallback."), "Fallback.");
    const kinds = new Set(S.BRAIN_SAMPLES.all.repos.map((x) => T.cardKind(x)));
    ck("samples: a queued repo behind others says its place", S.BRAIN_SAMPLES.queued.repos.some((x) => x.state === "queued" && x.place === 3));
    eq("samples: the all sample has every card state", [...kinds].sort(), ["cloning", "failed", "indexing", "queued", "ready", "remove_failed", "removing"].sort());
    ck("samples: a ready sample with failing updates", S.BRAIN_SAMPLES["ready-sync-failing"].repos[0].sync_failures > 0);
    ck("samples: empty and not set up", S.BRAIN_SAMPLES.empty.repos.length === 0 && S.BRAIN_SAMPLES["not-set-up"].configured === false);
    ck("samples: isBrainSample", S.isBrainSample("all") && S.isBrainSample("loading") && !S.isBrainSample("nope") && !S.isBrainSample(null) && !S.isBrainSample("toString"));
  }

  // ---- the daemons: licensed with keys, and one with no licence
  let d, bare;
  try {
    d = await startDaemon({ port: PORT, edition: "factory", licence: "valid", seats: 50, auth: ROOT });
    bare = await startDaemon({ port: PORT + 2, edition: "factory", licence: "none" });
  } catch (e) { console.log("FAIL daemon did not start: " + e.message); process.exit(1); }
  process.on("exit", () => { d.stop(); bare.stop(); });
  const root = { authorization: `Bearer ${ROOT}` };
  const admin = await call(d.base, "POST", "/api/keys", { name: "ada-laptop", scopes: ["admin"], person: { email: "ada@example.com", name: "Ada Lovelace" } }, root);
  const reader = await call(d.base, "POST", "/api/keys", { name: "grace-laptop", scopes: ["read"], person: { email: "grace@example.com", name: "Grace Hopper" } }, root);
  ck("keys made", admin.status === 201 && reader.status === 201, `${admin.status} ${reader.status}`);

  const browser = await chromium.launch(process.env.TARES_E2E_CHROME ? { executablePath: process.env.TARES_E2E_CHROME } : {});
  const errors = [];
  const open = async (opts = {}) => {
    const { token = admin.json.secret, width = 1280, mock, base = d.base, url = "/brain", wait = true, samples = false, clock = false } = opts;
    const ctx = await browser.newContext({ viewport: { width, height: 900 } });
    await ctx.addInitScript(([t, s]) => {
      try { if (t) localStorage.setItem("tares_token", t); if (s) localStorage.setItem("taresSamples", "1"); } catch { /* none */ }
    }, [token, samples]);
    const page = await ctx.newPage();
    page.on("pageerror", (e) => errors.push(String(e)));
    if (mock) await page.route(isBrainApi, mock.handle);
    if (clock) await page.clock.install();
    await page.goto(base + url);
    if (wait) await page.getByTestId("brain-repos").waitFor({ timeout: 15000 });
    return { ctx, page };
  };
  const card = (page, repo) => page.locator(`[data-testid="brain-card"][data-repo="${repo}"]`);
  const text = async (loc) => (await loc.innerText()).replace(/\s+/g, " ").trim();
  const shot = async (page, name) => { if (SHOTS) await page.screenshot({ path: path.join(SHOTS, name), fullPage: true }); };
  const cardShots = async (page, tag) => {
    if (!SHOTS) return;
    for (const c of await page.getByTestId("brain-card").all()) {
      await c.screenshot({ path: path.join(SHOTS, `brain-repos-card-${await c.getAttribute("data-state")}-${tag}.png`) });
    }
  };

  try {
    // ---- the menu row opens the page
    {
      const mock = brainMock([READY("acme/api")]);
      const { ctx, page } = await open({ mock, url: "/projects", wait: false });
      await page.getByTestId("menu-brain-repos").click();
      await page.getByTestId("brain-repos").waitFor({ timeout: 15000 });
      ck("the menu row opens /brain", new URL(page.url()).pathname === "/brain");
      ck("the heading", await page.getByRole("heading", { name: "Repos", exact: true }).isVisible());
      ck("the menu row is current", (await page.locator('nav[aria-label="Main"] a[aria-current="page"]').allInnerTexts()).map((t) => t.trim()).join() === "Repos");
      await ctx.close();
    }

    // ---- an admin, every state
    {
      const mock = brainMock(allRows());
      const { ctx, page } = await open({ mock });
      await page.getByTestId("brain-card").first().waitFor({ timeout: 15000 });
      ck("the line under the heading", (await text(page.locator("h1 + p"))) === "The code the brain knows. Each repo is indexed once, then kept up to date with every push to main.");
      const connect = page.getByTestId("brain-connect");
      ck("Connect a repo links to /brain/connect", (await connect.innerText()).trim() === "Connect a repo" && (await connect.getAttribute("href")) === "/brain/connect");
      eq("one card per repo", await page.getByTestId("brain-card").count(), 8);
      eq("the states, in the cell's order", await page.getByTestId("brain-card").evaluateAll((els) => els.map((e) => e.getAttribute("data-state"))),
         ["indexing", "queued", "cloning", "ready", "ready", "failed", "removing", "remove_failed"]);
      // a repo name heads its card, so it is set as a heading, not in mono (TR-1270)
      ck("repo names are headings in sans, and a person's text", await page.getByTestId("brain-name").first().evaluate((e) => e.tagName === "H2" && !getComputedStyle(e).fontFamily.toLowerCase().includes("mono") && e.hasAttribute("data-user-text")));

      const idx = card(page, "acme/indexer");
      ck("indexing: the state", (await text(idx.getByTestId("brain-state"))) === "Indexing");
      ck("indexing: a progress bar at 45 percent", await idx.getByTestId("brain-progress").isVisible()
         && /45/.test((await idx.getByTestId("brain-progress").getAttribute("aria-valuenow")) ?? ""), String(await idx.getByTestId("brain-progress").getAttribute("aria-valuenow")));
      eq("indexing: files done of total", await text(idx.getByTestId("brain-files")), "812 of 1,816 files");
      eq("indexing: started, and the grep line", await text(idx.getByTestId("brain-line")), "Started 14 minutes ago. Sessions use grep on this repo until it is ready.");
      ck("indexing: no buttons", (await idx.getByRole("button").count()) === 0);

      const q = card(page, "acme/docs-site");
      ck("queued: the state", (await text(q.getByTestId("brain-state"))) === "Queued");
      eq("queued: waits for the repo being indexed, and who added it", await text(q.getByTestId("brain-line")), "Starts when acme/indexer finishes. Added by Ada Lovelace 9 minutes ago.");
      ck("queued: who added it is a person's text", await q.locator("[data-user-text]", { hasText: "Ada Lovelace" }).count() === 1);

      const cl = card(page, "acme/fetcher");
      ck("cloning: the state, a bar and the started line", (await text(cl.getByTestId("brain-state"))) === "Cloning" && await cl.getByTestId("brain-progress").isVisible()
         && (await text(cl.getByTestId("brain-line"))) === "Started 3 minutes ago. Sessions use grep on this repo until it is ready.");

      const rd = card(page, "acme/api");
      ck("ready: the state", (await text(rd.getByTestId("brain-state"))) === "Ready");
      eq("ready: up to date with main at the 7 character commit", await text(rd.getByTestId("brain-synced")), "main at 8c41d2e, 2 minutes ago");
      ck("ready: the label", (await text(rd)).includes("Up to date with main at 8c41d2e, 2 minutes ago"));
      eq("ready: the files count", await text(rd.getByTestId("brain-file-count")), "640");
      ck("ready: no sync note", (await rd.getByTestId("brain-sync-note").count()) === 0);
      eq("ready: Remove only", await rd.getByRole("button").allInnerTexts(), ["Remove"]);

      const sf = card(page, "acme/web");
      ck("sync failing: still Ready, with a quiet note in words", (await text(sf.getByTestId("brain-state"))) === "Ready"
         && (await text(sf.getByTestId("brain-sync-note"))) === "The last 3 updates from main did not go through. GitHub did not answer when the brain asked for the latest push.", await text(sf.getByTestId("brain-sync-note")));

      const fl = card(page, "acme/legacy");
      ck("not indexed: the state and the error as the cell worded it", (await text(fl.getByTestId("brain-state"))) === "Not indexed"
         && (await text(fl.getByTestId("brain-error"))) === "GitHub refused access while cloning. Give the connection read access to this repo, then try again.");
      eq("not indexed: Retry and Remove", await fl.getByRole("button").allInnerTexts(), ["Retry", "Remove"]);

      const rm = card(page, "acme/old-prototype");
      ck("removing: the words, and no buttons", (await text(rm.getByTestId("brain-state"))) === "Removing"
         && (await text(rm.getByTestId("brain-line"))) === "Removing. This takes a minute or two." && (await rm.getByRole("button").count()) === 0);

      const rf = card(page, "acme/old-site");
      ck("removal failed: the error and only Retry removal", (await text(rf.getByTestId("brain-remove-error"))) === "Still indexing. Try again in a minute."
         && (await rf.getByRole("button").allInnerTexts()).join() === "Retry removal");
      const border = (c) => c.evaluate((e) => getComputedStyle(e).borderTopColor);
      ck("removal failed: the border is the failed colour, not the ready one", (await border(rf)) === (await border(fl)) && (await border(rf)) !== (await border(rd)), `${await border(rf)} ${await border(fl)} ${await border(rd)}`);

      const colour = (loc) => loc.evaluate((e) => getComputedStyle(e).color);
      const ink = await colour(idx.getByTestId("brain-name"));
      eq("Indexing and Cloning are in the page's own ink", [await colour(idx.getByTestId("brain-state")), await colour(cl.getByTestId("brain-state"))], [ink, ink]);
      const [cReady, cFailed, cQueued] = [await colour(rd.getByTestId("brain-state")), await colour(fl.getByTestId("brain-state")), await colour(q.getByTestId("brain-state"))];
      ck("Ready is green, Not indexed red, Queued quiet: three colours, none the ink", new Set([cReady, cFailed, cQueued, ink]).size === 4, `${cReady} ${cFailed} ${cQueued} ${ink}`);
      ck("Not removed is the same red as Not indexed", (await colour(rf.getByTestId("brain-state"))) === cFailed);
      const edge = (loc) => loc.evaluate((e) => getComputedStyle(e).borderTopColor);
      const dark = await edge(fl.getByTestId("brain-retry"));
      ck("Retry has the dark border (the foreground), Remove the light one", dark === ink && (await edge(fl.getByTestId("brain-remove"))) !== dark && (await edge(rd.getByTestId("brain-remove"))) !== dark,
         `${dark} ${ink} ${await edge(fl.getByTestId("brain-remove"))}`);
      ck("Retry removal has the dark border too", (await edge(rf.getByTestId("brain-retry-removal"))) === dark);

      const body = await text(page.locator("main"));
      ck("no Decisions row, section or folders button (TR-1213)", !/Decisions the brain knows|Decision folders/.test(body) && !/\bDecisions\b/.test(await text(page.getByTestId("brain-cards"))));
      await checkWords(page, ck, "admin, every state");
      ck("no script errors", errors.length === 0, errors.join(" "));
      await shot(page, "brain-repos-all-1280.png");
      await cardShots(page, "1280");
      await ctx.close();
    }

    // ---- a read key: the cards, and nothing to press
    {
      const mock = brainMock(allRows());
      const { ctx, page } = await open({ token: reader.json.secret, mock });
      await page.getByTestId("brain-card").first().waitFor({ timeout: 15000 });
      ck("read key: every card shows", (await page.getByTestId("brain-card").count()) === 8);
      ck("read key: no Connect, Retry, Remove or Retry removal", (await page.getByTestId("brain-connect").count()) === 0 && (await page.getByTestId("brain-card").getByRole("button").count()) === 0);
      await checkWords(page, ck, "read key");
      await shot(page, "brain-repos-readonly-1280.png");
      await ctx.close();
      const empty = brainMock([]);
      const e = await open({ token: reader.json.secret, mock: empty });
      await e.page.getByTestId("brain-empty").waitFor({ timeout: 15000 });
      ck("read key, empty: the words and no Connect", (await text(e.page.getByTestId("brain-empty"))).includes("Connect a repo so the crew can ask about its code instead of searching it.") && (await e.page.getByTestId("brain-connect").count()) === 0);
      await e.ctx.close();
    }

    // ---- remove: asks first, 202 then 200, errors in words
    {
      const mock = brainMock([READY("acme/api"), READY("acme/web"), READY("acme/mobile")]);
      mock.deleteAnswer = (repo) => (repo === "acme/api" ? { status: 202, json: { ok: true, removing: true } }
        : repo === "acme/mobile" ? { status: 409, json: { detail: "Still indexing. Try again in a minute." } } : { status: 200, json: { ok: true, removed: true } });
      const { ctx, page } = await open({ mock });
      await card(page, "acme/api").waitFor({ timeout: 15000 });
      await card(page, "acme/api").getByTestId("brain-remove").click();
      const dlg = page.getByTestId("brain-remove-dialog");
      await dlg.waitFor({ timeout: 5000 });
      ck("the dialog names the repo and says what goes", (await text(dlg.getByTestId("brain-remove-name"))) === "acme/api" && (await text(dlg)).includes("Removes its index and decisions from the brain."));
      await checkWords(page, ck, "remove dialog");
      await shot(page, "brain-repos-remove-dialog-1280.png");
      await dlg.getByTestId("brain-remove-cancel").click();
      await dlg.waitFor({ state: "hidden", timeout: 5000 });
      ck("Cancel sends nothing", mock.deletes.length === 0);
      await card(page, "acme/api").getByTestId("brain-remove").click();
      await dlg.waitFor({ timeout: 5000 });
      await dlg.getByTestId("brain-remove-confirm").click();
      await dlg.waitFor({ state: "hidden", timeout: 10000 });
      await page.waitForFunction(() => document.querySelector('[data-repo="acme/api"]')?.getAttribute("data-state") === "removing", null, { timeout: 10000 });
      eq("202: one DELETE, for that repo", mock.deletes, ["acme/api"]);
      ck("202: the card says it is removing and has no buttons", (await text(card(page, "acme/api").getByTestId("brain-line"))) === "Removing. This takes a minute or two."
         && (await card(page, "acme/api").getByRole("button").count()) === 0);
      // 200: gone
      await card(page, "acme/web").getByTestId("brain-remove").click();
      await dlg.waitFor({ timeout: 5000 });
      await dlg.getByTestId("brain-remove-confirm").click();
      await card(page, "acme/web").waitFor({ state: "detached", timeout: 10000 });
      eq("200: a second DELETE, and the card is gone", mock.deletes, ["acme/api", "acme/web"]);
      // 409: said in words, the repo stays
      await card(page, "acme/mobile").getByTestId("brain-remove").click();
      await dlg.waitFor({ timeout: 5000 });
      await dlg.getByTestId("brain-remove-confirm").click();
      await dlg.getByTestId("brain-remove-failed").waitFor({ timeout: 10000 });
      ck("409: the cell's words, in the dialog", (await text(dlg.getByTestId("brain-remove-failed"))).includes("Still indexing. Try again in a minute."));
      ck("409: the repo is still there", (await card(page, "acme/mobile").count()) === 1);
      await checkWords(page, ck, "remove error");
      await dlg.getByTestId("brain-remove-cancel").click();
      // 502: also words
      mock.deleteAnswer = () => ({ status: 502, json: { detail: "The brain could not reach the control plane" } });
      await card(page, "acme/mobile").getByTestId("brain-remove").click();
      await dlg.waitFor({ timeout: 5000 });
      await dlg.getByTestId("brain-remove-confirm").click();
      await dlg.getByTestId("brain-remove-failed").waitFor({ timeout: 10000 });
      ck("502: said in words", (await text(dlg.getByTestId("brain-remove-failed"))).includes("The brain could not reach the control plane."));
      await ctx.close();
    }

    // ---- retry removal: the same DELETE; retry: POST retry
    {
      const mock = brainMock(allRows());
      const { ctx, page } = await open({ mock });
      await card(page, "acme/old-site").waitFor({ timeout: 15000 });
      mock.deleteAnswer = () => ({ status: 202, json: { ok: true, removing: true } });
      await card(page, "acme/old-site").getByTestId("brain-retry-removal").click();
      await page.waitForFunction(() => document.querySelector('[data-repo="acme/old-site"]')?.getAttribute("data-state") === "removing", null, { timeout: 10000 });
      eq("Retry removal: one DELETE, no dialog", mock.deletes, ["acme/old-site"]);
      ck("Retry removal: the card is removing now", (await card(page, "acme/old-site").getByRole("button").count()) === 0);
      await card(page, "acme/legacy").getByTestId("brain-retry").click();
      await page.waitForFunction(() => document.querySelector('[data-repo="acme/legacy"]')?.getAttribute("data-state") === "queued", null, { timeout: 10000 });
      eq("Retry: one POST retry for that repo", mock.retries, ["acme/legacy"]);
      ck("Retry: the card is queued, with no error", (await card(page, "acme/legacy").getByTestId("brain-error").count()) === 0);
      await ctx.close();
    }


    // ---- each repo is busy until its own call ends (two repos, calls interleaved)
    {
      const mock = brainMock([
        row({ repo: "acme/legacy", state: "failed", error: "GitHub refused access while cloning." }),
        READY("acme/old-site", { remove_error: "Still indexing. Try again in a minute." }),
      ]);
      let release;
      const gate = new Promise((res) => { release = res; });
      mock.deleteAnswer = async () => { await gate; return { status: 202, json: { ok: true, removing: true } }; };
      const { ctx, page } = await open({ mock });
      await card(page, "acme/old-site").waitFor({ timeout: 15000 });
      const slow = card(page, "acme/old-site").getByTestId("brain-retry-removal");
      await slow.click();
      await page.waitForFunction(() => document.querySelector('[data-testid="brain-retry-removal"]')?.getAttribute("aria-busy") === "true", null, { timeout: 5000 });
      ck("the slow Retry removal: its button is busy and disabled", (await slow.isDisabled()) && (await slow.getAttribute("aria-busy")) === "true");
      // while that DELETE waits, another repo's Retry goes through and ends
      await card(page, "acme/legacy").getByTestId("brain-retry").click();
      await page.waitForFunction(() => document.querySelector('[data-repo="acme/legacy"]')?.getAttribute("data-state") === "queued", null, { timeout: 10000 });
      eq("the other repo's Retry ended: one POST", mock.retries, ["acme/legacy"]);
      ck("the first button stays disabled until its own call ends", (await slow.isDisabled()) && (await slow.getAttribute("aria-busy")) === "true");
      eq("and no second DELETE went out", mock.deletes, ["acme/old-site"]);
      release();
      await page.waitForFunction(() => document.querySelector('[data-repo="acme/old-site"]')?.getAttribute("data-state") === "removing", null, { timeout: 10000 });
      ck("its own call ended: the card is removing", (await card(page, "acme/old-site").getByRole("button").count()) === 0);
      await ctx.close();
    }

    // ---- a queued repo behind others says where it stands
    {
      const mock = brainMock([
        row({ repo: "acme/indexer", state: "indexing", files_done: 5, files_total: 50, added_at: iso(14 * MIN) }),
        row({ repo: "acme/next-up", state: "queued", place: 1, added_at: iso(9 * MIN) }),
        row({ repo: "acme/second", state: "queued", place: 2, added_at: iso(8 * MIN) }),
        row({ repo: "acme/third", state: "queued", place: 3, added_at: iso(7 * MIN) }),
      ]);
      const { ctx, page } = await open({ mock });
      await card(page, "acme/third").waitFor({ timeout: 15000 });
      eq("queued, place 1: waits for the repo being indexed", await text(card(page, "acme/next-up").getByTestId("brain-line")), "Starts when acme/indexer finishes. Added by Ada Lovelace 9 minutes ago.");
      eq("queued, place 2: says it is second", await text(card(page, "acme/second").getByTestId("brain-line")), "Second in line. Added by Ada Lovelace 8 minutes ago.");
      eq("queued, place 3: says it is third, plainly", await text(card(page, "acme/third").getByTestId("brain-line")), "Third in line. Added by Ada Lovelace 7 minutes ago.");
      await checkWords(page, ck, "queued places");
      await ctx.close();
    }

    // ---- a 404 on Remove or Retry: a plain sentence, and the list is read again
    {
      const gone = { status: 404, json: { detail: "Not Found" } };
      const mock = brainMock([READY("acme/api"), READY("acme/web"), READY("acme/old-site", { remove_error: "Still indexing." }), row({ repo: "acme/legacy", state: "failed", error: "No access." })]);
      mock.deleteAnswer = () => gone;
      mock.retryAnswer = () => gone;
      const { ctx, page } = await open({ mock });
      await card(page, "acme/api").waitFor({ timeout: 15000 });
      const dlg = page.getByTestId("brain-remove-dialog");
      const reads = mock.reads;
      await card(page, "acme/api").getByTestId("brain-remove").click();
      await dlg.waitFor({ timeout: 5000 });
      await dlg.getByTestId("brain-remove-confirm").click();
      await dlg.getByTestId("brain-remove-failed").waitFor({ timeout: 10000 });
      const said = await text(dlg.getByTestId("brain-remove-failed"));
      ck("DELETE 404: a plain sentence in the dialog, not Not Found", said.includes("This repo is no longer connected.") && !/Not Found/i.test(said), said);
      await card(page, "acme/api").waitFor({ state: "detached", timeout: 10000 });
      ck("DELETE 404: the list was read again, the card is gone", mock.reads > reads && (await card(page, "acme/api").count()) === 0);
      await checkWords(page, ck, "remove 404");
      await dlg.getByTestId("brain-remove-cancel").click();
      await card(page, "acme/old-site").getByTestId("brain-retry-removal").click();
      await page.getByText("This repo is no longer connected.", { exact: true }).first().waitFor({ timeout: 5000 });
      await card(page, "acme/old-site").waitFor({ state: "detached", timeout: 10000 });
      ck("Retry removal 404: said in a toast, the card is gone", (await page.getByText(/Not Found/i).count()) === 0);
      await card(page, "acme/legacy").getByTestId("brain-retry").click();
      await card(page, "acme/legacy").waitFor({ state: "detached", timeout: 10000 });
      ck("Retry 404: the card is gone after a read", (await card(page, "acme/legacy").count()) === 0 && (await page.getByText(/Not Found/i).count()) === 0);
      await ctx.close();
    }

    // ---- empty, not set up, a failed read
    {
      const mock = brainMock([]);
      const { ctx, page } = await open({ mock });
      await page.getByTestId("brain-empty").waitFor({ timeout: 15000 });
      ck("empty: the words", (await text(page.getByTestId("brain-empty"))).includes("Connect a repo so the crew can ask about its code instead of searching it."));
      ck("empty: Connect for an admin", (await page.getByTestId("brain-connect").count()) === 1 && (await page.getByTestId("brain-connect").getAttribute("href")) === "/brain/connect");
      await checkWords(page, ck, "empty");
      await shot(page, "brain-repos-empty-1280.png");
      await ctx.close();
      for (const [what, m] of [["200 and not configured", brainMock([], { configured: false })], ["404", brainMock([], { readStatus: 404 })]]) {
        const o = await open({ mock: m });
        await o.page.getByTestId("brain-not-set-up").waitFor({ timeout: 15000 });
        ck(`not set up (${what}): the words, and no Connect`, (await text(o.page.getByTestId("brain-not-set-up"))).includes("The second brain is not set up on this cell.") && (await o.page.getByTestId("brain-connect").count()) === 0);
        if (what === "404") await shot(o.page, "brain-repos-not-set-up-1280.png");
        await o.ctx.close();
      }
      // a cell with no licence answers 404 for real
      const nl = await open({ base: bare.base, token: "" });
      await nl.page.getByTestId("brain-not-set-up").waitFor({ timeout: 15000 });
      ck("no licence: the real 404 reads as not set up", (await text(nl.page.getByTestId("brain-not-set-up"))).includes("The second brain is not set up on this cell."));
      await nl.ctx.close();
      // a failed read: a Banner with Retry, then the cards
      const bad = brainMock([READY("acme/api")], { readStatus: 500 });
      const f = await open({ mock: bad, wait: false });
      await f.page.getByRole("button", { name: "Retry" }).waitFor({ timeout: 15000 });
      ck("a failed read: the banner says so in words", (await text(f.page.getByTestId("brain-repos"))).includes("Repos did not load."));
      await checkWords(f.page, ck, "failed read");
      await shot(f.page, "brain-repos-error-1280.png");
      bad.readStatus = 200;
      await f.page.getByRole("button", { name: "Retry" }).click();
      await card(f.page, "acme/api").waitFor({ timeout: 10000 });
      ck("a failed read: Retry loads the cards", (await f.page.getByRole("button", { name: "Retry" }).count()) === 0);
      await f.ctx.close();
      // loading
      const slow = brainMock([READY("acme/api")]);
      const gate = new Promise((res) => setTimeout(res, 1500));
      const lo = await browser.newContext({ viewport: { width: 1280, height: 900 } });
      await lo.addInitScript((t) => { try { localStorage.setItem("tares_token", t); } catch { /* none */ } }, admin.json.secret);
      const lp = await lo.newPage();
      await lp.route(isBrainApi, async (route) => { if (new URL(route.request().url()).pathname === "/api/brain" && slow.reads >= 1) await gate; return slow.handle(route); });
      await lp.goto(d.base + "/brain");
      await lp.getByTestId("brain-loading").waitFor({ timeout: 15000 });
      ck("loading: a skeleton shows", await lp.getByTestId("brain-loading").isVisible());
      await shot(lp, "brain-repos-loading-1280.png");
      await card(lp, "acme/api").waitFor({ timeout: 15000 });
      await lo.close();
    }

    // ---- the pace of the read, on a fake clock
    {
      const settle = async (page) => { await page.waitForLoadState("networkidle"); await page.clock.runFor(100); await page.waitForTimeout(150); };
      const pace = async (what, rows, wantMs, afterFirst) => {
        const mock = brainMock(rows);
        const { ctx, page } = await open({ mock, clock: true });
        await page.getByTestId("brain-card").first().waitFor({ timeout: 15000 });
        await settle(page);
        const base = mock.reads;
        await page.clock.runFor(wantMs - 1500);
        await page.waitForTimeout(150);
        ck(`${what}: no read before ${wantMs / 1000} s`, mock.reads === base, `${mock.reads - base} reads`);
        await page.clock.runFor(3000);
        await page.waitForTimeout(250);
        ck(`${what}: a read at ${wantMs / 1000} s`, mock.reads === base + 1, `${mock.reads - base} reads`);
        if (afterFirst) await afterFirst(mock, page);
        await ctx.close();
      };
      await pace("every repo ready", [READY("acme/api")], 60_000);
      await pace("a repo indexing", [READY("acme/api"), row({ repo: "acme/new", state: "indexing", files_done: 5, files_total: 50 })], 10_000);
      await pace("a repo queued", [row({ repo: "acme/new", state: "queued" })], 10_000);
      await pace("a repo being removed", [READY("acme/api", { removing: true })], 10_000);
      await pace("a repo not indexed", [row({ repo: "acme/new", state: "failed", error: "No." })], 60_000);
      // the pace follows the data: the repo finishes, and the next reads are a minute apart
      {
        const mock = brainMock([row({ repo: "acme/new", state: "indexing", files_done: 5, files_total: 50 })]);
        const { ctx, page } = await open({ mock, clock: true });
        await page.getByTestId("brain-card").first().waitFor({ timeout: 15000 });
        await settle(page);
        mock.rows = [READY("acme/new")];
        await page.clock.runFor(11_000);
        await page.waitForFunction(() => document.querySelector('[data-repo="acme/new"]')?.getAttribute("data-state") === "ready", null, { timeout: 10000 });
        await settle(page);
        const base = mock.reads;
        await page.clock.runFor(45_000);
        await page.waitForTimeout(150);
        ck("the repo is ready: no read in the next 45 s", mock.reads === base, `${mock.reads - base} reads`);
        await page.clock.runFor(20_000);
        await page.waitForTimeout(250);
        ck("the repo is ready: a read after a minute", mock.reads === base + 1, `${mock.reads - base} reads`);
        await ctx.close();
      }
      // a removal in the background ends when the row is gone
      {
        const mock = brainMock([READY("acme/api", { removing: true })]);
        const { ctx, page } = await open({ mock, clock: true });
        await card(page, "acme/api").waitFor({ timeout: 15000 });
        await settle(page);
        mock.rows = [];
        await page.clock.runFor(11_000);
        await page.getByTestId("brain-empty").waitFor({ timeout: 10000 });
        ck("a removal that finishes: the card goes by itself", (await card(page, "acme/api").count()) === 0);
        await ctx.close();
      }
    }

    // ---- the sample flag
    {
      const mock = brainMock([]);
      const { ctx, page } = await open({ mock, samples: true, url: "/brain?sample=all" });
      await page.getByTestId("brain-card").first().waitFor({ timeout: 15000 });
      eq("sample all: a card for every state", [...new Set(await page.getByTestId("brain-card").evaluateAll((els) => els.map((e) => e.getAttribute("data-state"))))].sort(),
         ["cloning", "failed", "indexing", "queued", "ready", "remove_failed", "removing"]);
      await checkWords(page, ck, "sample");
      await page.getByTestId("brain-retry").first().click();
      await page.getByText("This is a sample, so nothing was sent.", { exact: true }).waitFor({ timeout: 5000 });
      ck("sample: Retry sends nothing", mock.retries.length === 0 && mock.deletes.length === 0);
      await shot(page, "brain-repos-sample-all-1280.png");
      await ctx.close();
      const nf = await open({ mock, samples: true, url: "/brain?sample=error", wait: false });
      await nf.page.getByRole("button", { name: "Retry" }).waitFor({ timeout: 15000 });
      ck("sample error: the banner", (await text(nf.page.getByTestId("brain-repos"))).includes("Repos did not load."));
      await nf.ctx.close();
      const ns = await open({ mock, url: "/brain?sample=all" });
      await ns.page.getByTestId("brain-empty").waitFor({ timeout: 15000 });
      ck("a sample name alone, without the flag, shows the live data", (await ns.page.getByTestId("brain-card").count()) === 0);
      await ns.ctx.close();
    }


    // ---- the states that are not the full list, at 1280 and at 390: words, no sideways scroll, a screenshot
    {
      const LONG_ORG = "the-international-association-of-extraordinarily-long-organisation-names";
      const LONG_ONE = `${LONG_ORG}/a-repository-whose-name-keeps-going-and-never-once-offers-a-place-to-break`;
      const LONG_TWO = `${LONG_ORG}/another-equally-unbreakable-repository-name-for-the-queued-card-to-hold`;
      const LONG_URL = "https://api.github.com/repos/the-international-association-of-extraordinarily-long-organisation-names/a-repository/commits?per_page=100&sha=8c41d2e9a7b0c3d4e5f60718293a4b5c6d7e8f90";
      const LONG_READY = `${LONG_ORG}/ready-and-syncing-with-a-long-name-that-goes-on-and-on-and-on`;
      const longRows = () => [
        row({ repo: LONG_ONE, state: "indexing", files_done: 812345, files_total: 1816000, added_at: iso(14 * MIN) }),
        row({ repo: LONG_TWO, state: "queued", place: 3, added_by_name: "Wolfeschlegelsteinhausenbergerdorff Junior the Third of Nowhere in Particular", added_at: iso(9 * MIN) }),
        READY(LONG_READY, {
          sync_failures: 12, sync_error: `GitHub did not answer when the brain asked for the latest push at ${LONG_URL} and gave up after waiting a long time for any reply at all`,
        }),
        row({ repo: `${LONG_ORG}/not-indexed-with-a-very-long-name-as-well-as-a-long-error-text`, state: "failed", error: `GitHub refused access while cloning ${LONG_URL}. Give the GitHub connection read access to this repo and to every repo in the organisation it belongs to, then try again from this page.` }),
        READY(`${LONG_ORG}/removal-gave-up-with-a-long-name-too`, { remove_error: `Still indexing ${LONG_URL}. Try again in a minute, or wait for the index to finish and then remove the repo once more from this page.` }),
      ];
      const fits = async (page, label) => {
        const over = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
        ck(`${label}: no sideways scroll`, over <= 0, String(over));
        const out = await page.locator("body").evaluate((m) => [...m.querySelectorAll("*")].filter((e) => e.getBoundingClientRect().right > window.innerWidth + 0.5 && getComputedStyle(e).position !== "fixed" && !e.closest("[aria-hidden='true']")).map((e) => e.tagName + "." + (e.getAttribute("data-testid") || String(e.className).slice(0, 30))).slice(0, 5));
        ck(`${label}: nothing runs past the screen edge`, out.length === 0, out.join(" "));
      };
      const inView = async (loc, label, w) => {
        const b = await loc.boundingBox();
        ck(`${label}: inside the screen`, b !== null && b.x >= -0.5 && b.x + b.width <= w + 0.5, JSON.stringify(b));
      };
      for (const w of [1280, 390]) {
        const tag = `${w}`;
        const done = async (page, name) => { await checkWords(page, ck, `${name} ${tag}`); await fits(page, `${name} ${tag}`); await shot(page, `brain-repos-${name}-${tag}.png`); };

        // a read key
        { const o = await open({ token: reader.json.secret, mock: brainMock(allRows()), width: w });
          await o.page.getByTestId("brain-card").first().waitFor({ timeout: 15000 });
          ck(`read key ${tag}: no buttons`, (await o.page.getByTestId("brain-card").getByRole("button").count()) === 0 && (await o.page.getByTestId("brain-connect").count()) === 0);
          await done(o.page, "readonly"); await o.ctx.close(); }
        // not set up, empty
        { const o = await open({ mock: brainMock([], { configured: false }), width: w, wait: false });
          await o.page.getByTestId("brain-not-set-up").waitFor({ timeout: 15000 }); await done(o.page, "not-set-up"); await o.ctx.close(); }
        { const o = await open({ mock: brainMock([]), width: w });
          await o.page.getByTestId("brain-empty").waitFor({ timeout: 15000 }); await done(o.page, "empty"); await o.ctx.close(); }
        // a failed read
        { const o = await open({ mock: brainMock([READY("acme/api")], { readStatus: 500 }), width: w, wait: false });
          await o.page.getByRole("button", { name: "Retry" }).waitFor({ timeout: 15000 }); await done(o.page, "error"); await o.ctx.close(); }
        // loading: the page's own read waits (the menu's read comes first and goes through)
        { const slow = brainMock([READY("acme/api")]);
          let release; const gate = new Promise((res) => { release = res; });
          const lo = await browser.newContext({ viewport: { width: w, height: 900 } });
          await lo.addInitScript((t) => { try { localStorage.setItem("tares_token", t); } catch { /* none */ } }, admin.json.secret);
          const lp = await lo.newPage();
          await lp.route(isBrainApi, async (route) => { if (new URL(route.request().url()).pathname === "/api/brain" && slow.reads >= 1) await gate; return slow.handle(route); });
          await lp.goto(d.base + "/brain");
          await lp.getByTestId("brain-loading").waitFor({ timeout: 15000 });
          ck(`loading ${tag}: a skeleton shows`, await lp.getByTestId("brain-loading").isVisible());
          await done(lp, "loading"); release(); await lo.close(); }
        // the remove dialog
        { const o = await open({ mock: brainMock(allRows()), width: w });
          await card(o.page, "acme/api").getByTestId("brain-remove").click();
          const dlg = o.page.getByTestId("brain-remove-dialog"); await dlg.waitFor({ timeout: 5000 });
          await inView(dlg, `remove dialog ${tag}`, w); await done(o.page, "remove-dialog"); await o.ctx.close(); }
        // long text: names that wrap, a long error, a long sync note, a long person's name
        { const rows = longRows();
          const o = await open({ mock: brainMock(rows), width: w });
          await o.page.getByTestId("brain-card").first().waitFor({ timeout: 15000 });
          eq(`long text ${tag}: a card for each row`, await o.page.getByTestId("brain-card").count(), rows.length);
          const cut = await o.page.getByTestId("brain-card").evaluateAll((els) => els.filter((e) => e.scrollWidth > e.clientWidth + 1).length);
          ck(`long text ${tag}: no card is wider inside than out`, cut === 0, String(cut));
          eq(`long text ${tag}: the whole long name is there`, await text(card(o.page, LONG_ONE).getByTestId("brain-name")), LONG_ONE);
          ck(`long text ${tag}: the long error, the sync note and the removal error are all shown`,
             (await text(o.page.getByTestId("brain-error"))).includes(LONG_URL) && (await text(o.page.getByTestId("brain-sync-note"))).includes("gave up after waiting")
             && (await text(o.page.getByTestId("brain-remove-error"))).includes("remove the repo once more"));
          eq(`long text ${tag}: the queued card still says its place`, (await text(card(o.page, LONG_TWO).getByTestId("brain-line"))).slice(0, 14), "Third in line.");
          for (const c of await o.page.getByTestId("brain-card").all()) await inView(c, `long text ${tag} card`, w);
          await done(o.page, "long");
          await o.ctx.close(); }
        // long text in the remove dialog: a long name and a long error from the cell
        { const rows = longRows();
          const m = brainMock(rows);
          m.deleteAnswer = () => ({ status: 409, json: { detail: `Still indexing ${LONG_URL}. Try again in a minute, or wait for the index to finish and then remove the repo once more.` } });
          const o = await open({ mock: m, width: w });
          await card(o.page, LONG_READY).getByTestId("brain-remove").click();
          const dlg = o.page.getByTestId("brain-remove-dialog"); await dlg.waitFor({ timeout: 5000 });
          await dlg.getByTestId("brain-remove-confirm").click();
          await dlg.getByTestId("brain-remove-failed").waitFor({ timeout: 10000 });
          await inView(dlg, `long remove dialog ${tag}`, w);
          const dcut = await dlg.evaluate((e) => e.scrollWidth - e.clientWidth);
          ck(`long remove dialog ${tag}: nothing cut off sideways`, dcut <= 1, String(dcut));
          await done(o.page, "long-remove-dialog"); await o.ctx.close(); }
      }
    }

    // ---- phone width
    {
      const mock = brainMock(allRows());
      const { ctx, page } = await open({ mock, width: 390 });
      await page.getByTestId("brain-card").first().waitFor({ timeout: 15000 });
      const over = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
      ck("390: no sideways scroll", over <= 0, String(over));
      const cols = await page.getByTestId("brain-cards").evaluate((e) => getComputedStyle(e).gridTemplateColumns.split(" ").length);
      ck("390: one card to a row", cols === 1, String(cols));
      const wide = await page.getByTestId("brain-card").evaluateAll((els) => els.filter((e) => e.getBoundingClientRect().right > window.innerWidth).length);
      ck("390: every card fits the screen", wide === 0, String(wide));
      await checkWords(page, ck, "390");
      await shot(page, "brain-repos-all-390.png");
      await cardShots(page, "390");
      await ctx.close();
      const e = await open({ mock: brainMock([]), width: 390 });
      await e.page.getByTestId("brain-empty").waitFor({ timeout: 15000 });
      const o2 = await e.page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
      ck("390, empty: no sideways scroll", o2 <= 0, String(o2));
      await shot(e.page, "brain-repos-empty-390.png");
      await e.ctx.close();
      const w = await open({ mock, width: 1280 });
      const cols2 = await w.page.getByTestId("brain-cards").evaluate((e) => getComputedStyle(e).gridTemplateColumns.split(" ").length);
      ck("1280: cards sit in a grid of several columns", cols2 >= 3, String(cols2));
      await w.ctx.close();
    }
  } catch (e) {
    fail++;
    console.log("  FAIL " + (e && e.stack || e));
  } finally {
    await browser.close();
  }
  console.log(`\n${pass} passed, ${fail} failed`);
  process.exit(fail ? 1 : 0);
})();
