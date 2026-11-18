/*
 * Browser e2e for the Second Brain Connect a repo page (TR-1205) on a factory cell.
 *  - Unit checks of the page's pure rules (button label, search, what is sent, admin names, words), run on the
 *    TypeScript as written.
 *  - An admin sees the notice, the search, a checkbox per repo that is not connected yet, and the repos already
 *    connected as plain text. The button counts the choice (0, 1, 3), and POST /api/brain/repos gets exactly the
 *    chosen repos, in the list's order. Success lands on /brain.
 *  - A failed connect says why in words and keeps the choice; a connect that failed part way drops the repos
 *    that did get connected from the choice. More than 50 repos cannot be sent.
 *  - Search finds by name, says so when nothing matches, and does not lose a choice it hides.
 *  - A read key sees "Only admins can connect repos." and who the admins are, and no list, no button: the list is
 *    not even asked for. When the people cannot be read, it says to ask an admin.
 *  - No GitHub connection (with the link to Settings), no repos, every repo connected, a failed read with Retry,
 *    the 502 of GitHub failing, a cell with no brain (404, and a real cell with no licence), loading.
 *  - The folder list of TR-1213 is not there, only room under a checked repo that stays empty.
 *  - The notice that everyone on the cell can ask is under the heading in every state, the loading, failed, not
 *    set up and read key states too. Chosen repos the search hides are said in words (the line is a pure rule too).
 *  - Two repos whose names differ only in "/" and "-" keep their own checkbox (a label toggles its own row).
 *  - Every state loads from exactly one read (the list for an admin, the people for a read key), checked with
 *    expectOneCall. No sideways scroll at 390, at 1280 for the many-repos states. The page's words keep the words list.
 * The brain's own API is answered by the test (the cell here has no GitHub to read from); the daemons are real
 * (tests/licensed.cjs) so the console, the keys, whoami and the 404 of a cell with no licence are real.
 * Screenshots of each state at 1280 and 390 go to $SHOTS_DIR when it is set (the many-repos states, 52 repos and the
 * 50 limit, at both widths too).
 * Run:  npm run build && TARES_E2E_PORT=8880 node tests/brain-connect.e2e.cjs
 */
const fs = require("fs");
const http = require("http");
const path = require("path");
const vm = require("vm");

const UI_DIR = path.resolve(__dirname, "..");
const { chromium } = require(path.join(UI_DIR, "node_modules", "playwright"));
const ts = require(path.join(UI_DIR, "node_modules", "typescript"));
const { checkWords, expectOneCall } = require("./words.cjs");
const { startDaemon } = require("./licensed.cjs");

const ROOT = "root-token-brain-connect-e2e";
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
function loadTs(file) {
  const src = fs.readFileSync(path.join(UI_DIR, "src", "editions", file), "utf8");
  const js = ts.transpileModule(src, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 } }).outputText;
  const mod = { exports: {} };
  vm.runInNewContext(js, {
    module: mod, exports: mod.exports, Date, Number, Math, Object, JSON, Promise, RegExp, Array, Set, String,
    require: (name) => { throw new Error(`unexpected import ${name}`); },
  });
  return mod.exports;
}
const T = loadTs(path.join("factory", "brainConnectText.ts"));

// ---- the cell's answers, as GET /api/brain/repos/available gives them
const R = (repo, o = {}) => ({ repo, private: true, connected: false, ...o });
const LIST = () => [
  R("acme/api"),
  R("acme/docs-site", { private: false }),
  R("acme/fetcher"),
  R("acme/web"),
  R("acme/a-repository-with-a-very-long-name-that-has-to-wrap-on-a-phone-screen-without-a-sideways-scroll"),
  R("acme/indexer", { connected: true }),
  R("acme/legacy", { connected: true }),
  R("acme/old-site", { connected: true }),
];

// ---- the brain API, answered here
function brainMock(repos, extra = {}) {
  const m = {
    repos, github: true, readStatus: 200, readDetail: "nope", gate: null, reads: 0, posts: [], postAnswer: null, ...extra,
  };
  m.handle = async (route) => {
    const req = route.request();
    const p = new URL(req.url()).pathname;
    if (req.method() === "GET" && p === "/api/brain") return route.fulfill({ status: 200, json: { configured: true, repos: [] } });
    if (req.method() === "GET" && p === "/api/brain/repos/available") {
      m.reads++;
      if (m.gate) await m.gate;
      if (m.readStatus !== 200) return route.fulfill({ status: m.readStatus, json: { detail: m.readDetail } });
      return route.fulfill({ status: 200, json: { github: m.github, repos: m.repos } });
    }
    if (req.method() === "POST" && p === "/api/brain/repos") {
      const body = JSON.parse(req.postData() || "null");
      m.posts.push(body);
      const a = m.postAnswer ? m.postAnswer(body, m) : { status: 201, json: { repos: (body.repos || []).map((repo) => ({ repo })) } };
      return route.fulfill(a);
    }
    return route.fulfill({ status: 404, json: { detail: "unexpected" } });
  };
  return m;
}
const isBrainApi = (u) => u.pathname === "/api/brain" || u.pathname.startsWith("/api/brain/");
const PEOPLE = [
  { id: "p1", name: "Ada Lovelace", email: "ada@example.com", admin: true },
  { id: "p2", name: "Grace Hopper", email: "grace@example.com", admin: false },
  { id: "p3", name: "Linus T", email: "linus@example.com", admin: true },
];

(async () => {
  // ---- 0. the pure rules
  {
    eq("connectLabel: none chosen", T.connectLabel(0), "Connect repos");
    eq("connectLabel: one", T.connectLabel(1), "Connect 1 repo");
    eq("connectLabel: three", T.connectLabel(3), "Connect 3 repos");
    eq("connectLabel: fifty", T.connectLabel(50), "Connect 50 repos");
    eq("connectLabel: never negative", T.connectLabel(-2), "Connect repos");
    const all = LIST();
    eq("choosable: the repos not connected yet, in order", T.choosable(all).map((r) => r.repo).slice(0, 2), ["acme/api", "acme/docs-site"]);
    eq("choosable: count", T.choosable(all).length, 5);
    eq("connectedNames", T.connectedNames(all), ["acme/indexer", "acme/legacy", "acme/old-site"]);
    eq("matching: a word of the name, any case", T.matching(T.choosable(all), "  DOCS ").map((r) => r.repo), ["acme/docs-site"]);
    eq("matching: the owner part also matches", T.matching(T.choosable(all), "acme/").length, 5);
    eq("matching: nothing typed keeps all", T.matching(T.choosable(all), "   ").length, 5);
    eq("matching: no match", T.matching(T.choosable(all), "zzz"), []);
    eq("visibility", [T.visibility(R("a/b")), T.visibility(R("a/b", { private: false }))], ["Private", "Public"]);
    eq("picked: in the list's order, not the click order", T.picked(all, new Set(["acme/web", "acme/api"])), ["acme/api", "acme/web"]);
    eq("picked: a repo connected meanwhile is dropped", T.picked(all, new Set(["acme/api", "acme/indexer"])), ["acme/api"]);
    eq("picked: a name that is not on offer is dropped", T.picked(all, new Set(["acme/nope"])), []);
    eq("hiddenChosen: none hidden", T.hiddenChosen(T.choosable(all), ["acme/api", "acme/web"]), 0);
    eq("hiddenChosen: the chosen the search hides", T.hiddenChosen(T.matching(T.choosable(all), "docs"), ["acme/api", "acme/web", "acme/docs-site"]), 2);
    eq("hiddenChosen: nothing chosen", T.hiddenChosen([], []), 0);
    eq("hiddenWords: none is empty", [T.hiddenWords(0), T.hiddenWords(-1)], ["", ""]);
    eq("hiddenWords: one, singular", T.hiddenWords(1), "1 chosen repo is hidden by the search.");
    eq("hiddenWords: two, plural", T.hiddenWords(2), "2 chosen repos are hidden by the search.");
    eq("adminNames", T.adminNames(PEOPLE), ["Ada Lovelace", "Linus T"]);
    eq("adminNames: none", T.adminNames([PEOPLE[1]]), []);
    eq("plainError: capital and full stop", T.plainError("already connected: a/b", "x"), "Already connected: a/b.");
    eq("plainError: keeps a full stop", T.plainError("Done.", "x"), "Done.");
    eq("plainError: falls back", T.plainError("  ", "Fallback."), "Fallback.");
    eq("plainError: nothing", T.plainError(undefined, "Fallback."), "Fallback.");
    eq("MAX_CONNECT is the cell's limit", T.MAX_CONNECT, 50);
    eq("the settings link", T.GITHUB_SETTINGS, "/settings?tab=github");
    ck("the words have no em dash", ![T.NOTICE_WORDS, T.SLOW_WORDS, T.SUBTITLE_WORDS, T.NO_GITHUB_WORDS, T.NO_REPOS_WORDS, T.NOT_ADMIN_WORDS, T.ASK_ADMIN_WORDS, T.NOT_DONE, T.TOO_MANY_WORDS, T.hiddenWords(1), T.hiddenWords(7)].some((w) => w.includes(String.fromCharCode(8212))));
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
    const { token = admin.json.secret, width = 1280, mock, base = d.base, url = "/brain/connect", wait = "page", people = PEOPLE, peopleStatus = 200 } = opts;
    const ctx = await browser.newContext({ viewport: { width, height: 900 } });
    await ctx.addInitScript((t) => { try { if (t) localStorage.setItem("tares_token", t); } catch { /* none */ } }, token);
    const page = await ctx.newPage();
    page.on("pageerror", (e) => errors.push(String(e)));
    if (mock) await page.route(isBrainApi, mock.handle);
    const seen = { people: 0 };
    // the screen's one read: the people for a read key, the list for an admin (and for the cells with no brain)
    const one = expectOneCall(page, token === reader.json.secret ? "/api/factory/people" : "/api/brain/repos/available");
    await page.route((u) => u.pathname === "/api/factory/people", (route) => {
      seen.people++;
      return peopleStatus === 200 ? route.fulfill({ status: 200, json: { people } }) : route.fulfill({ status: peopleStatus, json: { detail: "no" } });
    });
    await page.goto(base + url);
    if (wait === "page") await page.getByTestId("brain-connect").waitFor({ timeout: 15000 });
    return { ctx, page, seen, one: (label) => one(ck, label) };
  };
  const text = async (loc) => (await loc.innerText()).replace(/\s+/g, " ").trim();
  const shot = async (page, name) => { if (SHOTS) await page.screenshot({ path: path.join(SHOTS, name), fullPage: true }); };
  const row = (page, repo) => page.locator(`[data-testid="brain-connect-row"][data-repo="${repo}"]`);
  const box = (page, repo) => row(page, repo).getByRole("checkbox");
  const label = (page) => text(page.getByTestId("brain-connect-submit"));
  const overflow = (page) => page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  const listReady = (page) => page.getByTestId("brain-connect-rows").waitFor({ timeout: 15000 });
  // the checks every state gets: the words, and the notice under the heading. `fit` adds no sideways scroll and
  // nothing wider than the screen (the states shot at a width).
  const NOTICE = "Everyone on this cell will be able to ask about the code in these repos, including people who cannot open them on GitHub.";
  const audit = async (page, label, fit = false) => {
    await checkWords(page, ck, label);
    const n = page.getByTestId("brain-connect-notice");
    ck(`${label}: the notice is there, once, in words`, (await n.count()) === 1 && (await text(n)) === NOTICE, `${await n.count()}`);
    ck(`${label}: the notice comes right after the heading and its line`, await page.evaluate(() => {
      const h = document.querySelector("h1").getBoundingClientRect(), nb = document.querySelector('[data-testid="brain-connect-notice"]').getBoundingClientRect();
      return nb.top >= h.bottom && nb.top - h.bottom < 120;
    }));
    if (fit) {
      const over = await overflow(page);
      ck(`${label}: no sideways scroll`, over <= 0, String(over));
      const wide = await page.evaluate(() => [...document.querySelectorAll('[data-testid^="brain-connect"]')].filter((e) => e.getBoundingClientRect().right > window.innerWidth + 1).map((e) => e.getAttribute("data-testid")));
      ck(`${label}: nothing wider than the screen`, wide.length === 0, wide.join(","));
    }
  };
  const checkedRepos = (page) => page.getByTestId("brain-connect-row").evaluateAll((els) => els.filter((e) => e.querySelector('[role="checkbox"]')?.getAttribute("aria-checked") === "true").map((e) => e.getAttribute("data-repo")));

  try {
    // ---- an admin, the list
    {
      const mock = brainMock(LIST());
      const { ctx, page, one } = await open({ mock });
      await listReady(page);
      await one("admin, list");
      ck("the heading", await page.getByRole("heading", { name: "Connect a repo", exact: true }).isVisible());
      const back = page.getByTestId("brain-connect-back");
      ck("Back to Second Brain links to /brain", (await text(back)) === "Back to Second Brain" && (await back.getAttribute("href")) === "/brain");
      ck("the line under the heading", (await text(page.locator("h1 + p"))) === "Repos the GitHub connection can read. Pick the ones the brain should know.");
      const notice = page.getByTestId("brain-connect-notice");
      eq("the notice, always, in words", await text(notice), "Everyone on this cell will be able to ask about the code in these repos, including people who cannot open them on GitHub.");
      ck("the notice is boxed", await notice.evaluate((e) => parseFloat(getComputedStyle(e).borderTopWidth) >= 1 && getComputedStyle(e).backgroundColor !== "rgba(0, 0, 0, 0)"));
      const find = page.getByLabel("Find a repo");
      ck("the search is labelled Find a repo and has that placeholder", (await find.count()) === 1 && (await find.getAttribute("placeholder")) === "Find a repo");
      eq("a row for every repo that is not connected", await page.getByTestId("brain-connect-row").count(), 5);
      eq("a checkbox in every row, none checked", await page.getByTestId("brain-connect-row").getByRole("checkbox").count(), 5);
      eq("none checked at the start", await checkedRepos(page), []);
      eq("kinds: private and public", await page.getByTestId("brain-connect-kind").allInnerTexts(), ["Private", "Public", "Private", "Private", "Private"]);
      ck("repo names are mono, and a person's text", await row(page, "acme/api").locator("label").evaluate((e) => getComputedStyle(e).fontFamily.toLowerCase().includes("mono") && e.hasAttribute("data-user-text")));
      eq("already connected sit at the end, as plain text", await text(page.getByTestId("brain-connect-already")), "Already connected: acme/indexer, acme/legacy, acme/old-site");
      ck("already connected have no checkbox", (await page.getByTestId("brain-connect-already").getByRole("checkbox").count()) === 0 && (await row(page, "acme/indexer").count()) === 0);
      ck("already connected come after the last row", await page.evaluate(() => {
        const rows = document.querySelectorAll('[data-testid="brain-connect-row"]');
        const al = document.querySelector('[data-testid="brain-connect-already"]');
        return !!rows.length && !!al && rows[rows.length - 1].getBoundingClientRect().bottom <= al.getBoundingClientRect().top + 1;
      }));
      ck("the rows span the whole panel, like the search row", await page.evaluate(() => {
        const list = document.querySelector('[data-testid="brain-connect-list"]').getBoundingClientRect();
        const r = document.querySelector('[data-testid="brain-connect-row"]').getBoundingClientRect();
        return Math.abs(r.left - list.left) <= 2 && Math.abs(r.right - list.right) <= 2;
      }));
      const btn = page.getByTestId("brain-connect-submit");
      eq("nothing chosen: the button label", await label(page), "Connect repos");
      ck("nothing chosen: the button is disabled", await btn.isDisabled());
      eq("the explanation beside the button", await text(page.getByTestId("brain-connect-slow")),
         "The first index of a large repo can take a while. Its card shows how far it has got, and the crew keeps working with grep meanwhile.");
      ck("the explanation sits beside the button", await page.evaluate(() => {
        const b = document.querySelector('[data-testid="brain-connect-submit"]').getBoundingClientRect();
        const s = document.querySelector('[data-testid="brain-connect-slow"]').getBoundingClientRect();
        return s.left >= b.right - 1 && Math.abs((s.top + s.bottom) / 2 - (b.top + b.bottom) / 2) < 60;
      }));
      // no folder UI (TR-1213), and no room until a repo is checked
      const body = await text(page.locator("main"));
      ck("no folder list, fieldset or Add another folder (TR-1213)", !/Folders with decisions|Add another folder|Decision folders/.test(body) && (await page.locator("fieldset").count()) === 0
         && (await page.getByRole("button", { name: /folder/i }).count()) === 0);
      eq("no room under a repo before it is checked", await page.getByTestId("brain-connect-under").count(), 0);
      await audit(page, "admin, list");
      ck("no script errors", errors.length === 0, errors.join(" "));
      await shot(page, "brain-connect-list-1280.png");
      eq("the available list was read once", mock.reads, 1);
      await ctx.close();
    }

    // ---- the button counts the choice; the room under a checked repo; the body sent
    {
      const mock = brainMock(LIST());
      const { ctx, page, one } = await open({ mock });
      await listReady(page);
      await one("the choice");
      const btn = page.getByTestId("brain-connect-submit");
      await box(page, "acme/web").click();
      eq("one chosen: Connect 1 repo", await label(page), "Connect 1 repo");
      ck("one chosen: the button is enabled", await btn.isEnabled());
      const under = page.locator('[data-testid="brain-connect-under"][data-repo="acme/web"]');
      ck("room under the checked repo, and it holds nothing yet", (await under.count()) === 1 && (await under.innerText()).trim() === "" && (await under.evaluate((e) => e.getBoundingClientRect().height)) === 0);
      eq("no room under the repos that are not checked", await page.getByTestId("brain-connect-under").count(), 1);
      await row(page, "acme/api").locator("label").click();   // the name is a label: it checks too
      await box(page, "acme/fetcher").click();
      eq("three chosen: Connect 3 repos", await label(page), "Connect 3 repos");
      eq("the three are checked", await checkedRepos(page), ["acme/api", "acme/fetcher", "acme/web"]);
      await shot(page, "brain-connect-chosen-1280.png");
      await box(page, "acme/fetcher").click();
      eq("one unchecked: Connect 2 repos", await label(page), "Connect 2 repos");
      await box(page, "acme/web").click();
      await box(page, "acme/api").click();
      eq("all unchecked: Connect repos again", await label(page), "Connect repos");
      ck("all unchecked: disabled again, and the room is gone", (await btn.isDisabled()) && (await page.getByTestId("brain-connect-under").count()) === 0);
      // a choice survives a search that hides it, and still counts
      await box(page, "acme/web").click();
      await box(page, "acme/api").click();
      await page.getByLabel("Find a repo").fill("docs");
      eq("search: only the match shows", await page.getByTestId("brain-connect-row").evaluateAll((els) => els.map((e) => e.getAttribute("data-repo"))), ["acme/docs-site"]);
      eq("search: a hidden choice still counts", await label(page), "Connect 2 repos");
      const hid = page.getByTestId("brain-connect-hidden");
      eq("search: two chosen repos are hidden, said in words", await text(hid), "2 chosen repos are hidden by the search.");
      ck("search: the line sits right under the button", await page.evaluate(() => {
        const b = document.querySelector('[data-testid="brain-connect-submit"]').getBoundingClientRect();
        const h = document.querySelector('[data-testid="brain-connect-hidden"]').getBoundingClientRect();
        return h.top >= b.bottom - 1 && h.top - b.bottom < 40;
      }));
      await audit(page, "search hides two chosen repos");
      await page.getByLabel("Find a repo").fill("acme/a");
      eq("search: one of the two is shown again, so one is hidden (singular)", [await page.getByTestId("brain-connect-row").count(), await text(hid)], [2, "1 chosen repo is hidden by the search."]);
      await page.getByLabel("Find a repo").fill("docs");
      await box(page, "acme/docs-site").click();
      eq("search: a visible chosen repo is not counted as hidden", [await text(hid), await label(page)], ["2 chosen repos are hidden by the search.", "Connect 3 repos"]);
      await box(page, "acme/docs-site").click();
      eq("search: unchecking it leaves the same two hidden", [await text(hid), await label(page)], ["2 chosen repos are hidden by the search.", "Connect 2 repos"]);
      await page.getByLabel("Find a repo").fill("");
      eq("search cleared: no hidden line, none left out", await page.getByTestId("brain-connect-hidden").count(), 0);
      eq("search cleared: the choice is still checked", await checkedRepos(page), ["acme/api", "acme/web"]);
      // the body: exactly the chosen repos, in the list's order (api was clicked after web)
      await btn.click();
      await page.waitForURL((u) => u.pathname === "/brain", { timeout: 10000 });
      eq("POST: one request, exactly the chosen repos in the list's order", mock.posts, [{ repos: ["acme/api", "acme/web"] }]);
      ck("success lands on /brain", new URL(page.url()).pathname === "/brain");
      await ctx.close();
    }

    // ---- search
    {
      const mock = brainMock(LIST());
      const { ctx, page, one } = await open({ mock });
      await listReady(page);
      await one("search");
      const shown = () => page.getByTestId("brain-connect-row").evaluateAll((els) => els.map((e) => e.getAttribute("data-repo")));
      await page.getByLabel("Find a repo").fill("FETCH");
      eq("search: by part of the name, any case", await shown(), ["acme/fetcher"]);
      eq("search: nothing chosen, nothing hidden, no line", await page.getByTestId("brain-connect-hidden").count(), 0);
      await page.getByLabel("Find a repo").fill("acme/w");
      eq("search: by the owner and a start", await shown(), ["acme/web"]);
      await page.getByLabel("Find a repo").fill("indexer");
      ck("search: a connected repo is not offered", (await page.getByTestId("brain-connect-row").count()) === 0);
      await page.getByLabel("Find a repo").fill("zzz");
      const nm = page.getByTestId("brain-connect-no-match");
      eq("search: nothing matches, said in words", await text(nm), "No repo matches zzz.");
      ck("search: the already connected line stays, the button stays", (await page.getByTestId("brain-connect-already").count()) === 1 && (await page.getByTestId("brain-connect-submit").count()) === 1);
      await audit(page, "search, no match");
      await shot(page, "brain-connect-no-match-1280.png");
      await page.getByLabel("Find a repo").fill("");
      eq("search cleared: every repo is back", (await shown()).length, 5);
      await ctx.close();
    }

    // ---- two repos whose names differ only in "/" and "-" keep a checkbox each
    {
      const mock = brainMock([R("a/b-c"), R("a-b/c")]);
      const { ctx, page, one } = await open({ mock });
      await listReady(page);
      await one("lookalike names");
      const ids = await page.getByTestId("brain-connect-row").evaluateAll((els) => els.map((e) => e.querySelector('[role="checkbox"]').id));
      ck("lookalike names: each checkbox has its own id", ids.length === 2 && ids[0] !== "" && ids[0] !== ids[1], ids.join(" | "));
      const forIds = await page.getByTestId("brain-connect-row").evaluateAll((els) => els.map((e) => e.querySelector("label").htmlFor));
      eq("lookalike names: each label points at the checkbox of its own row", forIds, ids);
      await row(page, "a-b/c").locator("label").click();
      eq("lookalike names: the label of a-b/c checks only a-b/c", await checkedRepos(page), ["a-b/c"]);
      await row(page, "a/b-c").locator("label").click();
      eq("lookalike names: the label of a/b-c checks only a/b-c, both are checked now", await checkedRepos(page), ["a/b-c", "a-b/c"]);
      await row(page, "a-b/c").locator("label").click();
      eq("lookalike names: a second click unchecks only its own row", await checkedRepos(page), ["a/b-c"]);
      eq("lookalike names: the button counts one", await label(page), "Connect 1 repo");
      await ctx.close();
    }

    // ---- a failed connect: words, the choice kept; a failure part way
    {
      const mock = brainMock(LIST(), { postAnswer: () => ({ status: 409, json: { detail: "already connected: acme/api" } }) });
      const { ctx, page, one } = await open({ mock });
      await listReady(page);
      await one("failed connect");
      await box(page, "acme/api").click();
      await box(page, "acme/web").click();
      await page.getByTestId("brain-connect-submit").click();
      const err = page.getByTestId("brain-connect-error");
      await err.waitFor({ timeout: 10000 });
      eq("a failed connect says the cell's words", await text(err), "Already connected: acme/api.");
      ck("a failed connect stays on the page", new URL(page.url()).pathname === "/brain/connect");
      eq("a failed connect keeps the choice", await checkedRepos(page), ["acme/api", "acme/web"]);
      eq("a failed connect keeps the count and the button works again", [await label(page), await page.getByTestId("brain-connect-submit").isEnabled()], ["Connect 2 repos", true]);
      await audit(page, "failed connect");
      await shot(page, "brain-connect-error-1280.png");
      // another try clears the old error
      mock.postAnswer = null;
      await page.getByTestId("brain-connect-submit").click();
      await page.waitForURL((u) => u.pathname === "/brain", { timeout: 10000 });
      eq("a second try sends the same choice again", mock.posts, [{ repos: ["acme/api", "acme/web"] }, { repos: ["acme/api", "acme/web"] }]);
      await ctx.close();
      // not json, a network failure: a plain sentence, never a stack or a status line
      const m2 = brainMock(LIST(), { postAnswer: () => ({ status: 500, body: "<html>boom</html>", contentType: "text/html" }) });
      const o = await open({ mock: m2 });
      await listReady(o.page);
      await o.one("a failed connect, not json");
      await box(o.page, "acme/api").click();
      await o.page.getByTestId("brain-connect-submit").click();
      await o.page.getByTestId("brain-connect-error").waitFor({ timeout: 10000 });
      const words = await text(o.page.getByTestId("brain-connect-error"));
      ck("an answer that is not words becomes a plain sentence", words.length > 0 && !/<html|boom|500|undefined|\[object/i.test(words) && /[.]$/.test(words), words);
      eq("and the choice is kept", await checkedRepos(o.page), ["acme/api"]);
      await o.ctx.close();
      // a failure part way: acme/api did get connected, the repos are read again and the choice drops it
      const m3 = brainMock(LIST(), {
        postAnswer: (_b, m) => { m.repos = m.repos.map((r) => (r.repo === "acme/api" ? { ...r, connected: true } : r)); return { status: 502, json: { detail: "Could not connect acme/web: GitHub did not answer. Connected before it failed: acme/api" } }; },
      });
      const p = await open({ mock: m3 });
      await listReady(p.page);
      await p.one("a failure part way");
      await box(p.page, "acme/api").click();
      await box(p.page, "acme/web").click();
      await p.page.getByTestId("brain-connect-submit").click();
      await p.page.getByTestId("brain-connect-error").waitFor({ timeout: 10000 });
      await p.page.waitForFunction(() => !document.querySelector('[data-testid="brain-connect-row"][data-repo="acme/api"]'), null, { timeout: 10000 });
      ck("part way: the error names who got connected", (await text(p.page.getByTestId("brain-connect-error"))).includes("Connected before it failed: acme/api"));
      eq("part way: the connected repo leaves the choice and the list, the other stays checked", [await checkedRepos(p.page), await label(p.page)], [["acme/web"], "Connect 1 repo"]);
      ck("part way: the connected repo is in the already connected line", (await text(p.page.getByTestId("brain-connect-already"))).includes("acme/api"));
      await p.ctx.close();
    }

    // ---- more than 50 repos cannot be sent
    {
      const many = Array.from({ length: 52 }, (_, i) => R(`acme/repo-${String(i).padStart(2, "0")}`));
      const mock = brainMock(many);
      const { ctx, page, one } = await open({ mock });
      await listReady(page);
      await one("many repos");
      eq("52 repos: a row each", await page.getByTestId("brain-connect-row").count(), 52);
      await audit(page, "many repos, 1280", true);
      await shot(page, "brain-connect-many-1280.png");
      await page.getByTestId("brain-connect-row").getByRole("checkbox").evaluateAll((els) => els.forEach((e) => e.click()));
      eq("52 chosen: the count", await label(page), "Connect 52 repos");
      ck("52 chosen: the button is disabled and the limit is said in words", (await page.getByTestId("brain-connect-submit").isDisabled()) && (await text(page.getByTestId("brain-connect-too-many"))) === "Connect at most 50 repos at a time.");
      await audit(page, "too many, 1280", true);
      await shot(page, "brain-connect-too-many-1280.png");
      await box(page, "acme/repo-00").click();
      await box(page, "acme/repo-01").click();
      eq("50 chosen: the button works", [await label(page), await page.getByTestId("brain-connect-submit").isEnabled(), await page.getByTestId("brain-connect-too-many").count()], ["Connect 50 repos", true, 0]);
      await page.getByTestId("brain-connect-submit").click();
      await page.waitForURL((u) => u.pathname === "/brain", { timeout: 10000 });
      eq("50 chosen: 50 are sent", [mock.posts.length, mock.posts[0].repos.length], [1, 50]);
      await ctx.close();
    }

    // ---- a read key: the admins named, nothing to press, and the list is never asked for
    {
      const mock = brainMock(LIST());
      const { ctx, page, seen, one } = await open({ token: reader.json.secret, mock });
      await page.getByTestId("brain-connect-admin-only").waitFor({ timeout: 15000 });
      await page.getByTestId("brain-connect-admins").waitFor({ timeout: 15000 });
      await one("read key");
      ck("read key: Only admins can connect repos.", (await text(page.getByTestId("brain-connect-admin-only"))).startsWith("Only admins can connect repos."));
      eq("read key: the admins are named", await text(page.getByTestId("brain-connect-admins")), "Admins on this cell: Ada Lovelace, Linus T.");
      ck("read key: the admins' names are a person's text", await page.getByTestId("brain-connect-admins").locator("[data-user-text]").count() === 2);
      ck("read key: the admins' names are plain text, not mono", await page.getByTestId("brain-connect-admins").locator("[data-user-text]").first().evaluate((e) => !getComputedStyle(e).fontFamily.toLowerCase().includes("mono")));
      ck("read key: no list, no search, no button, no checkbox", (await page.getByTestId("brain-connect-list").count()) === 0 && (await page.getByLabel("Find a repo").count()) === 0
         && (await page.getByTestId("brain-connect-submit").count()) === 0 && (await page.getByRole("checkbox").count()) === 0 && (await page.getByRole("button", { name: /connect/i }).count()) === 0);
      ck("read key: the heading and the way back stay", (await page.getByRole("heading", { name: "Connect a repo", exact: true }).count()) === 1 && (await page.getByTestId("brain-connect-back").count()) === 1);
      ck("read key: the repos were not even asked for", mock.reads === 0 && mock.posts.length === 0, `${mock.reads} reads`);
      ck("read key: the people were read", seen.people >= 1);
      ck("read key: no repo name anywhere on the page", !(await text(page.locator("main"))).includes("acme/"));
      await audit(page, "read key");
      await shot(page, "brain-connect-readonly-1280.png");
      await ctx.close();
      // the people cannot be read, or hold no admin: ask an admin
      for (const [what, o] of [["people fail", { peopleStatus: 500 }], ["no admin among them", { people: [PEOPLE[1]] }]]) {
        const x = await open({ token: reader.json.secret, mock: brainMock(LIST()), ...o });
        await x.page.getByTestId("brain-connect-admins").waitFor({ timeout: 15000 });
        await x.one(`read key, ${what}`);
        await audit(x.page, `read key, ${what}`);
        eq(`read key, ${what}: ask an admin`, await text(x.page.getByTestId("brain-connect-admins")), "Ask an admin of this cell.");
        ck(`read key, ${what}: still no list or button`, (await x.page.getByTestId("brain-connect-list").count()) === 0 && (await x.page.getByTestId("brain-connect-submit").count()) === 0);
        if (what === "people fail") await shot(x.page, "brain-connect-readonly-ask-1280.png");
        await x.ctx.close();
      }
      // a non-admin that posts anyway (the cell answers 403 on its own): nothing in the page can
      const direct = await call(d.base, "POST", "/api/brain/repos", { repos: ["acme/api"] }, { authorization: `Bearer ${reader.json.secret}` });
      ck("read key: the cell itself refuses a connect (403 or 404 on a cell with no brain)", direct.status === 403 || direct.status === 404, String(direct.status));
    }

    // ---- no GitHub connection, no repos, every repo connected
    {
      const mock = brainMock([], { github: false });
      const { ctx, page, one } = await open({ mock });
      await page.getByTestId("brain-connect-no-github").waitFor({ timeout: 15000 });
      await one("no GitHub");
      ck("no GitHub: the words", (await text(page.getByTestId("brain-connect-no-github"))).includes("The GitHub connection is not set up yet."));
      const link = page.getByTestId("brain-connect-settings");
      ck("no GitHub: a link to the connection settings", (await link.count()) === 1 && (await link.getAttribute("href")) === "/settings?tab=github");
      ck("no GitHub: no list and no button", (await page.getByTestId("brain-connect-list").count()) === 0 && (await page.getByTestId("brain-connect-submit").count()) === 0);
      await audit(page, "no GitHub");
      await shot(page, "brain-connect-no-github-1280.png");
      await link.click();
      await page.waitForURL((u) => u.pathname === "/settings", { timeout: 10000 });
      ck("no GitHub: the link opens Settings on the GitHub tab", new URL(page.url()).search === "?tab=github", page.url());
      await ctx.close();

      const e = await open({ mock: brainMock([]) });
      await e.page.getByTestId("brain-connect-empty").waitFor({ timeout: 15000 });
      await e.one("no repos");
      ck("no repos: the words", (await text(e.page.getByTestId("brain-connect-empty"))).includes("The connection cannot read any repos yet."));
      ck("no repos: no button, a link to the connection", (await e.page.getByTestId("brain-connect-submit").count()) === 0 && (await e.page.getByTestId("brain-connect-settings").count()) === 1);
      await audit(e.page, "no repos");
      await shot(e.page, "brain-connect-empty-1280.png");
      await e.ctx.close();

      const a = await open({ mock: brainMock([R("acme/indexer", { connected: true }), R("acme/legacy", { connected: true })]) });
      await a.page.getByTestId("brain-connect-none-left").waitFor({ timeout: 15000 });
      await a.one("every repo connected");
      ck("every repo connected: the words and the line, no checkbox", (await text(a.page.getByTestId("brain-connect-none-left"))).includes("Every repo the connection can read is already connected.")
         && (await text(a.page.getByTestId("brain-connect-already"))) === "Already connected: acme/indexer, acme/legacy" && (await a.page.getByRole("checkbox").count()) === 0);
      ck("every repo connected: the button stays disabled", await a.page.getByTestId("brain-connect-submit").isDisabled());
      await audit(a.page, "every repo connected");
      await shot(a.page, "brain-connect-all-connected-1280.png");
      await a.ctx.close();
    }

    // ---- a failed read, the 502, no brain, loading
    {
      const bad = brainMock(LIST(), { readStatus: 500 });
      const f = await open({ mock: bad });
      await f.page.getByTestId("brain-connect-retry").waitFor({ timeout: 15000 });
      await f.one("a failed read");
      ck("a failed read: the banner says so in words", (await text(f.page.getByTestId("brain-connect-read-error"))).includes("The repo list did not load."));
      ck("a failed read: no list and no button", (await f.page.getByTestId("brain-connect-list").count()) === 0 && (await f.page.getByTestId("brain-connect-submit").count()) === 0);
      await audit(f.page, "failed read");
      await shot(f.page, "brain-connect-read-error-1280.png");
      bad.readStatus = 200;
      await f.page.getByTestId("brain-connect-retry").click();
      await listReady(f.page);
      ck("a failed read: Retry loads the list", (await f.page.getByTestId("brain-connect-retry").count()) === 0 && (await f.page.getByTestId("brain-connect-row").count()) === 5);
      await f.ctx.close();

      const gh = brainMock(LIST(), { readStatus: 502, readDetail: "GitHub refused the connection's token" });
      const g = await open({ mock: gh });
      await g.page.getByTestId("brain-connect-retry").waitFor({ timeout: 15000 });
      await g.one("the 502");
      ck("502: the cell's own words are shown", (await text(g.page.getByTestId("brain-connect-read-error"))).includes("GitHub refused the connection's token."));
      await g.ctx.close();

      for (const [what, m, url, base, token] of [["404", brainMock(LIST(), { readStatus: 404 }), undefined, undefined, undefined], ["no licence", null, undefined, bare.base, ""]]) {
        const o = await open({ mock: m, base, token });
        await o.page.getByTestId("brain-connect-not-set-up").waitFor({ timeout: 15000 });
        await o.one(`no brain (${what})`);
        ck(`no brain (${what}): said plainly`, (await text(o.page.getByTestId("brain-connect-not-set-up"))).includes("The second brain is not set up on this cell."));
        ck(`no brain (${what}): no list and no button`, (await o.page.getByTestId("brain-connect-list").count()) === 0 && (await o.page.getByTestId("brain-connect-submit").count()) === 0);
        await audit(o.page, `no brain (${what})`);
        if (what === "404") { await shot(o.page, "brain-connect-not-set-up-1280.png"); }
        await o.ctx.close();
        void url;
      }

      let release; const gate = new Promise((res) => { release = res; });
      const slow = brainMock(LIST(), { gate });
      const lo = await open({ mock: slow });
      await lo.page.getByTestId("brain-connect-loading").waitFor({ timeout: 15000 });
      ck("loading: a skeleton shows, no button yet", (await lo.page.getByTestId("brain-connect-loading").isVisible()) && (await lo.page.getByTestId("brain-connect-submit").count()) === 0);
      ck("loading: the heading and the way back are there already", (await lo.page.getByRole("heading", { name: "Connect a repo", exact: true }).isVisible()) && (await lo.page.getByTestId("brain-connect-back").isVisible()));
      await audit(lo.page, "loading", true);
      await shot(lo.page, "brain-connect-loading-1280.png");
      release();
      await listReady(lo.page);
      await lo.one("loading");
      ck("loading: the list replaces the skeleton", (await lo.page.getByTestId("brain-connect-loading").count()) === 0);
      await lo.ctx.close();
    }

    // ---- phone width: every state, no sideways scroll
    {
      const LONG = "acme/a-repository-with-a-very-long-name-that-has-to-wrap-on-a-phone-screen-without-a-sideways-scroll";
      const many = () => Array.from({ length: 52 }, (_, i) => R(`acme/repo-${String(i).padStart(2, "0")}`));
      const chooseAll = (p) => p.getByTestId("brain-connect-row").getByRole("checkbox").evaluateAll((els) => els.forEach((e) => e.click()));
      // [name, mock, open options, how it gets ready, what a person does next]
      const states = [
        ["list", brainMock(LIST()), {}, async (p) => { await listReady(p); }],
        ["chosen", brainMock(LIST()), {}, async (p) => { await listReady(p); }, async (p) => { await box(p, "acme/api").click(); await box(p, LONG).click(); }],
        ["hidden", brainMock(LIST()), {}, async (p) => { await listReady(p); }, async (p) => {
          await box(p, "acme/api").click(); await box(p, "acme/web").click(); await p.getByLabel("Find a repo").fill("docs"); await p.getByTestId("brain-connect-hidden").waitFor({ timeout: 5000 });
        }],
        ["no-match", brainMock(LIST()), {}, async (p) => { await listReady(p); }, async (p) => { await p.getByLabel("Find a repo").fill("a-name-with-no-match-at-all-that-is-long-enough-to-need-wrapping-on-a-phone"); await p.getByTestId("brain-connect-no-match").waitFor({ timeout: 5000 }); }],
        ["error", brainMock(LIST(), { postAnswer: () => ({ status: 409, json: { detail: `already connected: acme/api, ${LONG}` } }) }), {}, async (p) => { await listReady(p); }, async (p) => {
          await box(p, "acme/api").click(); await p.getByTestId("brain-connect-submit").click(); await p.getByTestId("brain-connect-error").waitFor({ timeout: 10000 });
        }],
        ["many", brainMock(many()), {}, async (p) => { await listReady(p); }],
        ["too-many", brainMock(many()), {}, async (p) => { await listReady(p); }, async (p) => { await chooseAll(p); await p.getByTestId("brain-connect-too-many").waitFor({ timeout: 5000 }); }],
        ["readonly", brainMock(LIST()), { token: reader.json.secret }, async (p) => { await p.getByTestId("brain-connect-admins").waitFor({ timeout: 15000 }); }],
        ["readonly-ask", brainMock(LIST()), { token: reader.json.secret, peopleStatus: 500 }, async (p) => { await p.getByTestId("brain-connect-admins").waitFor({ timeout: 15000 }); }],
        ["no-github", brainMock([], { github: false }), {}, async (p) => { await p.getByTestId("brain-connect-no-github").waitFor({ timeout: 15000 }); }],
        ["empty", brainMock([]), {}, async (p) => { await p.getByTestId("brain-connect-empty").waitFor({ timeout: 15000 }); }],
        ["all-connected", brainMock([R("acme/indexer", { connected: true }), R("acme/legacy", { connected: true })]), {}, async (p) => { await p.getByTestId("brain-connect-none-left").waitFor({ timeout: 15000 }); }],
        ["read-error", brainMock(LIST(), { readStatus: 500 }), {}, async (p) => { await p.getByTestId("brain-connect-retry").waitFor({ timeout: 15000 }); }],
        ["not-set-up", brainMock(LIST(), { readStatus: 404 }), {}, async (p) => { await p.getByTestId("brain-connect-not-set-up").waitFor({ timeout: 15000 }); }],
      ];
      for (const [name, mock, o, ready, act] of states) {
        const { ctx, page, one } = await open({ mock, width: 390, ...o });
        await ready(page);
        await one(`390, ${name}`);
        if (act) await act(page);
        await audit(page, `390, ${name}`, true);
        await shot(page, `brain-connect-${name}-390.png`);
        await ctx.close();
      }
      // loading at 390: held open, then released (the one read is counted once it has answered)
      {
        let release; const gate = new Promise((res) => { release = res; });
        const { ctx, page, one } = await open({ mock: brainMock(LIST(), { gate }), width: 390 });
        await page.getByTestId("brain-connect-loading").waitFor({ timeout: 15000 });
        await audit(page, "390, loading", true);
        await shot(page, "brain-connect-loading-390.png");
        release();
        await listReady(page);
        await one("390, loading");
        await ctx.close();
      }
      // at 390 the button and its line stack without overlapping
      const { ctx, page } = await open({ mock: brainMock(LIST()), width: 390 });
      await listReady(page);
      const ok = await page.evaluate(() => {
        const b = document.querySelector('[data-testid="brain-connect-submit"]').getBoundingClientRect();
        const s = document.querySelector('[data-testid="brain-connect-slow"]').getBoundingClientRect();
        return !(b.left < s.right && s.left < b.right && b.top < s.bottom && s.top < b.bottom);
      });
      ck("390: the button and its explanation do not overlap", ok);
      const tap = await page.getByTestId("brain-connect-submit").evaluate((e) => e.getBoundingClientRect().height);
      ck("390: the button is tall enough to press", tap >= 32, String(tap));
      await ctx.close();
      // the same list at 1280 holds in the 860 px column
      const w = await open({ mock: brainMock(LIST()), width: 1280 });
      await listReady(w.page);
      const colW = await w.page.getByTestId("brain-connect").evaluate((e) => e.getBoundingClientRect().width);
      ck("1280: the column is at most 860 px", colW <= 860.5 && colW > 600, String(colW));
      await w.ctx.close();
    }

    ck("no script errors anywhere", errors.length === 0, errors.join(" "));
  } catch (e) {
    fail++;
    console.log("  FAIL " + (e && e.stack || e));
  } finally {
    await browser.close();
  }
  console.log(`\n${pass} passed, ${fail} failed`);
  process.exit(fail ? 1 : 0);
})();
