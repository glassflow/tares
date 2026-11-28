/*
 * Browser e2e for the Second Brain Ask page (TR-1206) on a factory cell.
 *  - Unit checks of the page's pure rules (the words, the sources, the indexing line, the errors), run on the
 *    TypeScript as written.
 *  - The Ask menu row opens /brain/ask. The heading and the line under it count the repos on the brain. The page
 *    takes the shell's one read of GET /api/brain: every state counts exactly one call to it (expectOneCall).
 *  - A question sent with Enter posts {question, deep:false}; the answer is markdown (no raw HTML, links open in a
 *    new tab), the sources are numbered mono links to GitHub, "Answered in N seconds." says how long.
 *  - Look deeper asks the same question again with deep:true, shows a waiting state, replaces the answer, and is
 *    gone once the deep answer is there. A failed deep ask leaves the quick answer and the link.
 *  - The repos still indexing are named in one muted line.
 *  - Plain words for each failure: day's cap (quick and deep), the brain down, a network error, no brain (404), no
 *    right to ask (402, 403: no retry line), a blank answer, a question over 2,000 characters (said on the page,
 *    nothing sent). The error words are the page's own and keep the words list.
 *  - An answer's links: only http, https, mailto and relative ones are anchors; a javascript: link stays text.
 *  - Screen readers: a status region says when the answer is waiting and ready; Look deeper keeps focus on the
 *    answer's place.
 *  - No repo ready: the cell's own words, or "No repo is ready yet." naming the failed repos, or "Connect a repo
 *    first." when there are none; a Repos link and Check again. No brain: said plainly. A failed read has Retry.
 *  - A read key can ask. No sideways scroll at 390, with a screenshot of each state there (loading, waiting, deep
 *    waiting, deep answer, deep error, too long, read error). The page's own words keep the words list.
 * The brain's API is answered by the test (the cell here has no GitHub or lore to ask); the daemons are real
 * (tests/licensed.cjs) so the console, the keys and the shell are real.
 * Screenshots of each state at 1280 and 390 go to $SHOTS_DIR when it is set.
 * Run:  npm run build && TARES_E2E_PORT=8880 node tests/brain-ask.e2e.cjs
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

const ROOT = "root-token-brain-ask-e2e";
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
    module: mod, exports: mod.exports, Date, Number, Math, Object, JSON, Promise, RegExp, Array, String, encodeURIComponent,
    require: (name) => { if (name in requires) return requires[name]; throw new Error(`unexpected import ${name}`); },
  });
  return mod.exports;
}
const wordsJson = JSON.parse(fs.readFileSync(path.join(UI_DIR, "src", "editions", "words.json"), "utf8"));
const wordsMod = loadTs("words.ts", { "./words.json": { default: wordsJson } });
const timeMod = loadTs(path.join("..", "lib", "time.ts"));
const T = loadTs(path.join("factory", "brainAskText.ts"), { "../../lib/time": timeMod });

// ---- the cell's rows, as GET /api/brain gives them
const row = (repo, state = "ready", o = {}) => ({ repo, state, files_done: 0, files_total: 0, removing: false, ...o });
const READY = (repo, o = {}) => row(repo, "ready", { files_done: 640, files_total: 640, ...o });

// ---- the answers, as POST /api/brain/ask gives them
const URL_BASE = "https://github.com/glassflow/tares/blob/8c41d2e";
const CITES = [
  { label: 1, kind: "code", title: "ask.py", path: "ee/tares_ee/brain/ask.py", start_line: 120, end_line: 148, url: `${URL_BASE}/ee/tares_ee/brain/ask.py#L120-L148` },
  { label: 2, kind: "doc", title: "Decision 7", path: "docs/decisions/0007.md", start_line: 1, end_line: 1, url: `${URL_BASE}/docs/decisions/0007.md#L1` },
  { label: 3, kind: "note", title: "Why DuckLake", path: null, start_line: null, end_line: null, url: null },
];
const QUICK = {
  answer: "The **brain** syncs each repo every 5 minutes.\n\n- first point\n- second point\n\nSee [the docs](https://example.com/docs) and `ask.py`.\n\n![a picture](https://example.com/p.png)\n\n<img src=\"x\" onerror=\"window.__pwned=1\">\n\n<b id=\"rawb\">raw bold</b>",
  mode: "quick", citations: CITES, repos: ["glassflow/tares"], duration_ms: 6400, notes: ["agent-only note"],
};
const DEEP = { answer: "The deeper answer: the brain syncs on every push and every 5 minutes.", mode: "deep", citations: [CITES[0]], repos: ["glassflow/tares"], duration_ms: 65000 };

// ---- the brain API, answered here
function brainMock(rows, extra = {}) {
  const m = {
    rows, configured: true, readStatus: 200, readGate: null, reads: 0, asks: [], gate: null, answer: null, abort: false, ...extra,
  };
  m.handle = async (route) => {
    const req = route.request();
    const p = new URL(req.url()).pathname;
    if (req.method() === "GET" && p === "/api/brain") {
      m.reads++;
      if (m.readGate) await m.readGate.p;
      if (m.readStatus !== 200) return route.fulfill({ status: m.readStatus, json: { detail: "nope" } });
      if ("readBody" in m) return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(m.readBody) });
      return route.fulfill({ status: 200, json: { configured: m.configured, repos: m.rows } });
    }
    if (req.method() === "POST" && p === "/api/brain/ask") {
      const body = JSON.parse(req.postData() || "{}");
      m.asks.push(body);
      if (m.gate) await m.gate(body, m);
      if (m.abort) return route.abort("connectionfailed");
      const a = m.answer ? await m.answer(body, m) : { status: 200, json: body.deep ? DEEP : QUICK };
      return route.fulfill(a);
    }
    return route.fulfill({ status: 404, json: { detail: "unexpected" } });
  };
  return m;
}
const isBrainApi = (u) => u.pathname === "/api/brain" || u.pathname.startsWith("/api/brain/");
const deferred = () => { let go; const p = new Promise((r) => { go = r; }); return { p, go }; };

(async () => {
  // ---- 0. the pure rules
  {
    const r = (repo, state, o = {}) => row(repo, state, o);
    eq("subtitle: several repos", T.subtitle(3), "Ask about the code and decisions in 3 repos. The crew asks the same brain.");
    eq("subtitle: one repo is singular", T.subtitle(1), "Ask about the code and decisions in 1 repo. The crew asks the same brain.");
    ck("subtitle: no repo does not say 0", !T.subtitle(0).includes("0"));
    eq("repoCount", [T.repoCount(1), T.repoCount(2), T.repoCount(0)], ["1 repo", "2 repos", "0 repos"]);
    const rows = [r("a/ready", "ready"), r("a/q", "queued"), r("a/c", "cloning"), r("a/i", "indexing"), r("a/f", "failed"), r("a/gone", "indexing", { removing: true }), r("a/gone2", "ready", { removing: true })];
    eq("reposOnBrain leaves out repos being removed", T.reposOnBrain(rows).map((x) => x.repo), ["a/ready", "a/q", "a/c", "a/i", "a/f"]);
    eq("readyRepos: only ready, not removing", T.readyRepos(rows).map((x) => x.repo), ["a/ready"]);
    eq("indexingNames: queued, cloning and indexing, not failed or removing", T.indexingNames(rows), ["a/q", "a/c", "a/i"]);
    eq("indexingLine: one", T.indexingLine(["glassflow/tares"]), "glassflow/tares is still indexing, so its newest files may be missing from answers.");
    eq("indexingLine: two", T.indexingLine(["a/x", "b/y"]), "a/x and b/y are still indexing, so their newest files may be missing from answers.");
    eq("indexingLine: three", T.indexingLine(["a/x", "b/y", "c/z"]), "a/x, b/y and c/z are still indexing, so their newest files may be missing from answers.");
    eq("indexingLine: none", T.indexingLine([]), "");
    eq("nameSeparator", [T.nameSeparator(0, 3), T.nameSeparator(1, 3), T.nameSeparator(2, 3), T.nameSeparator(0, 2), T.nameSeparator(0, 1)], [", ", " and ", "", " and ", ""]);
    eq("questionLength: counts what a person typed, trimmed, an emoji as one", [T.questionLength("  hi  "), T.questionLength("a\u{1F600}b"), T.questionLength("x".repeat(2001)), T.questionLength("   ")], [2, 3, 2001, 0]);
    eq("MAX_QUESTION", T.MAX_QUESTION, 2000);
    eq("answeredWords: from the cell's milliseconds", [T.answeredWords(6400, 1), T.answeredWords(1000, 1), T.answeredWords(65000, 1)], ["Answered in 6 seconds.", "Answered in 1 second.", "Answered in 1 minute, 5 seconds."]);
    eq("answeredWords: the page's own clock when the cell did not say", [T.answeredWords(undefined, 3500), T.answeredWords(null, 3500), T.answeredWords(-5, 3500)], Array(3).fill("Answered in 3 seconds."));
    eq("sourceLabel: the brain's label, else the place", [T.sourceLabel({ label: 7 }, 0), T.sourceLabel({ label: null }, 1), T.sourceLabel({}, 2), T.sourceLabel({ label: " 4 " }, 0)], ["[7]", "[2]", "[3]", "[4]"]);
    eq("lineRange", [T.lineRange({ start_line: 12, end_line: 20 }), T.lineRange({ start_line: 12, end_line: 12 }), T.lineRange({ start_line: 12, end_line: null }), T.lineRange({ start_line: null, end_line: 5 }), T.lineRange({ start_line: 0, end_line: 5 })], ["12-20", "12", "12", "", ""]);
    eq("repoOfUrl", [T.repoOfUrl(CITES[0].url), T.repoOfUrl(null), T.repoOfUrl("https://example.com/x")], ["tares", "", ""]);
    eq("sourceText: repo, path and lines", T.sourceText(CITES[0], ["glassflow/tares"]), "tares/ee/tares_ee/brain/ask.py:120-148");
    eq("sourceText: one line", T.sourceText(CITES[1], ["glassflow/tares"]), "tares/docs/decisions/0007.md:1");
    eq("sourceText: no file says the title", T.sourceText(CITES[2], []), "Why DuckLake");
    eq("sourceText: no file and no title", T.sourceText({ path: null, title: " " }, []), "Untitled");
    eq("sourceText: no url and several repos names no repo", T.sourceText({ path: "a/b.py", start_line: 3, end_line: 4, url: null }, ["x/one", "x/two"]), "a/b.py:3-4");
    eq("sourceText: no url and one repo names it", T.sourceText({ path: "a/b.py", start_line: 3, end_line: 4, url: null }, ["x/one"]), "one/a/b.py:3-4");
    eq("sourceHref: the brain's own address", T.sourceHref(CITES[0], ["glassflow/tares"]), CITES[0].url);
    eq("sourceHref: no address, one repo, a file", T.sourceHref({ path: "a b/c.py", start_line: 3, end_line: 9, url: null }, ["x/one"]), "https://github.com/x/one/blob/HEAD/a%20b/c.py#L3-L9");
    eq("sourceHref: one line", T.sourceHref({ path: "c.py", start_line: 3, end_line: 3, url: null }, ["x/one"]), "https://github.com/x/one/blob/HEAD/c.py#L3");
    eq("sourceHref: no address and several repos is not a link", T.sourceHref({ path: "c.py", start_line: 3, end_line: 3, url: null }, ["x/one", "x/two"]), "");
    eq("sourceHref: a note is not a link", T.sourceHref(CITES[2], ["x/one"]), "");
    eq("sourceHref: a javascript address is not a link", T.sourceHref({ path: null, url: "javascript:alert(1)" }, []), "");
    eq("plainError: capital and full stop", T.plainError("could not reach the brain", "x"), "Could not reach the brain.");
    eq("plainError: keeps a full stop, falls back", [T.plainError("Done.", "x"), T.plainError("  ", "Fallback."), T.plainError(undefined, "Fallback.")], ["Done.", "Fallback.", "Fallback."]);
    const quickCap = "This cell has used its 200 answers for today. Search instead, or try again after midnight UTC.";
    const deepCap = "This cell has used its 20 deep answers for today. Ask without looking deeper, or try again after midnight UTC.";
    eq("askErrorWords: quick cap names the cap", T.askErrorWords({ status: 429, message: quickCap }, false), "This cell has used its 200 answers for today. Try again after midnight UTC.");
    eq("askErrorWords: deep cap names the cap", T.askErrorWords({ status: 429, message: deepCap }, true), "This cell has used its 20 deep answers for today. Try again after midnight UTC.");
    eq("askErrorWords: a cap with other words still says which", [T.askErrorWords({ status: 429, message: "daily limit for today reached" }, false), T.askErrorWords({ status: 429, message: "daily limit for today reached" }, true)], ["This cell has used its answers for today. Try again after midnight UTC.", "This cell has used its deep answers for today. Try again after midnight UTC."]);
    eq("askErrorWords: the brain's own 429", T.askErrorWords({ status: 429, message: "slow down" }, false), "Slow down.");
    eq("askErrorWords: 502 keeps the cell's words and says to retry", T.askErrorWords({ status: 502, message: "Could not reach the brain." }, false), "Could not reach the brain. Try again in a moment.");
    eq("askErrorWords: 502 without words", T.askErrorWords({ status: 502, message: "" }, false), T.NOT_ANSWERED_WORDS);
    eq("askErrorWords: 404 Not Found is no brain", [T.askErrorWords({ status: 404, message: "Not Found" }, false), T.askErrorWords({ status: 404, message: "" }, false)], [T.NOT_SET_UP_WORDS, T.NOT_SET_UP_WORDS]);
    eq("askErrorWords: a 404 with words is the brain's own, not 'not set up'", T.askErrorWords({ status: 404, message: "a/b is not connected to the brain" }, false), "A/b is not connected to the brain. Try again in a moment.");
    eq("askErrorWords: 402 and 403 have their own words", [T.askErrorWords({ status: 402, message: "x" }, false), T.askErrorWords({ status: 403, message: "x" }, false)], [T.NOT_AVAILABLE_WORDS, T.NO_ACCESS_WORDS]);
    ck("askErrorWords: 402 and 403 have no retry line", ![402, 403].some((status) => /try again/i.test(T.askErrorWords({ status, message: "x" }, true))));
    ck("askErrorWords: a daily cap says after midnight UTC, not tomorrow", !/tomorrow/i.test(T.askErrorWords({ status: 429, message: quickCap }, false)) && /after midnight UTC/.test(T.askErrorWords({ status: 429, message: quickCap }, false)));
    eq("answerText: blank or not text is empty", [T.answerText("  a "), T.answerText("   "), T.answerText(null), T.answerText(undefined), T.answerText(5), T.answerText({})], ["a", "", "", "", "", ""]);
    eq("safeHref: http, https, mailto and relative stay", [T.safeHref("https://a.co/x"), T.safeHref("HTTP://a.co"), T.safeHref("mailto:a@b.co"), T.safeHref("/brain"), T.safeHref("./x"), T.safeHref("#top"), T.safeHref("docs/x.md")], ["https://a.co/x", "HTTP://a.co", "mailto:a@b.co", "/brain", "./x", "#top", "docs/x.md"]);
    eq("safeHref: javascript, data, vbscript, a hidden scheme and a protocol-relative address (also with backslashes) are dropped", [T.safeHref("javascript:alert(1)"), T.safeHref(" JaVaScRiPt:alert(1)"), T.safeHref("java\nscript:alert(1)"), T.safeHref("data:text/html,x"), T.safeHref("vbscript:x"), T.safeHref("//evil.example/x"), T.safeHref("/\\evil.example/x"), T.safeHref("\\\\evil.example/x"), T.safeHref(""), T.safeHref(null)], ["", "", "", "", "", "", "", "", "", ""]);
    eq("failedNames: failed, not removing", T.failedNames([row("a/f", "failed"), row("b/g", "failed", { removing: true }), row("c/r", "ready")]), ["a/f"]);
    eq("askErrorWords: 400 keeps the cell's words", T.askErrorWords({ status: 400, message: "question: at most 2,000 characters" }, false), "Question: at most 2,000 characters.");
    eq("askErrorWords: no status is the network", T.askErrorWords({ message: "Failed to fetch" }, false), T.NETWORK_WORDS);
    eq("askErrorWords: an unknown status", T.askErrorWords({ status: 500, message: "boom" }, false), T.NOT_ANSWERED_WORDS);
    const copy = Object.entries(T).filter(([, v]) => typeof v === "string").map(([, v]) => v).join(" ");
    ck("the page's fixed words have no em dash", !copy.includes("\u2014"));
  }

  // ---- the daemons: licensed with keys
  let d;
  try {
    d = await startDaemon({ port: PORT, edition: "factory", licence: "valid", seats: 50, auth: ROOT });
  } catch (e) { console.log("FAIL daemon did not start: " + e.message); process.exit(1); }
  process.on("exit", () => { d.stop(); });
  const root = { authorization: `Bearer ${ROOT}` };
  const admin = await call(d.base, "POST", "/api/keys", { name: "ada-laptop", scopes: ["admin"], person: { email: "ada@example.com", name: "Ada Lovelace" } }, root);
  const reader = await call(d.base, "POST", "/api/keys", { name: "grace-laptop", scopes: ["read"], person: { email: "grace@example.com", name: "Grace Hopper" } }, root);
  ck("keys made", admin.status === 201 && reader.status === 201, `${admin.status} ${reader.status}`);

  const browser = await chromium.launch(process.env.TARES_E2E_CHROME ? { executablePath: process.env.TARES_E2E_CHROME } : {});
  const errors = [];
  const open = async (opts = {}) => {
    // `one`: the screen's one read of GET /api/brain (the shell's, which the page takes) is counted once the
    // page has settled; `label` names the state in the check. A caller that holds the read back checks later.
    const { token = admin.json.secret, width = 1280, mock, url = "/brain/ask", wait = "brain-ask-question", one: countOne = true, label = "the page's read" } = opts;
    const ctx = await browser.newContext({ viewport: { width, height: 900 } });
    await ctx.addInitScript((t) => { try { if (t) localStorage.setItem("tares_token", t); } catch { /* none */ } }, token);
    const page = await ctx.newPage();
    page.on("pageerror", (e) => errors.push(String(e)));
    if (mock) await page.route(isBrainApi, mock.handle);
    const one = expectOneCall(page, "/api/brain");
    await page.goto(d.base + url);
    if (wait) await page.getByTestId(wait).waitFor({ timeout: 15000 });
    if (wait && countOne) await one(ck, `${label} at ${width}`);
    return { ctx, page, one };
  };
  const text = async (loc) => (await loc.innerText()).replace(/\s+/g, " ").trim();
  const shot = async (page, name) => { if (SHOTS) await page.screenshot({ path: path.join(SHOTS, name), fullPage: true }); };
  const ask = async (page, q) => { await page.getByTestId("brain-ask-question").fill(q); await page.getByTestId("brain-ask-question").press("Enter"); };
  const fits = async (page, label) => {
    const over = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    ck(`${label}: no sideways scroll`, over <= 0, String(over));
    const out = await page.locator("body").evaluate((m) => [...m.querySelectorAll("*")].filter((e) => e.getBoundingClientRect().right > window.innerWidth + 0.5 && getComputedStyle(e).position !== "fixed" && !e.closest("[aria-hidden='true']") && !e.closest("pre") && !e.closest("table")).map((e) => e.tagName + "." + (e.getAttribute("data-testid") || String(e.className).slice(0, 30))).slice(0, 5));
    ck(`${label}: nothing runs past the screen edge`, out.length === 0, out.join(" "));
  };
  const THREE = () => [READY("glassflow/tares"), READY("glassflow/lore"), row("glassflow/new-repo", "indexing", { files_done: 10, files_total: 100 })];

  try {
    // ---- the menu row opens the page; the heading and the line under it
    {
      const mock = brainMock(THREE());
      const { ctx, page } = await open({ mock, url: "/projects", wait: null });
      await page.getByTestId("menu-brain-ask").waitFor({ timeout: 15000 });
      await page.getByTestId("menu-brain-ask").click();
      await page.getByTestId("brain-ask-question").waitFor({ timeout: 15000 });
      await page.waitForLoadState("networkidle");
      eq("the shell's read is the page's read: one GET /api/brain from the menu to the page", mock.reads, 1);
      ck("the Ask menu row opens /brain/ask", new URL(page.url()).pathname === "/brain/ask", page.url());
      eq("the heading", await text(page.locator("h1")), "Ask");
      eq("the line under it counts the repos on the brain", await text(page.getByTestId("brain-ask-subtitle")), "Ask about the code and decisions in 3 repos. The crew asks the same brain.");
      eq("the question box has the label Question", await page.getByLabel("Question").count(), 1);
      eq("an Ask button", await page.getByRole("button", { name: "Ask", exact: true }).count(), 1);
      ck("no mode buttons: the only buttons are Ask", (await page.getByTestId("brain-ask").getByRole("button").count()) === 1);
      ck("Ask is off while the box is empty", await page.getByTestId("brain-ask-submit").isDisabled());
      await page.getByTestId("brain-ask-question").fill("   ");
      ck("Ask is off for a box of spaces", await page.getByTestId("brain-ask-submit").isDisabled());
      await ctx.close();
      const one = await open({ mock: brainMock([READY("glassflow/tares")]) });
      eq("one repo is singular", await text(one.page.getByTestId("brain-ask-subtitle")), "Ask about the code and decisions in 1 repo. The crew asks the same brain.");
      await one.ctx.close();
      const gone = await open({ mock: brainMock([READY("a/b"), READY("a/c", { removing: true })]) });
      eq("a repo being removed is not counted", await text(gone.page.getByTestId("brain-ask-subtitle")), "Ask about the code and decisions in 1 repo. The crew asks the same brain.");
      await gone.ctx.close();
    }

    // ---- a quick answer
    {
      const mock = brainMock(THREE());
      const { ctx, page } = await open({ mock });
      await ask(page, "  How does the brain sync?  ");
      await page.getByTestId("brain-ask-answer").waitFor({ timeout: 15000 });
      eq("one POST, the trimmed question, deep false, nothing else", mock.asks, [{ question: "How does the brain sync?", deep: false }]);
      const ans = page.getByTestId("brain-ask-text");
      ck("the answer is markdown: bold, a list, code", (await ans.locator("strong").innerText()) === "brain" && (await ans.locator("ul li").count()) === 2 && (await ans.locator("code").innerText()) === "ask.py");
      const link = ans.locator("a", { hasText: "the docs" });
      eq("a link in the answer opens in a new tab and says noopener", [await link.getAttribute("href"), await link.getAttribute("target"), /noopener/.test((await link.getAttribute("rel")) || "")], ["https://example.com/docs", "_blank", true]);
      eq("raw HTML in the answer is not rendered as elements", [await ans.locator("img").count(), await ans.locator("#rawb").count(), await page.evaluate(() => window.__pwned === undefined)], [0, 0, true]);
      ck("the answer is exempt from the words rule", (await ans.getAttribute("data-words-exempt")) !== null);
      const labels = await page.getByTestId("brain-ask-source-label").allInnerTexts();
      eq("the sources are numbered", labels, ["[1]", "[2]", "[3]"]);
      const links = page.getByTestId("brain-ask-source-link");
      eq("the sources read as repo, file and lines", await links.allInnerTexts(), ["tares/ee/tares_ee/brain/ask.py:120-148", "tares/docs/decisions/0007.md:1", "Why DuckLake"]);
      eq("a source with a file links to GitHub at the lines, in a new tab", [await links.nth(0).getAttribute("href"), await links.nth(0).getAttribute("target"), /noopener/.test((await links.nth(0).getAttribute("rel")) || "")], [CITES[0].url, "_blank", true]);
      eq("a one-line source", await links.nth(1).getAttribute("href"), CITES[1].url);
      eq("a note with no file is plain text, not a link", await links.nth(2).evaluate((e) => e.tagName), "SPAN");
      ck("a source is in a monospace face", (await links.nth(0).evaluate((e) => getComputedStyle(e).fontFamily)).toLowerCase().includes("mono"));
      eq("how long it took", await text(page.getByTestId("brain-ask-took")), "Answered in 6 seconds.");
      eq("Look deeper is offered, as one link-button", [await page.getByTestId("brain-ask-deeper").innerText(), await page.getByRole("button", { name: "Look deeper" }).count()], ["Look deeper", 1]);
      eq("a screen reader hears that the answer is ready", [await page.getByTestId("brain-ask-live").getAttribute("role"), await text(page.getByTestId("brain-ask-live"))], ["status", "Answer ready. Answered in 6 seconds."]);
      ck("the agent's own notes are not shown", !(await page.getByTestId("brain-ask").innerText()).includes("agent-only note"));
      ck("the repo still indexing is named under the answer", (await text(page.getByTestId("brain-ask-indexing"))).startsWith("glassflow/new-repo is still indexing"));
      await checkWords(page, ck, "quick answer 1280");
      await fits(page, "quick answer 1280");
      await shot(page, "brain-ask-answer-1280.png");
      await ctx.close();
    }

    // ---- the shell's ask field: /brain/ask?q=<question> puts the question in the box and asks it, once
    {
      const mock = brainMock(THREE());
      const { ctx, page } = await open({ mock, url: "/projects", wait: null });
      const field = page.getByTestId("edition-search");
      await field.waitFor({ timeout: 15000 });
      await field.fill("How does the brain sync?");
      await field.press("Enter");
      await page.getByTestId("brain-ask-answer").waitFor({ timeout: 15000 });
      ck("the ask field lands on /brain/ask", new URL(page.url()).pathname === "/brain/ask", page.url());
      eq("the question is asked once, quick", mock.asks, [{ question: "How does the brain sync?", deep: false }]);
      eq("and waits in the box", await page.getByTestId("brain-ask-question").inputValue(), "How does the brain sync?");
      ck("q leaves the address, so a reload does not ask again", !new URL(page.url()).searchParams.has("q"), page.url());
      await page.reload();
      await page.getByTestId("brain-ask-question").waitFor({ timeout: 15000 });
      await page.waitForLoadState("networkidle");
      eq("a reload asks nothing more", mock.asks.length, 1);
      await ctx.close();
    }
    {
      // no repo ready: nothing is asked and q stays in the address for when one is
      const mock = brainMock([row("glassflow/new-repo", "indexing", { files_done: 10, files_total: 100 })]);
      const { ctx, page } = await open({ mock, url: "/brain/ask?q=hello", wait: "brain-ask-empty" });
      await page.waitForLoadState("networkidle");
      eq("?q= with no repo ready asks nothing", mock.asks.length, 0);
      ck("and keeps q", new URL(page.url()).searchParams.get("q") === "hello", page.url());
      await ctx.close();
    }
    {
      // a question over the limit waits in the box with its warning and is not sent
      const mock = brainMock(THREE());
      const long = "x".repeat(2100);
      const { ctx, page } = await open({ mock, url: `/brain/ask?q=${long}` });
      await page.getByTestId("brain-ask-too-long").waitFor({ timeout: 15000 });
      eq("?q= over the limit is not asked", mock.asks.length, 0);
      eq("and waits in the box", (await page.getByTestId("brain-ask-question").inputValue()).length, 2100);
      await ctx.close();
    }

    // ---- Look deeper
    {
      const gate = deferred();
      const mock = brainMock(THREE(), { gate: async (body) => { if (body.deep) await gate.p; } });
      const { ctx, page } = await open({ mock });
      await ask(page, "How does the brain sync?");
      await page.getByTestId("brain-ask-answer").waitFor({ timeout: 15000 });
      await page.getByTestId("brain-ask-question").fill("something else typed since");
      await page.getByTestId("brain-ask-deeper").click();
      await page.getByTestId("brain-ask-waiting").waitFor({ timeout: 15000 });
      const focused = () => page.evaluate(() => document.activeElement?.getAttribute("data-testid") || document.activeElement?.tagName);
      eq("focus stays on the answer's place when Look deeper goes away, not on the page", await focused(), "brain-ask-region");
      eq("the deep POST asks the question the answer belongs to, with deep true", mock.asks[1], { question: "How does the brain sync?", deep: true });
      ck("while it runs the quick answer is gone", (await page.getByTestId("brain-ask-answer").count()) === 0 && !(await page.getByTestId("brain-ask").innerText()).includes("syncs each repo"));
      const wait = await text(page.getByTestId("brain-ask-waiting-words"));
      ck("the deep wait says it can take a minute", /can take a minute/i.test(wait), wait);
      eq("the wait is announced once, by the page's status region", [await page.getByTestId("brain-ask-waiting").locator("[role=status]").count(), await text(page.getByTestId("brain-ask-live"))], [0, "Asking the brain to look deeper... This can take a minute."]);
      ck("Ask is off while it runs", await page.getByTestId("brain-ask-submit").isDisabled());
      await checkWords(page, ck, "deep waiting 1280");
      await shot(page, "brain-ask-deep-waiting-1280.png");
      gate.go();
      await page.getByTestId("brain-ask-answer").waitFor({ timeout: 15000 });
      eq("the deep answer replaces it", [await text(page.getByTestId("brain-ask-text")), await page.getByTestId("brain-ask-answer").getAttribute("data-deep")], [DEEP.answer, "true"]);
      eq("its time: 65 seconds is a minute and 5 seconds", await text(page.getByTestId("brain-ask-took")), "Answered in 1 minute, 5 seconds.");
      eq("the deep answer is announced and focus is still on its place", [await text(page.getByTestId("brain-ask-live")), await focused()], ["Answer ready. Answered in 1 minute, 5 seconds.", "brain-ask-region"]);
      eq("Look deeper is gone once the deep answer is there", await page.getByTestId("brain-ask-deeper").count(), 0);
      eq("the sources are the deep answer's", await page.getByTestId("brain-ask-source").count(), 1);
      eq("the box keeps what was typed", await page.getByTestId("brain-ask-question").inputValue(), "something else typed since");
      eq("two POSTs in all", mock.asks.length, 2);
      await checkWords(page, ck, "deep answer 1280");
      await shot(page, "brain-ask-deep-answer-1280.png");
      // a new question starts over with a quick answer, and Look deeper is back
      await ask(page, "Another question");
      await page.waitForFunction(() => document.querySelector("[data-testid=brain-ask-answer]")?.getAttribute("data-deep") === "false", null, { timeout: 15000 });
      eq("a new question is a quick ask", mock.asks[2], { question: "Another question", deep: false });
      eq("and Look deeper is offered again", await page.getByTestId("brain-ask-deeper").count(), 1);
      await ctx.close();
    }

    // ---- a deep ask that fails leaves the quick answer and the link
    {
      const mock = brainMock(THREE(), { answer: async (body) => (body.deep ? { status: 502, json: { detail: "The brain did not answer within 120 seconds." } } : { status: 200, json: QUICK }) });
      const { ctx, page } = await open({ mock });
      await ask(page, "Why?");
      await page.getByTestId("brain-ask-answer").waitFor({ timeout: 15000 });
      await page.getByTestId("brain-ask-deeper").click();
      await page.getByTestId("brain-ask-error").waitFor({ timeout: 15000 });
      eq("the error says what happened and to try again", await text(page.getByTestId("brain-ask-error")), "The brain did not answer within 120 seconds. Try again in a moment.");
      ck("the quick answer is still there, with Look deeper", (await page.getByTestId("brain-ask-text").count()) === 1 && (await page.getByTestId("brain-ask-deeper").count()) === 1);
      eq("focus did not fall to the page body", await page.evaluate(() => document.activeElement?.getAttribute("data-testid")), "brain-ask-region");
      await checkWords(page, ck, "deep failed 1280");
      await shot(page, "brain-ask-deep-error-1280.png");
      await ctx.close();
    }

    // ---- the indexing line
    {
      const lines = async (rows) => {
        const o = await open({ mock: brainMock(rows) });
        const n = await o.page.getByTestId("brain-ask-indexing").count();
        const t = n ? await text(o.page.getByTestId("brain-ask-indexing")) : "";
        await o.ctx.close();
        return t;
      };
      eq("one repo indexing", await lines([READY("a/ready"), row("glassflow/tares", "indexing")]), "glassflow/tares is still indexing, so its newest files may be missing from answers.");
      eq("two repos, queued and cloning", await lines([READY("a/ready"), row("a/x", "queued"), row("b/y", "cloning")]), "a/x and b/y are still indexing, so their newest files may be missing from answers.");
      eq("three repos", await lines([READY("a/ready"), row("a/x", "queued"), row("b/y", "cloning"), row("c/z", "indexing")]), "a/x, b/y and c/z are still indexing, so their newest files may be missing from answers.");
      eq("nothing indexing, no line", await lines([READY("a/ready"), row("a/f", "failed")]), "");
      eq("a repo being removed is not named", await lines([READY("a/ready"), row("a/x", "indexing", { removing: true })]), "");
      const o = await open({ mock: brainMock(THREE()) });
      const tone = await o.page.getByTestId("brain-ask-indexing").evaluate((e) => ({ line: getComputedStyle(e).color, heading: getComputedStyle(document.querySelector("h1")).color, size: parseFloat(getComputedStyle(e).fontSize) }));
      ck("the line is muted: a softer colour than the heading, and small", tone.line !== tone.heading && tone.size <= 13.5, JSON.stringify(tone));
      await o.ctx.close();
    }

    // ---- failures, in plain words
    {
      const failure = async (label, answer, want, opts = {}) => {
        const mock = brainMock(THREE(), { answer, abort: opts.abort });
        const { ctx, page } = await open({ mock });
        await ask(page, "A question");
        await page.getByTestId("brain-ask-error").waitFor({ timeout: 15000 });
        eq(label, await text(page.getByTestId("brain-ask-error")), want);
        ck(`${label}: no answer shown, and the box is usable again`, (await page.getByTestId("brain-ask-answer").count()) === 0 && !(await page.getByTestId("brain-ask-submit").isDisabled()));
        await checkWords(page, ck, label);
        await ctx.close();
        return page;
      };
      await failure("the day's quick cap", async () => ({ status: 429, json: { detail: "This cell has used its 200 answers for today. Search instead, or try again after midnight UTC." } }), "This cell has used its 200 answers for today. Try again after midnight UTC.");
      await failure("the brain is down (502)", async () => ({ status: 502, json: { detail: "Could not reach the brain." } }), "Could not reach the brain. Try again in a moment.");
      await failure("a network error", null, "Could not reach the cell. Check your connection and try again.", { abort: true });
      await failure("no brain on the cell (404 on ask)", async () => ({ status: 404, json: { detail: "Not Found" } }), "The second brain is not set up on this cell.");
      await failure("a 404 with the brain's own words is not 'not set up'", async () => ({ status: 404, json: { detail: "glassflow/x is not connected to the brain" } }), "Glassflow/x is not connected to the brain. Try again in a moment.");
      await failure("no right to the brain (402)", async () => ({ status: 402, json: { detail: "payment required" } }), "The second brain is not available on this cell.");
      await failure("a key that may not ask (403)", async () => ({ status: 403, json: { detail: "forbidden" } }), "You do not have access to the brain. Ask an admin of this cell.");
      await failure("a blank answer", async () => ({ status: 200, json: { answer: "   \n ", citations: [], repos: ["glassflow/tares"] } }), "No answer came back. Try asking again.");
      await failure("an answer that is not text", async () => ({ status: 200, json: { answer: null, citations: null, repos: null } }), "No answer came back. Try asking again.");
      await failure("the cell refuses a long question (400)", async () => ({ status: 400, json: { detail: "question: at most 2,000 characters" } }), "Question: at most 2,000 characters.");
      // the deep cap shows on a deep ask, with the quick answer kept
      const mock = brainMock(THREE(), { answer: async (b) => (b.deep ? { status: 429, json: { detail: "This cell has used its 20 deep answers for today. Ask without looking deeper, or try again after midnight UTC." } } : { status: 200, json: QUICK }) });
      const { ctx, page } = await open({ mock });
      await ask(page, "Q");
      await page.getByTestId("brain-ask-deeper").waitFor({ timeout: 15000 });
      await page.getByTestId("brain-ask-deeper").click();
      await page.getByTestId("brain-ask-error").waitFor({ timeout: 15000 });
      eq("the day's deep cap", await text(page.getByTestId("brain-ask-error")), "This cell has used its 20 deep answers for today. Try again after midnight UTC.");
      ck("the quick answer stays", (await page.getByTestId("brain-ask-text").count()) === 1);
      await page.getByTestId("brain-ask-deeper").waitFor();
      await ctx.close();
      // a failure clears when the next ask starts
      const m2 = brainMock(THREE(), { answer: async () => ({ status: 502, json: { detail: "Could not reach the brain." } }) });
      const o = await open({ mock: m2 });
      await ask(o.page, "One");
      await o.page.getByTestId("brain-ask-error").waitFor({ timeout: 15000 });
      m2.answer = null;
      await ask(o.page, "Two");
      await o.page.getByTestId("brain-ask-answer").waitFor({ timeout: 15000 });
      eq("a new ask clears the last error", await o.page.getByTestId("brain-ask-error").count(), 0);
      await o.ctx.close();
    }

    // ---- the 2000 limit, said on the page, nothing sent
    {
      const mock = brainMock(THREE());
      const { ctx, page } = await open({ mock });
      await page.getByTestId("brain-ask-question").fill("x".repeat(2000));
      ck("2000 characters is allowed", !(await page.getByTestId("brain-ask-submit").isDisabled()) && (await page.getByTestId("brain-ask-too-long").count()) === 0);
      await page.getByTestId("brain-ask-question").fill("x".repeat(2001));
      eq("2001 characters says so in plain words", await text(page.getByTestId("brain-ask-too-long")), "That question is too long. Keep it to 2,000 characters or fewer.");
      ck("the message is an alert", (await page.getByTestId("brain-ask-too-long").getAttribute("role")) === "alert");
      ck("Ask is off", await page.getByTestId("brain-ask-submit").isDisabled());
      await page.getByTestId("brain-ask-question").press("Enter");
      await page.waitForTimeout(300);
      eq("Enter sends nothing", mock.asks.length, 0);
      await checkWords(page, ck, "too long 1280");
      await shot(page, "brain-ask-too-long-1280.png");
      await page.getByTestId("brain-ask-question").fill("x".repeat(1999) + "\u{1F600}");
      ck("an emoji counts as one character", (await page.getByTestId("brain-ask-too-long").count()) === 0);
      await ctx.close();
    }

    // ---- one ask at a time
    {
      const gate = deferred();
      const mock = brainMock(THREE(), { gate: async () => { await gate.p; } });
      const { ctx, page } = await open({ mock });
      await ask(page, "First");
      await page.getByTestId("brain-ask-waiting").waitFor({ timeout: 15000 });
      eq("the quick wait is the plain words", await text(page.getByTestId("brain-ask-waiting-words")), "Asking the brain...");
      await page.getByTestId("brain-ask-question").press("Enter");
      await page.getByTestId("brain-ask-question").press("Enter");
      await page.waitForTimeout(300);
      eq("Enter while it runs sends nothing more", mock.asks.length, 1);
      await checkWords(page, ck, "waiting 1280");
      await fits(page, "waiting 1280");
      await shot(page, "brain-ask-waiting-1280.png");
      gate.go();
      await page.getByTestId("brain-ask-answer").waitFor({ timeout: 15000 });
      await ctx.close();
    }

    // ---- links in an answer: only http, https, mailto and relative ones are anchors
    {
      const links = { answer: "Click [x](javascript:alert(1)) or [y](JaVaScRiPt:alert(2)) or [z](data:text/html,hi) then [web](https://example.com/a), [here](/brain) and [mail](mailto:a@b.co).", citations: [], repos: ["glassflow/tares"], duration_ms: 1000 };
      const mock = brainMock(THREE(), { answer: async () => ({ status: 200, json: links }) });
      const { ctx, page } = await open({ mock });
      await ask(page, "Links?");
      await page.getByTestId("brain-ask-answer").waitFor({ timeout: 15000 });
      const ans = page.getByTestId("brain-ask-text");
      eq("only the safe links are anchors", await ans.locator("a").evaluateAll((as) => as.map((a) => a.getAttribute("href"))), ["https://example.com/a", "/brain", "mailto:a@b.co"]);
      eq("no anchor holds a javascript or data address, or an empty one", await ans.locator("a[href^='javascript' i], a[href^='data' i], a[href='']").count(), 0);
      eq("an unsafe link stays as its text", await text(ans), "Click x or y or z then web, here and mail.");
      await ctx.close();
      // a deep answer that comes back blank keeps the quick answer and says so
      const m2 = brainMock(THREE(), { answer: async (b) => (b.deep ? { status: 200, json: { answer: " ", citations: [] } } : { status: 200, json: QUICK }) });
      const o = await open({ mock: m2 });
      await ask(o.page, "Q");
      await o.page.getByTestId("brain-ask-deeper").waitFor({ timeout: 15000 });
      await o.page.getByTestId("brain-ask-deeper").click();
      await o.page.getByTestId("brain-ask-error").waitFor({ timeout: 15000 });
      eq("a blank deep answer says so", await text(o.page.getByTestId("brain-ask-error")), "No answer came back. Try asking again.");
      ck("and the quick answer stays, with Look deeper", (await o.page.getByTestId("brain-ask-text").count()) === 1 && (await o.page.getByTestId("brain-ask-deeper").count()) === 1);
      await o.ctx.close();
    }

    // ---- sources without an address
    {
      const answer = { answer: "Plain.", repos: ["x/one"], duration_ms: 2000, citations: [{ label: 1, path: "src/a b.py", start_line: 5, end_line: 5, url: null }, { label: 2, path: null, title: "A note", start_line: null, end_line: null, url: null }] };
      const mock = brainMock([READY("x/one")], { answer: async () => ({ status: 200, json: answer }) });
      const { ctx, page } = await open({ mock });
      await ask(page, "Q");
      await page.getByTestId("brain-ask-answer").waitFor({ timeout: 15000 });
      const links = page.getByTestId("brain-ask-source-link");
      eq("one repo and a file: the file on its main branch", [await links.nth(0).getAttribute("href"), await text(links.nth(0))], ["https://github.com/x/one/blob/HEAD/src/a%20b.py#L5", "one/src/a b.py:5"]);
      eq("a note stays plain text", [await links.nth(1).evaluate((e) => e.tagName), await text(links.nth(1))], ["SPAN", "A note"]);
      eq("no indexing line when every repo is ready", await page.getByTestId("brain-ask-indexing").count(), 0);
      await ctx.close();
      const none = brainMock([READY("x/one")], { answer: async () => ({ status: 200, json: { answer: "Nothing cited.", repos: ["x/one"], citations: [] } }) });
      const o = await open({ mock: none });
      await ask(o.page, "Q");
      await o.page.getByTestId("brain-ask-answer").waitFor({ timeout: 15000 });
      eq("an answer with no sources shows none", await o.page.getByTestId("brain-ask-sources").count(), 0);
      ck("and still says how long it took, by the page's own clock", /^Answered in \d+ seconds?\.$/.test(await text(o.page.getByTestId("brain-ask-took"))));
      await o.ctx.close();
    }

    // ---- no repo is ready
    {
      const rows = [row("glassflow/tares", "indexing"), row("a/b", "failed")];
      const mock = brainMock(rows);
      const { ctx, page } = await open({ mock, wait: "brain-ask-empty" });
      eq("the empty words say no repo is ready and name the failed one", await text(page.getByTestId("brain-ask-empty")), "Nothing to ask yet No repo is ready yet. a/b failed to index. Repos Check again");
      eq("no question box", await page.getByTestId("brain-ask-question").count(), 0);
      eq("the indexing repo is still named", await text(page.getByTestId("brain-ask-indexing")), "glassflow/tares is still indexing, so its newest files may be missing from answers.");
      eq("the link goes to Repos", await page.getByTestId("brain-ask-repos").getAttribute("href"), "/brain");
      await checkWords(page, ck, "empty 1280");
      await fits(page, "empty 1280");
      await shot(page, "brain-ask-empty-1280.png");
      await page.getByTestId("brain-ask-repos").click();
      await page.waitForURL(/\/brain$/, { timeout: 15000 });
      ck("the link opens /brain", new URL(page.url()).pathname === "/brain");
      await ctx.close();
      const waiting = await open({ mock: brainMock([row("glassflow/tares", "indexing")]), wait: "brain-ask-empty", label: "only indexing" });
      eq("only indexing: no repo is ready yet, no failed names", await text(waiting.page.getByTestId("brain-ask-empty")), "Nothing to ask yet No repo is ready yet. Repos Check again");
      await waiting.ctx.close();
      const nothing = await open({ mock: brainMock([]), wait: "brain-ask-empty" });
      eq("no repos at all: connect one first", await text(nothing.page.getByTestId("brain-ask-empty")), "Nothing to ask yet Connect a repo first. Repos Check again");
      eq("no repos at all: the same empty state", await text(nothing.page.getByTestId("brain-ask-subtitle")), "Ask about the code and decisions in the repos on the brain. The crew asks the same brain.");
      eq("and no indexing line", await nothing.page.getByTestId("brain-ask-indexing").count(), 0);
      await nothing.ctx.close();
      // the cell says no repo is ready when the page thought one was
      const stale = brainMock([READY("a/b")], { answer: async () => ({ status: 200, json: { ready: false, message: "No repo is ready on the brain yet. a/b is still being indexed.", notes: [] } }) });
      const s = await open({ mock: stale });
      await ask(s.page, "Q");
      await s.page.getByTestId("brain-ask-empty").waitFor({ timeout: 15000 });
      ck("a not-ready answer turns the page into the empty state", (await s.page.getByTestId("brain-ask-question").count()) === 0);
      eq("it shows the cell's own words, not 'Connect a repo first'", await text(s.page.getByTestId("brain-ask-empty")), "Nothing to ask yet No repo is ready on the brain yet. a/b is still being indexed. Repos Check again");
      await checkWords(s.page, ck, "not ready 1280");
      stale.answer = null;
      await s.page.getByTestId("brain-ask-recheck").click();
      await s.page.getByTestId("brain-ask-question").waitFor({ timeout: 15000 });
      eq("Check again reads the repos again and gives the box back", stale.reads, 2);
      await ask(s.page, "Q again");
      await s.page.getByTestId("brain-ask-answer").waitFor({ timeout: 15000 });
      await s.ctx.close();
    }

    // ---- no brain, a failed read, loading
    {
      const nb = await open({ mock: brainMock([], { configured: false }), wait: "brain-ask-not-set-up" });
      eq("no brain settings: said plainly", await text(nb.page.getByTestId("brain-ask-not-set-up")), "Not set up The second brain is not set up on this cell.");
      eq("no question box", await nb.page.getByTestId("brain-ask-question").count(), 0);
      await checkWords(nb.page, ck, "not set up 1280");
      await shot(nb.page, "brain-ask-not-set-up-1280.png");
      await nb.ctx.close();
      const n404 = await open({ mock: brainMock([], { readStatus: 404 }), wait: "brain-ask-not-set-up" });
      eq("a 404 on the read is the same", await text(n404.page.getByTestId("brain-ask-not-set-up")), "Not set up The second brain is not set up on this cell.");
      await n404.ctx.close();

      const bad = brainMock(THREE(), { readStatus: 500 });
      const f = await open({ mock: bad, wait: "brain-ask-read-error" });
      eq("a failed read says so", await text(f.page.getByTestId("brain-ask-read-error")), "Ask did not load. The cell did not answer. Try again in a moment. Retry");
      await checkWords(f.page, ck, "read failed 1280");
      await shot(f.page, "brain-ask-read-error-1280.png");
      bad.readStatus = 200;
      await f.page.getByTestId("brain-ask-retry").click();
      await f.page.getByTestId("brain-ask-question").waitFor({ timeout: 15000 });
      ck("Retry loads the page", (await f.page.getByTestId("brain-ask-read-error").count()) === 0);
      await f.ctx.close();

      // a 200 with a body that is not an object (null) is a failed read: the page says so, it does not crash
      const nul = brainMock(THREE(), {});
      nul.readBody = null;
      const nu = await open({ mock: nul, wait: "brain-ask-read-error" });
      eq("a null body on the read is a failed read", await text(nu.page.getByTestId("brain-ask-read-error")), "Ask did not load. The cell did not answer. Try again in a moment. Retry");
      await nu.ctx.close();

      const gate = deferred();
      const lo = await open({ mock: brainMock(THREE(), { readGate: gate }), wait: "brain-ask-loading", one: false });
      const lp = lo.page;
      ck("loading: a skeleton shows, announced", (await lp.getByTestId("brain-ask-loading").getAttribute("aria-busy")) === "true");
      eq("loading: no question box yet", await lp.getByTestId("brain-ask-question").count(), 0);
      await checkWords(lp, ck, "loading 1280");
      await shot(lp, "brain-ask-loading-1280.png");
      gate.go();
      await lp.getByTestId("brain-ask-question").waitFor({ timeout: 15000 });
      await lo.one(ck, "loading, then loaded: the page's read at 1280");
      await lo.ctx.close();
    }

    // ---- a read key can ask
    {
      const mock = brainMock(THREE());
      const { ctx, page } = await open({ token: reader.json.secret, mock });
      await ask(page, "Can a read key ask?");
      await page.getByTestId("brain-ask-answer").waitFor({ timeout: 15000 });
      eq("a read key got an answer", mock.asks.length, 1);
      await ctx.close();
    }

    // ---- at 390: words, no sideways scroll, a screenshot of each state
    {
      const LONG = "an-organisation-with-a-very-long-name/a-repository-with-an-even-longer-name-than-the-organisation";
      const longAnswer = {
        answer: "A long answer. " + "word ".repeat(80) + "\n\n```\n" + "const aVeryLongLineOfCodeThatWouldPushThePageWiderThanThePhone = callSomething(withArguments, andMoreArguments);\n" + "```\n\n| a | b | c |\n|---|---|---|\n| one | two | three |\n\nA link: [" + "long".repeat(30) + "](https://example.com/" + "x".repeat(120) + ")",
        repos: [LONG], duration_ms: 8000,
        citations: [
          { label: 1, path: "src/" + "deeply/".repeat(12) + "nested/file_with_a_long_name.py", start_line: 1200, end_line: 1480, url: `https://github.com/${LONG}/blob/abc/src/x.py#L1200-L1480` },
          { label: 2, path: null, title: "A note with a very long title that goes on and on and on and on and on and on", url: null },
        ],
      };
      for (const w of [1280, 390]) {
        const tag = `${w}`;
        const done = async (page, name) => { await checkWords(page, ck, `${name} ${tag}`); await fits(page, `${name} ${tag}`); await shot(page, `brain-ask-${name}-${tag}.png`); };
        { const o = await open({ mock: brainMock([READY(LONG), row("another-long-organisation/yet-another-quite-long-repository-name", "indexing")], { answer: async () => ({ status: 200, json: longAnswer }) }), width: w });
          await ask(o.page, "A question that is long enough to wrap on a phone, with a lot of words in it, to see how the box behaves");
          await o.page.getByTestId("brain-ask-answer").waitFor({ timeout: 15000 });
          await done(o.page, "long-answer"); await o.ctx.close(); }
        { const o = await open({ mock: brainMock(THREE(), { answer: async () => ({ status: 200, json: QUICK }) }), width: w });
          await done(o.page, "start");
          await ask(o.page, "How does the brain sync?"); await o.page.getByTestId("brain-ask-answer").waitFor({ timeout: 15000 });
          await done(o.page, "answer"); await o.ctx.close(); }
        { const o = await open({ mock: brainMock([row("glassflow/tares", "indexing")]), width: w, wait: "brain-ask-empty" }); await done(o.page, "empty"); await o.ctx.close(); }
        { const o = await open({ mock: brainMock([], { configured: false }), width: w, wait: "brain-ask-not-set-up" }); await done(o.page, "not-set-up"); await o.ctx.close(); }
        { const o = await open({ mock: brainMock(THREE(), { answer: async () => ({ status: 429, json: { detail: "This cell has used its 200 answers for today. Search instead, or try again after midnight UTC." } }) }), width: w });
          await ask(o.page, "Q"); await o.page.getByTestId("brain-ask-error").waitFor({ timeout: 15000 }); await done(o.page, "error"); await o.ctx.close(); }
        // the states of the definition of done, at 1280 and at 390: loading, quick waiting, deep waiting, deep answer, deep error, too long, read error
        const Q = "A question that is long enough to wrap on a phone, with a lot of words in it, to see how the box behaves";
        { const g = deferred();
          const o = await open({ mock: brainMock(THREE(), { readGate: g }), width: w, wait: "brain-ask-loading", one: false });
          await done(o.page, "loading");
          g.go(); await o.page.getByTestId("brain-ask-question").waitFor({ timeout: 15000 });
          await o.one(ck, `loading, then loaded: the page's read at ${w}`); await o.ctx.close(); }
        { const g = deferred();
          const o = await open({ mock: brainMock(THREE(), { gate: async () => { await g.p; } }), width: w });
          await ask(o.page, Q); await o.page.getByTestId("brain-ask-waiting").waitFor({ timeout: 15000 }); await done(o.page, "waiting");
          g.go(); await o.page.getByTestId("brain-ask-answer").waitFor({ timeout: 15000 }); await o.ctx.close(); }
        { const g = deferred();
          const o = await open({ mock: brainMock(THREE(), { gate: async (b) => { if (b.deep) await g.p; } }), width: w });
          await ask(o.page, Q); await o.page.getByTestId("brain-ask-deeper").waitFor({ timeout: 15000 });
          await o.page.getByTestId("brain-ask-deeper").click(); await o.page.getByTestId("brain-ask-waiting").waitFor({ timeout: 15000 });
          await done(o.page, "deep-waiting");
          g.go(); await o.page.waitForFunction(() => document.querySelector("[data-testid=brain-ask-answer]")?.getAttribute("data-deep") === "true", null, { timeout: 15000 });
          await done(o.page, "deep-answer"); await o.ctx.close(); }
        { const o = await open({ mock: brainMock(THREE(), { answer: async (b) => (b.deep ? { status: 502, json: { detail: "The brain did not answer within 120 seconds." } } : { status: 200, json: QUICK }) }), width: w });
          await ask(o.page, Q); await o.page.getByTestId("brain-ask-deeper").waitFor({ timeout: 15000 });
          await o.page.getByTestId("brain-ask-deeper").click(); await o.page.getByTestId("brain-ask-error").waitFor({ timeout: 15000 });
          await done(o.page, "deep-error"); await o.ctx.close(); }
        { const o = await open({ mock: brainMock(THREE()), width: w });
          await o.page.getByTestId("brain-ask-question").fill("word ".repeat(450)); await o.page.getByTestId("brain-ask-too-long").waitFor({ timeout: 15000 });
          await done(o.page, "too-long"); await o.ctx.close(); }
        { const o = await open({ mock: brainMock(THREE(), { readStatus: 500 }), width: w, wait: "brain-ask-read-error" }); await done(o.page, "read-error"); await o.ctx.close(); }
      }
      const { ctx, page } = await open({ mock: brainMock(THREE()), width: 390 });
      const b = await page.getByTestId("brain-ask-question").boundingBox();
      ck("390: the question box is inside the screen and wide", b !== null && b.x >= 0 && b.x + b.width <= 390.5 && b.width > 200, JSON.stringify(b));
      await ctx.close();
    }

    ck("no page errors", errors.length === 0, errors.join(" | "));
  } catch (e) {
    fail++;
    console.log("  FAIL " + (e && e.stack || e));
  } finally {
    await browser.close();
  }
  console.log(`\n${pass} passed, ${fail} failed`);
  process.exit(fail ? 1 : 0);
})();
