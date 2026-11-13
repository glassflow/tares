/*
 * Browser e2e for TR-741: the factory home on a cell with more than one person, "Waiting for you" with its actions.
 *
 * Starts a scratch daemon (TARES_EDITION=factory, auth on, licensed: the team home is ee) on TARES_E2E_PORT (default 8857),
 * MCP port +20, a second one of a single person, no licence, on port +2, MCP +22, and a third of three people and no licence on port +4,
 * MCP +24, each in a clean environment and a fresh data directory; stops only those.
 * tests/seed-team-home.py fills the first through its API: Alex, Kim and Lee, a project Kim leads, and seven things
 * waiting for Alex (a sign-off, a spec question, two grants, a permission, a question with options, a long question).
 * The page is opened as Alex with his key, then as Lee, who has nothing waiting.
 * Checks: who each row is from, the one read call, the actions (Answer in words, Answer by option, Sign off, Agree,
 * Decline with a reason) and what each leaves behind on the daemon, the long question, a long list, a refusal shown
 * under its row, the cell of one person that keeps the v2 list, no horizontal scroll at 390, and the words check in
 * every state. Shots: team-home-<state>-<width>.png in SHOTS_DIR (three-people, nothing-waiting, loading, error,
 * long-text, many-items, one-person) at 1440 and 390.
 * Run:  npm run test:team-home     (builds first; tares installed in ../.venv or on PATH)
 * Needs python3 for the seeds (TARES_E2E_PYTHON to choose another). TARES_E2E_CHROME points Playwright at another Chrome.
 */
const http = require("http");
const { spawn, spawnSync } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");

const UI_DIR = path.resolve(__dirname, "..");
const REPO = path.resolve(UI_DIR, "..");
const { chromium } = require(path.join(UI_DIR, "node_modules", "playwright"));
const { checkWords, expectOneCall } = require("./words.cjs");
const { checkPaper } = require("./paper.cjs");

const PORT = Number(process.env.TARES_E2E_PORT || 8857);
const SOLO = PORT + 2;
const UNLIC = PORT + 4;   // three people, no licence (TR-932)
const BASE = `http://127.0.0.1:${PORT}`;
const ROOT = "root-token-team-home-e2e";
const SHOTS = process.env.SHOTS_DIR;
const PY = process.env.TARES_E2E_PYTHON || "python3";

let pass = 0, fail = 0;
const ck = (label, cond, detail = "") => {
  cond ? pass++ : fail++;
  console.log((cond ? "  ok   " : "  FAIL ") + label + (cond ? "" : `   ${detail}`));
};

async function waitFor(url, tries = 150) {
  for (; tries > 0; tries--) {
    const ok = await new Promise((res) => http.get(url, (r) => { r.resume(); res(true); }).on("error", () => res(false)));
    if (ok) return true;
    await new Promise((r) => setTimeout(r, 200));
  }
  return false;
}

function cleanEnv(extra) {
  const keep = ["PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "SHELL", "USER"];
  const env = {};
  for (const k of keep) if (process.env[k] !== undefined) env[k] = process.env[k];
  return { ...env, ...extra };
}

const tares = (() => {
  const bin = path.join(REPO, ".venv/bin");
  return fs.existsSync(path.join(bin, "tares")) ? path.join(bin, "tares") : "tares";
})();
const started = [];
function startDaemon(port, extra, people = [], licensed = false) {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "tares-team-home-e2e-"));
  if (people.length) {   // people on the cell before it starts: adding a second person over HTTP needs a licence
    const made = spawnSync(PY, [path.join(REPO, "tests", "_people.py"), path.join(home, "tares.duckdb"), ...people],
      { cwd: REPO, env: { ...process.env, PYTHONPATH: REPO }, encoding: "utf8" });
    if (made.status !== 0) { console.log("FAIL could not seed people: " + (made.stderr || made.stdout)); process.exit(1); }
  }
  const env = cleanEnv({ TARES_EDITION: "factory", TARES_HOME: home, TARES_PORT: String(port), TARES_MCP_PORT: String(port + 20),
                         TARES_OTLP_GRPC_PORT: "off", TARES_UI_DIST: process.env.TARES_UI_DIST || path.join(UI_DIR, "dist"),
                         PYTHONPATH: REPO, ...extra });
  // The team home is ee code: with people on the cell it shows only on a licensed daemon (ee/tests/licensed.py).
  const venvPy = path.join(REPO, ".venv/bin/python");
  const daemon = licensed
    ? spawn(fs.existsSync(venvPy) ? venvPy : PY, [path.join(REPO, "ee", "tests", "licensed.py"), "daemon", "--seats", "50"],
        { cwd: REPO, stdio: "ignore", detached: true,
          env: { ...env, TARES_DB: path.join(home, "tares.duckdb"), TARES_HOST: "127.0.0.1", TARES_CATALOG: path.join(home, "none.yaml") } })
    : spawn(tares, ["up", "--port", String(port), "--data-dir", home], { cwd: REPO, stdio: "ignore", env, detached: true });
  started.push({ daemon, home });
  console.log(`  daemon: pid ${daemon.pid}, port ${port}`);
  return daemon;
}

const overflow = (page) => page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);

(async () => {
  startDaemon(PORT, { TARES_AUTH_TOKEN: ROOT },
    ["Alex=alex-seed@example.com", "Kim=kim-seed@example.com", "Lee=lee-seed@example.com"], true);
  startDaemon(SOLO, {});
  startDaemon(UNLIC, {}, ["Alex=alex-seed@example.com", "Kim=kim-seed@example.com"]);   // plus the owner: three people
  let browser;
  try {
    ck("daemons up", (await waitFor(`${BASE}/health`)) && (await waitFor(`http://127.0.0.1:${SOLO}/health`))
       && (await waitFor(`http://127.0.0.1:${UNLIC}/health`)));
    const seeded = spawnSync(PY, [path.join(__dirname, "seed-team-home.py"), String(PORT), ROOT], { encoding: "utf8" });
    ck("seed ran", seeded.status === 0, (seeded.stderr || seeded.stdout || "").slice(-300));
    const solo = spawnSync(PY, [path.join(__dirname, "seed-factory-home.py"), String(SOLO)], { encoding: "utf8" });
    ck("seed of one person ran", solo.status === 0, (solo.stderr || solo.stdout || "").slice(-300));
    const unlic = spawnSync(PY, [path.join(__dirname, "seed-factory-home.py"), String(UNLIC)], { encoding: "utf8" });
    ck("seed of three people without a licence ran", unlic.status === 0, (unlic.stderr || unlic.stdout || "").slice(-300));
    if (seeded.status !== 0) throw new Error("no seed");
    const ids = JSON.parse(seeded.stdout.trim().split("\n").pop());

    const api = async (method, p, body, token = ROOT) => {
      const r = await fetch(BASE + p, { method, headers: { "content-type": "application/json", authorization: `Bearer ${token}` },
                                        body: body ? JSON.stringify(body) : undefined });
      return { status: r.status, body: await r.json().catch(() => ({})) };
    };
    const shot = async (page, state, width) => {
      if (SHOTS) { fs.mkdirSync(SHOTS, { recursive: true }); await page.screenshot({ path: path.join(SHOTS, `team-home-${state}-${width}.png`), fullPage: true }); }
    };

    browser = await chromium.launch(process.env.TARES_E2E_CHROME ? { executablePath: process.env.TARES_E2E_CHROME } : {});
    const open = async (who, width, base = BASE) => {
      const ctx = await browser.newContext({ viewport: { width, height: width === 390 ? 844 : 900 } });
      const token = who === "solo" || who === "unlicensed" ? null : ids.keys[who];
      await ctx.addInitScript((t) => { try { if (t) localStorage.setItem("tares_token", t); } catch { /* blocked */ } }, token);
      const page = await ctx.newPage();
      const ctl = { fail: false, hold: null, reads: 0, posts: [], more: 0 };
      await page.route(/\/api\/home(\?|$)/, async (route) => {
        ctl.reads++;
        if (ctl.hold) await ctl.hold;
        if (ctl.fail) return route.fulfill({ status: 500, contentType: "application/json", body: JSON.stringify({ detail: "down" }) });
        if (ctl.more || ctl.planTitle) {   // a sign-off over more tickets than the daemon lists by title, or a plan of a long name
          const res = await route.fetch();
          const data = await res.json();
          for (const it of data.waiting_for_you || []) for (const a of it.actions || []) if (a.do === "sign_off") a.more_tickets = ctl.more;
          if (ctl.planTitle) data.plan.title = ctl.planTitle;
          return route.fulfill({ response: res, json: data });
        }
        return route.continue();
      });
      page.on("request", (req) => { if (req.method() === "POST" && new URL(req.url()).pathname.startsWith("/api/")) ctl.posts.push({ path: new URL(req.url()).pathname, body: req.postData() }); });
      return { ctx, page, ctl, base };
    };
    const row = (page, text) => page.getByTestId("waiting-row").filter({ hasText: text });
    const settle = async (page) => { await page.waitForLoadState("networkidle"); };

    // ── looking only: loading, error, the three people's list, Lee with nothing ──
    for (const width of [390, 1440]) {
      const t = (s) => `@${width}: ${s}`;
      {
        const { ctx, page, ctl } = await open("alex", width);
        let release; ctl.hold = new Promise((r) => { release = r; });
        const oneRead = expectOneCall(page, "/api/home");
        await page.goto(`${BASE}/`, { waitUntil: "load" });
        await page.getByTestId("factory-home-loading").waitFor();
        ck(t("placeholders while the first read is out"), (await page.getByTestId("waiting-row").count()) === 0);
        await shot(page, "loading", width);
        await checkWords(page, ck, t("loading"));
        await checkPaper(page, ck, t("loading"), { skip: "[data-testid^=start-checklist]" });
        release(); ctl.hold = null;
        await page.getByTestId("factory-home").waitFor();
        await oneRead(ck, t("alex"));
        await ctx.close();
      }
      {
        const { ctx, page, ctl } = await open("alex", width);
        ctl.fail = true;
        const oneRead = expectOneCall(page, "/api/home");
        await page.goto(`${BASE}/`, { waitUntil: "load" });
        await page.getByTestId("factory-home-error").waitFor();
        await oneRead(ck, t("error"));
        await shot(page, "error", width);
        await checkWords(page, ck, t("error"));
        ck(t("a first read that fails offers Retry and shows no rows"), (await page.getByRole("button", { name: "Retry" }).count()) === 1
           && (await page.getByTestId("waiting-row").count()) === 0);
        await ctx.close();
      }
      {
        const { ctx, page } = await open("alex", width);
        const oneRead = expectOneCall(page, "/api/home");
        await page.goto(`${BASE}/`, { waitUntil: "load" });
        await page.getByTestId("factory-home").waitFor();
        await settle(page);
        await oneRead(ck, t("three people"));
        // TR-1274: the team plan's Home is on the paper too: its waiting lists, crews and repos.
        const paper = await checkPaper(page, ck, t("alex"), { skip: "[data-testid^=start-checklist]" });
        ck(t("the paper check saw the team sections"), paper.heads.length >= 4, JSON.stringify(paper.heads.map((h) => h.t)));
        const rows = page.getByTestId("waiting-row");
        ck(t("six things wait for Alex"), (await rows.count()) === 6, String(await rows.count()));
        const senders = await rows.evaluateAll((els) => els.map((e) => e.querySelector('[data-testid="waiting-from"]')?.getAttribute("aria-label")));
        ck(t("each row says who it is from: Kim three times, the crew three"),
           senders.filter((s) => s === "From Kim").length === 3 && senders.filter((s) => s === "From Your crew").length === 3, JSON.stringify(senders));
        ck(t("the headline counts them"), /6 things wait for you/.test(await page.getByTestId("home-headline").innerText()), await page.getByTestId("home-headline").innerText());
        const buttons = async (text) => (await row(page, text).getByRole("button").allInnerTexts()).map((s) => s.trim()).sort();
        ck(t("the question has Answer"), (await buttons("Which page does the checkout")).join() === "Answer");
        ck(t("nothing asks for a sign-off"), (await page.getByRole("button", { name: "Sign off" }).count()) === 0);
        ck(t("a grant has Agree and Decline"), (await buttons("merge to acme/web")).join() === "Agree,Decline");
        ck(t("the permission has Allow and Answer"), (await buttons("gh api repos")).join() === "Allow,Answer");
        ck(t("the command shows as code"), (await row(page, "gh api repos").locator("code").innerText()) === "gh api repos/acme/web/statuses/abc");
        ck(t("no horizontal scroll"), (await overflow(page)) <= 0, `${await overflow(page)}px over`);
        await shot(page, "three-people", width);
        await checkWords(page, ck, t("three people"));
        await ctx.close();
      }
      {
        const { ctx, page } = await open("lee", width);
        const oneRead = expectOneCall(page, "/api/home");
        await page.goto(`${BASE}/`, { waitUntil: "load" });
        await page.getByTestId("factory-home").waitFor();
        await oneRead(ck, t("lee"));
        ck(t("Lee has no rows and the page says so"), (await page.getByTestId("waiting-row").count()) === 0
           && (await page.getByText("Nothing waits for you", { exact: false }).count()) >= 1);
        ck(t("no horizontal scroll"), (await overflow(page)) <= 0);
        await shot(page, "nothing-waiting", width);
        await checkWords(page, ck, t("nothing waiting"));
        await ctx.close();
      }
    }


    // ── TR-761, live: what Alex waits on, no Projects on Home (TR-1275), the side column; Kim's view of Shop ──
    let kim = null;   // Kim's page at 1440 stays open: she reads Shop again once Alex has answered
    for (const width of [390, 1440]) {
      const t = (s) => `live @${width}: ${s}`;
      const { ctx, page } = await open("alex", width);
      const oneRead = expectOneCall(page, "/api/home");
      await page.goto(`${BASE}/`, { waitUntil: "load" });
      await page.getByTestId("factory-home").waitFor();
      await oneRead(ck, t("alex"));
      const n = (id) => page.getByTestId(id).count();
      const texts = (id) => page.getByTestId(id).allInnerTexts();
      const alexHome = (await api("GET", "/api/home", undefined, ids.keys.alex)).body;

      // a. You are waiting on
      const won = page.getByTestId("waiting-on-row");
      const portal = won.filter({ hasText: "Portal" }), notes = won.filter({ hasText: "Notes" });
      ck(t("a project's name opens the project"), (await portal.getByTestId("waiting-on-project").getAttribute("href")) === `/projects/${ids.projects.portal}`);
      ck(t("You are waiting on lists Portal and Notes, the two projects Alex leads that wait on Kim"), (await won.count()) === 2
         && (await portal.count()) === 1 && (await notes.count()) === 1, JSON.stringify(await won.allInnerTexts()));
      ck(t("each row says the question that is open, for Kim"), (await portal.innerText()).includes("1 question open for Kim.")
         && (await notes.innerText()).includes("1 question open for Kim."), JSON.stringify(await won.allInnerTexts()));
      ck(t("no repo chips, no Hand over anyway, no Set an owner, no Remind"), (await n("waiting-on-chip")) === 0
         && (await page.getByRole("button", { name: "Hand over anyway" }).count()) === 0
         && (await page.getByRole("link", { name: "Set an owner" }).count()) === 0
         && (await page.getByRole("button", { name: "Remind" }).count()) === 0);
      ck(t("Portal and Notes are not also Ready to start"), (await page.getByTestId("waiting-row").filter({ hasText: "Ready to start" }).filter({ hasText: /Portal|Notes/ }).count()) === 0);
      ck(t("the daemon's summary says Alex waits on Kim for two"), /You are waiting on Kim for 2\./.test(alexHome.summary || ""), alexHome.summary);
      await shot(page, "live-waiting-on", width);
      ck(t("the label says the list holds projects Alex leads, none marked no lead"),
         (await page.getByTestId("waiting-on-scope").innerText()) === "projects you lead" && (await n("waiting-on-no-lead")) === 0);
      {
        // a project nobody leads falls to the cell's first person: the row says so and offers Set a lead
        const other = await open("alex", width);
        await other.page.route(/\/api\/home(\?|$)/, async (route) => {
          const res = await route.fetch();
          const data = await res.json();
          for (const e of data.you_are_waiting_on || []) if (e.title === "Notes") {
            e.no_lead = true;
            e.actions = [...e.actions, { label: "Set a lead", primary: false, href: `/projects/${e.project.id}#people` }];
          }
          return route.fulfill({ response: res, json: data });
        });
        await other.page.goto(`${BASE}/`, { waitUntil: "load" });
        await other.page.getByTestId("waiting-on").waitFor();
        const nrow = other.page.getByTestId("waiting-on-row").filter({ hasText: "Notes" });
        ck(t("with one project of no lead, the label names both kinds"),
           (await other.page.getByTestId("waiting-on-scope").innerText()) === "projects you lead, and ones with no lead yet");
        ck(t("the row of no lead says No lead yet, and only that row"),
           (await nrow.getByTestId("waiting-on-no-lead").count()) === 1 && (await other.page.getByTestId("waiting-on-no-lead").count()) === 1);
        ck(t("Set a lead opens the project's People card"),
           (await nrow.getByRole("link", { name: "Set a lead" }).getAttribute("href")) === `/projects/${ids.projects.notes}#people`);
        await other.ctx.close();
      }

      // d. no Projects on Home (TR-1275): the list is one click away on Projects; the private project still leaks nowhere
      const private_ = ids.private;
      const bodyText = await page.evaluate(() => document.body.innerText);
      ck(t("no Projects section and no project rows on Home"), (await page.getByRole("region", { name: "Projects" }).count()) === 0
         && (await n("project-row")) === 0 && (await n("projects-switch")) === 0 && (await n("hidden-row")) === 0 && (await n("hidden-summary")) === 0);
      ck(t("the page and the home read name nothing of the private project"), ![private_.name, private_.goal].some((w) => bodyText.includes(w))
         && !JSON.stringify(alexHome).includes(private_.name) && !JSON.stringify(alexHome).includes(private_.goal));

      // e. the side column
      const crewRows = await texts("team-crew-row");
      ck(t("Kim's crew is a row, with its machine and what it does"), crewRows.length === 1 && /Kim's crew/.test(crewRows[0]) && /kim-mbp/.test(crewRows[0]), JSON.stringify(crewRows));
      ck(t("Alex's own crew is not a row"), !crewRows.some((x) => /Your crew|Alex/.test(x)), JSON.stringify(crewRows));
      ck(t("Lee's crew, last heard two days ago, is under Not seen in a day"), (await n("quiet-crews")) === 1 && (await texts("quiet-crews"))[0].trim() === "Not seen in a day: Lee's crew", JSON.stringify(await texts("quiet-crews")));
      ck(t("the daemon agrees: Kim's crew is heard from, Lee's is quiet"), JSON.stringify(alexHome.crews.map((c) => c.name)) === JSON.stringify(["Your crew", "Kim's crew"])
         && JSON.stringify(alexHome.crews_quiet) === JSON.stringify(["Lee's crew"]), JSON.stringify([alexHome.crews.map((c) => c.name), alexHome.crews_quiet]));
      const members = (await texts("crew-member")).map((x) => x.split("\n")[0].trim());
      ck(t("Your crew holds Alex's own orchestrator and builder, not Kim's or Lee's orchestrator"), members.length === 2
         && members[0] === "Orchestrator" && /^Builder/.test(members[1]), JSON.stringify(members));
      ck(t("Your crew reads his orchestrator: running, heard 4 minutes ago"), (await page.getByText("Running, last heard 4 minutes ago").count()) === 1);
      const owned = await texts("repo-you-own");
      ck(t("Repos you own lists what the daemon says, acme/web, and not acme/docs, which nobody owns"), alexHome.areas.map((a) => a.repo).join() === "acme/web" && owned.length === 1
         && /acme\/web/.test(owned[0]) && !/acme\/docs/.test(owned.join()), JSON.stringify([alexHome.areas, owned]));
      await shot(page, "live-side", width);
      await page.getByRole("link", { name: "All owners" }).click();
      await page.getByTestId("pane-areas").waitFor({ timeout: 10000 }).catch(() => {});
      ck(t("All owners opens Settings with the Areas tab selected"), (await page.getByTestId("pane-areas").count()) === 1, page.url());
      await ctx.close();
    }
    {
      // Kim leads Shop, and waits on Alex for the question she asked him
      const { ctx, page } = await open("kim", 1440);
      await page.goto(`${BASE}/`, { waitUntil: "load" });
      await page.getByTestId("factory-home").waitFor();
      await settle(page);
      const shopRow = page.getByTestId("waiting-on-row").filter({ hasText: "Shop" });
      ck("kim: Shop waits for Alex's answer, and nothing else", (await page.getByTestId("waiting-on-row").count()) === 1
         && (await shopRow.innerText()).includes("1 question open for Alex."), await shopRow.innerText());
      await shot(page, "live-kim", 1440);
      await checkWords(page, ck, "kim: live");
      kim = { ctx, page };
    }

    // ── acting, as Alex at 1440 ──
    // ── F7: in a sample, a box opens and confirming it says nothing was sent ──
    {
      const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
      await ctx.addInitScript((tok) => { try { localStorage.setItem("taresSamples", "1"); localStorage.setItem("tares_token", tok); } catch { /* blocked */ } }, ids.keys.alex);
      const page = await ctx.newPage();
      const posts = [];
      page.on("request", (req) => { if (req.method() !== "GET" && new URL(req.url()).pathname.startsWith("/api/")) posts.push(req.url()); });
      const t = (s) => `sample: ${s}`;
      await page.goto(`${BASE}/?sample=threePeople`, { waitUntil: "load" });
      await page.getByTestId("factory-home").waitFor();
      await settle(page);
      const note = page.getByText("This is a sample, so nothing was sent.");
      await row(page, "Try migrations on staging").getByRole("button", { name: "Decline" }).click();
      const sd = page.getByTestId("decline-dialog");
      ck(t("Decline opens its box"), await sd.waitFor({ timeout: 3000 }).then(() => true, () => false));
      await sd.getByTestId("decline-confirm").click();
      await note.first().waitFor({ timeout: 3000 }).catch(() => {});
      ck(t("confirming says nothing was sent, and the box closes"), (await note.count()) >= 1 && (await sd.count()) === 0, String(await note.count()));
      ck(t("the row stays and the cell was not asked to change anything"), (await row(page, "Try migrations on staging").count()) === 1 && posts.length === 0, JSON.stringify(posts));
      await note.first().waitFor({ state: "detached", timeout: 15000 }).catch(() => {});   // let the first one go, so the next is its own
      await row(page, "Try migrations on staging").getByRole("button", { name: "Agree" }).click();
      await note.first().waitFor({ timeout: 3000 }).catch(() => {});
      ck(t("Agree says so too and sends nothing"), (await note.count()) >= 1 && posts.length === 0, JSON.stringify(posts));
      await checkWords(page, ck, t("after a sample action"));
      await ctx.close();
    }
    // ── TR-1051 F1: the ee chunk is slow; core's plain rows (with the team's buttons) must not flash ──
    {
      const t = (s) => `slow ee chunk: ${s}`;
      const { ctx, page } = await open("alex", 1440);
      let release; const hold = new Promise((r) => { release = r; });
      const asked = [];
      await page.route(/\/assets\/ee-[^/]*\.js/, async (route) => { asked.push(route.request().url()); await hold; await route.continue(); });
      await page.goto(`${BASE}/`, { waitUntil: "load" });
      await page.getByTestId("factory-home").waitFor();
      await page.getByTestId("waiting-pending").waitFor({ timeout: 10000 }).catch(() => {});
      await page.waitForTimeout(500);   // the home's own read has long come back; only the chunk is out
      const buttons = () => page.getByRole("button", { name: /^(Agree|Decline|Answer|Allow|Sign off|Keep)/ }).count();
      ck(t("the chunk was asked for and is held"), asked.length >= 1, String(asked.length));
      ck(t("a placeholder stands where Waiting for you will be"), (await page.getByTestId("waiting-pending").count()) === 1);
      ck(t("no waiting row and no row button shows before the chunk arrives"), (await page.getByTestId("waiting-row").count()) === 0 && (await buttons()) === 0,
         `${await page.getByTestId("waiting-row").count()} rows, ${await buttons()} buttons`);
      release();
      await page.getByTestId("waiting-team").waitFor({ timeout: 10000 }).catch(() => {});
      ck(t("then the team's list shows with its buttons and the placeholder is gone"), (await page.getByTestId("waiting-team").count()) === 1
         && (await page.getByTestId("waiting-pending").count()) === 0 && (await page.getByRole("button", { name: "Agree" }).count()) >= 1);
      await checkWords(page, ck, t("after it loads"));
      await ctx.close();
    }
    // ── TR-1050 F1: the cell's licence answer is slow; core's plain rows must not flash before it comes ──
    {
      const t = (s) => `slow licence answer: ${s}`;
      const { ctx, page } = await open("alex", 1440);
      let release; const hold = new Promise((r) => { release = r; });
      const asked = [];
      await page.route(/\/api\/licence(\?|$)/, async (route) => { asked.push(route.request().url()); await hold; await route.continue(); });
      await page.goto(`${BASE}/`, { waitUntil: "load" });
      await page.getByTestId("factory-home").waitFor();
      await page.waitForTimeout(800);   // the home's own read has long come back; only the licence answer is out
      const buttons = () => page.getByRole("button", { name: /^(Agree|Decline|Answer|Allow|Sign off|Keep)/ }).count();
      ck(t("the licence was asked for and is held"), asked.length >= 1, String(asked.length));
      ck(t("a placeholder stands where Waiting for you will be"), (await page.getByTestId("waiting-pending").count()) === 1);
      ck(t("no waiting row and no row button shows before the answer comes"), (await page.getByTestId("waiting-row").count()) === 0 && (await buttons()) === 0,
         `${await page.getByTestId("waiting-row").count()} rows, ${await buttons()} buttons`);
      release();
      await page.getByTestId("waiting-team").waitFor({ timeout: 10000 }).catch(() => {});
      ck(t("then the team's list draws with its six rows and the placeholder is gone"), (await page.getByTestId("waiting-team").count()) === 1
         && (await page.getByTestId("waiting-row").count()) === 6 && (await page.getByTestId("waiting-pending").count()) === 0);
      await ctx.close();
    }
    // ── TR-1050 F4: a person has one colour: on a waiting row and on their crew's square, keyed by name ──
    {
      const t = (s) => `person colour: ${s}`;
      const { ctx, page } = await open("alex", 1440);
      await page.unroute(/\/api\/home(\?|$)/);
      await page.route(/\/api\/home(\?|$)/, async (route) => {   // give Kim an id whose hash would pick another colour than "Kim" does
        const res = await route.fetch();
        const data = await res.json();
        for (const it of data.waiting_for_you || []) if (it.from && it.from.name === "Kim") it.from.id = "u2";
        return route.fulfill({ response: res, json: data });
      });
      await page.goto(`${BASE}/`, { waitUntil: "load" });
      await page.getByTestId("factory-home").waitFor();
      await settle(page);
      const kimMark = page.locator('[data-testid="waiting-from"][aria-label="From Kim"]').first();
      const crewMark = page.getByTestId("team-crew-row").filter({ hasText: "Kim's crew" }).locator("span[aria-hidden]").first();
      const bg = (loc) => loc.evaluate((e) => getComputedStyle(e).backgroundColor);
      ck(t("Kim has a mark on a waiting row and on her crew's row"), (await kimMark.count()) === 1 && (await crewMark.count()) === 1);
      const a = await bg(kimMark), b = await bg(crewMark);
      ck(t("the two agree, though the row's sender carries an id"), a === b, `${a} vs ${b}`);
      await ctx.close();
    }
    // ── TR-1051 F2: a sample without the team's code says "this is a sample", not "needs a licence" ──
    {
      const t = (s) => `sample, no team code: ${s}`;
      const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
      await ctx.addInitScript((tok) => { try { localStorage.setItem("taresSamples", "1"); localStorage.setItem("tares_token", tok); } catch { /* blocked */ } }, ids.keys.alex);
      // a stand-in for the ee chunk that registers one team sample (the live home, with its grants) and no slots
      await ctx.route(/\/assets\/ee-[^/]*\.js/, (route) => route.fulfill({ contentType: "text/javascript", body: `
        const tok = (() => { try { return localStorage.getItem("tares_token"); } catch { return null; } })();
        const home = await fetch("/api/home", { headers: tok ? { authorization: "Bearer " + tok } : {} }).then((r) => r.json());
        export function register(registerSlot, registerSamples) { registerSamples("home", { stubTeam: home }); }` }));
      const page = await ctx.newPage();
      const posts = [];
      page.on("request", (req) => { if (req.method() !== "GET" && new URL(req.url()).pathname.startsWith("/api/")) posts.push(req.url()); });
      await page.goto(`${BASE}/?sample=stubTeam`, { waitUntil: "load" });
      await page.getByTestId("factory-home").waitFor({ timeout: 15000 });
      await settle(page);
      const decline = page.getByRole("button", { name: "Decline" }).first();
      ck(t("the sample shows a Decline button"), (await decline.count()) === 1);
      await decline.click();
      const note = page.getByText("This is a sample, so nothing was sent.");
      await note.first().waitFor({ timeout: 3000 }).catch(() => {});
      ck(t("Decline says nothing was sent"), (await note.count()) >= 1, String(await note.count()));
      ck(t("and not that a licence is needed, under the row or anywhere"), (await page.getByTestId("row-error").count()) === 0
         && (await page.getByText(/needs an enterprise licence/).count()) === 0);
      ck(t("nothing was sent to the cell"), posts.length === 0, JSON.stringify(posts));
      await ctx.close();
    }
    {
      const { ctx, page, ctl } = await open("alex", 1440);
      const t = (s) => `acts: ${s}`;
      await page.goto(`${BASE}/`, { waitUntil: "load" });
      await page.getByTestId("factory-home").waitFor();
      await settle(page);
      const rows = page.getByTestId("waiting-row");

      // Answer a spec question in words
      const q = row(page, "Which page does the checkout");
      await q.getByRole("button", { name: "Answer" }).click();
      const panel = q.getByTestId("answer-panel");
      await panel.waitFor();
      ck(t("Answer opens a box under the row, Send is off while it is empty"), (await panel.getByRole("button", { name: "Send" }).isDisabled()));
      ck(t("a spec question cannot stand for next time"), (await panel.getByTestId("answer-standing").count()) === 0);
      await panel.getByTestId("answer-text").fill("The checkout lives on /pay.");
      await checkWords(page, ck, t("answer box"));
      await panel.getByRole("button", { name: "Cancel" }).click();
      await panel.waitFor({ state: "detached" });
      ck(t("Cancel closes it and asks the daemon for nothing"), ctl.posts.length === 0 && (await rows.count()) === 6);
      await q.getByRole("button", { name: "Answer" }).click();
      await panel.getByTestId("answer-text").fill("The checkout lives on /pay.");
      await panel.getByRole("button", { name: "Send" }).click();
      await q.waitFor({ state: "detached" });
      const qs = (await api("GET", `/api/projects/${ids.project}/questions`)).body.questions;
      ck(t("the answer reached the spec question, from the console"), qs[0].answer === "The checkout lives on /pay." && qs[0].via === "console" && qs[0].state === "answered", JSON.stringify(qs[0]));
      ck(t("its row is gone and five remain"), (await rows.count()) === 5, String(await rows.count()));

      // Answer a crew question by option
      const colour = row(page, "Which colour for the links");
      await colour.getByRole("button", { name: "Answer" }).click();
      const optPanel = colour.getByTestId("answer-panel");
      await optPanel.waitFor();
      ck(t("a question with options shows one button each"), (await optPanel.getByRole("button", { name: "blue", exact: true }).count()) === 1
         && (await optPanel.getByRole("button", { name: "green", exact: true }).count()) === 1 && (await optPanel.getByRole("button", { name: "b!", exact: true }).count()) === 1
         && (await optPanel.getByTestId("answer-text").count()) === 0);
      ck(t("a crew question can stand for next time"), (await optPanel.getByTestId("answer-standing").count()) === 1);
      await optPanel.getByTestId("answer-standing").click();
      await optPanel.getByRole("button", { name: "b!", exact: true }).click();
      await colour.waitFor({ state: "detached" });
      const decided = (await api("GET", `/api/projects/${ids.project}/decisions`)).body.decisions.find((d) => /Which colour for the links/.test(d.question));
      ck(t("the option clicked is the recorded answer, made in the console"), !!decided && decided.via === "console", JSON.stringify(decided));
      const replyPost = ctl.posts.find((x) => /^\/api\/desk\/[^/]+\/reply$/.test(x.path));
      ck(t("F5, F3: the option clicked is sent as the choice, with its position as the words"), !!replyPost && JSON.parse(replyPost.body || "{}").choice === "b!" && JSON.parse(replyPost.body || "{}").words === "C", JSON.stringify(replyPost));
      ck(t("F5, F3: the daemon recorded the option clicked, not the one a look-alike letter names"), !!decided && decided.choice === "b!", JSON.stringify(decided));
      const stood = (await api("GET", `/api/grants?project=${ids.project}`, undefined, ids.keys.kim)).body;
      const stoodWords = (stood.grants || stood).map((g) => g.words);
      ck(t("F5: a standing option click keeps the option text as the grant, never its letter"), !!decided && decided.standing === true && stoodWords.includes("b!") && !stoodWords.includes("C"), JSON.stringify(stoodWords));
      const desk = (await api("GET", "/api/desk?state=answered")).body.desk;
      ck(t("the desk item is answered"), desk.some((d) => d.label === ids.desk.options), JSON.stringify(desk.map((d) => d.label)));
      ck(t("four remain"), (await rows.count()) === 4, String(await rows.count()));

      // TR-1021: a question that holds the crew up comes first and is answered on its row
      const font = await api("POST", "/api/desk", { question: "Which font for the shop?", options: ["serif", "sans"],
        recommendation: "sans, it reads best on phones", why: "the mock uses it", project: ids.project,
        to: ids.people.alex, blocking: true, blocks: "the shop's header ticket" });
      ck(t("TR-1021: a blocking question is put on the desk"), font.status < 300, JSON.stringify(font));
      await page.reload();
      await row(page, "Which font for the shop").waitFor();
      const first = row(page, "Which font for the shop");
      const inNow = await page.evaluate(() => {
        const rest = document.querySelector('[data-testid="waiting-rest"]');
        const f = [...document.querySelectorAll('[data-testid="waiting-row"]')].find((r) => r.textContent.includes("Which font for the shop"));
        return !!f && (!rest || !!(f.compareDocumentPosition(rest) & Node.DOCUMENT_POSITION_FOLLOWING));
      });
      ck(t("TR-1021: it is among what holds the crew up, above the rest"), (await page.getByTestId("waiting-now").count()) === 1 && inNow);
      ck(t("TR-1021: its row shows the recommendation and what it holds up"),
         (await first.getByTestId("question-recommendation").innerText()).includes("sans, it reads best on phones")
         && (await first.innerText()).includes("Holds up the shop's header ticket"));
      ck(t("TR-1021: the meta line says who it is from, in words"), /^From /.test((await first.getByTestId("waiting-meta").innerText()).trim()),
         await first.getByTestId("waiting-meta").innerText());
      ctl.posts.length = 0;
      await first.getByTestId("question-choices").getByRole("button", { name: "sans", exact: true }).click();
      await row(page, "Which font for the shop").waitFor({ state: "detached" });
      const fontPost = ctl.posts.find((x) => /^\/api\/desk\/[^/]+\/reply$/.test(x.path));
      ck(t("TR-1021: the option on the row is sent as the choice, its letter as the words"),
         !!fontPost && JSON.parse(fontPost.body || "{}").choice === "sans" && JSON.parse(fontPost.body || "{}").words === "B", JSON.stringify(fontPost));
      const fontDecided = (await api("GET", `/api/projects/${ids.project}/decisions`)).body.decisions.find((d) => /Which font for the shop/.test(d.question));
      ck(t("TR-1021: the daemon recorded it from the console"), !!fontDecided && fontDecided.via === "console" && fontDecided.choice === "sans", JSON.stringify(fontDecided));
      ck(t("TR-1021: four remain again"), (await rows.count()) === 4, String(await rows.count()));

      // Kim, who leads Shop, waits on nobody once her home is read again: Alex answered her question
      await new Promise((r) => setTimeout(r, 5500));   // the daemon keeps /api/home for 5 seconds per credential
      await kim.page.reload({ waitUntil: "load" });
      await kim.page.getByTestId("factory-home").waitFor();
      await settle(kim.page);
      ck(t("Kim no longer waits on Alex for Shop"), (await kim.page.getByTestId("waiting-on-row").filter({ hasText: "Shop" }).count()) === 0);
      await shot(kim.page, "live-kim-answered", 1440);
      await checkWords(kim.page, ck, t("kim after the answer"));
      await kim.ctx.close();

      // Agree and Decline
      const g1 = row(page, "merge to acme/web");
      await g1.getByRole("button", { name: "Agree" }).click();
      await g1.waitFor({ state: "detached" });
      const g2 = row(page, "read the logs of acme/web");
      await g2.getByRole("button", { name: "Decline" }).click();
      const dd = page.getByTestId("decline-dialog");
      await dd.waitFor();
      await dd.getByTestId("decline-reason").fill("It reads the logs of every customer.");
      await checkWords(page, ck, t("decline dialog"));
      await dd.getByTestId("decline-confirm").click();
      await g2.waitFor({ state: "detached" });
      const listed = (await api("GET", `/api/grants?project=${ids.project}`, undefined, ids.keys.kim)).body;
      const grants = listed.grants;
      const state = (id) => (grants.find((g) => g.id === id) || {}).state || (JSON.stringify(listed.known_words || "").includes("read the logs of acme/web") ? "revoked" : undefined);
      ck(t("Agree made the first grant active"), state(ids.grants[0]) === "active", JSON.stringify(grants.map((g) => [g.id, g.state])));
      ck(t("Decline made the second one revoked"), state(ids.grants[1]) === "revoked", JSON.stringify(grants.map((g) => [g.id, g.state])));
      const declinePost = ctl.posts.find((x) => x.path === `/api/grants/${ids.grants[1]}/decline`);
      ck(t("Decline sends the reason it was given to the daemon"), !!declinePost && JSON.parse(declinePost.body || "{}").reason === "It reads the logs of every customer.", JSON.stringify(declinePost));
      ck(t("two remain: the permission and the long question"), (await rows.count()) === 2, String(await rows.count()));

      // Allow: the permission a builder asked for, kept as a grant in words
      const perm = row(page, "gh api repos");
      await perm.getByRole("button", { name: "Allow" }).click();
      const ad = page.getByTestId("allow-dialog");
      await ad.waitFor();
      await checkWords(page, ck, t("allow dialog"));
      await ad.getByRole("button", { name: "Allow", exact: true }).click();
      await perm.waitFor({ state: "detached" });
      const given = (await api("GET", "/api/desk?state=answered")).body.desk;
      ck(t("Allow answered the permission on the daemon"), given.some((d) => d.label === ids.desk.allow), JSON.stringify(given.map((d) => d.label)));
      ck(t("one remains: the long question"), (await rows.count()) === 1, String(await rows.count()));

      // Two items made for the refusals: the daemon withdraws one after the page listed it, the other has an open assumption
      const asm = (await api("POST", `/api/projects/${ids.project}/assumptions`, { question: "Which payment provider?", choice: "provider-x", why: "it has a test mode" }, ids.keys.kim)).body;
      const gone = (await api("POST", "/api/desk", { question: "Is the footer wording fine?", recommendation: "yes", why: "it is short", project: ids.project, to: ids.people.alex })).body.label;
      await api("POST", "/api/desk", { question: "Is provider-x fine for the checkout?", recommendation: "yes", why: "it has a test mode",
                                      project: ids.project, to: ids.people.alex, assumptions: [asm.label] });
      await new Promise((r) => setTimeout(r, 5500));   // the daemon keeps /api/home for 5 seconds per credential
      await page.reload({ waitUntil: "load" });
      await page.getByTestId("factory-home").waitFor();
      await settle(page);
      await api("POST", `/api/desk/${gone}/withdraw`, { reason: "seeded" });

      // A refusal shows under its row and the row stays: the daemon no longer has the item open
      const foot = row(page, "Is the footer wording fine");
      await foot.getByRole("button", { name: "Answer" }).click();
      await foot.getByTestId("answer-panel").getByTestId("answer-text").fill("Yes.");
      await foot.getByTestId("answer-panel").getByRole("button", { name: "Send" }).click();
      await foot.getByTestId("row-error").waitFor();
      ck(t("a refused answer says why under its row and the row stays"), /already/i.test(await foot.getByTestId("row-error").innerText()) && (await foot.count()) === 1,
         await foot.getByTestId("row-error").innerText());
      await foot.getByTestId("answer-panel").getByRole("button", { name: "Cancel" }).click();

      // A batch of calls on the desk: no free answer, it keeps them all in one step or opens the calls page
      const pay = row(page, "Is provider-x fine");
      ck(t("a batch of calls has no Answer box"), (await pay.getByRole("button", { name: "Answer" }).count()) === 0);
      ck(t("Change some opens the project's calls page"), (await pay.getByRole("link", { name: "Change some" }).getAttribute("href")) === `/projects/${ids.project}/calls`);
      await checkWords(page, ck, t("a batch of calls"));
      await pay.getByRole("button", { name: "Keep it" }).click();
      await pay.waitFor({ state: "detached", timeout: 15000 });
      const still = (await api("GET", "/api/desk?state=open")).body.desk;
      ck(t("Keep closes the desk item"), !still.some((d) => /provider-x/.test(JSON.stringify(d))), JSON.stringify(still).slice(0, 200));
      const kept = (await api("GET", `/api/projects/${ids.project}/assumptions`)).body;
      const asmNow = (kept.assumptions || kept).find((x) => x.label === asm.label);
      ck(t("and the call reads kept"), asmNow && asmNow.state === "kept", JSON.stringify(asmNow));
      await checkWords(page, ck, t("a refusal under a row"));
      await ctx.close();
    }

    // ── Later, live: Portal is kept for later from its page, leaves the home, and Start now brings it back ──
    {
      const { ctx, page } = await open("alex", 1440);
      const t = (s) => `later: ${s}`;
      await page.goto(`${BASE}/projects/${ids.projects.portal}`, { waitUntil: "load" });
      await page.getByTestId("factory-project").waitFor();
      ck(t("Portal's page offers Later, not Start now"), (await page.getByTestId("project-later").count()) === 1
         && (await page.getByTestId("project-start-now").count()) === 0);
      await page.getByTestId("project-later").click();
      await page.locator('[data-testid="factory-project"][data-state="later"]').waitFor({ timeout: 10000 });
      ck(t("its chip reads Later, and Start now takes the button's place"), (await page.getByTestId("project-chip").innerText()).trim().toLowerCase() === "later"
         && (await page.getByTestId("project-start-now").count()) === 1 && (await page.getByTestId("project-later").count()) === 0);
      await checkWords(page, ck, t("kept for later"));
      await shot(page, "live-project-later", 1440);
      await new Promise((r) => setTimeout(r, 5500));   // the daemon keeps /api/home for 5 seconds per credential
      await page.goto(`${BASE}/`, { waitUntil: "load" });
      await page.getByTestId("factory-home").waitFor();
      await settle(page);
      ck(t("the home no longer waits on Kim for Portal"), (await page.getByTestId("waiting-on-row").filter({ hasText: "Portal" }).count()) === 0
         && (await page.getByTestId("waiting-on-row").filter({ hasText: "Notes" }).count()) === 1);
      await page.goto(`${BASE}/projects?state=later`, { waitUntil: "load" });
      await page.getByText("Portal").first().waitFor({ timeout: 10000 }).catch(() => {});
      ck(t("All projects lists it under Later"), (await page.getByText("Portal").count()) >= 1);
      await page.goto(`${BASE}/projects/${ids.projects.portal}`, { waitUntil: "load" });
      await page.getByTestId("project-start-now").click();
      await page.locator('[data-testid="factory-project"]:not([data-state="later"])').waitFor({ timeout: 10000 });
      ck(t("Start now brings it back: Later is offered again"), (await page.getByTestId("project-later").count()) === 1);
      await ctx.close();
    }

    // ── a long question, a long list ──
    for (const width of [390, 1440]) {
      const t = (s) => `@${width}: ${s}`;
      const { ctx, page } = await open("alex", width);
      const oneRead = expectOneCall(page, "/api/home");
      await page.goto(`${BASE}/`, { waitUntil: "load" });
      await page.getByTestId("factory-home").waitFor();
      await settle(page);
      await oneRead(ck, t("long text"));
      ck(t("a long question wraps and never pushes the page wider"), (await overflow(page)) <= 0, `${await overflow(page)}px over`);
      await shot(page, "long-text", width);
      await checkWords(page, ck, t("long text"));
      await ctx.close();
    }
    for (let i = 1; i <= 12; i++) await api("POST", `/api/projects/${ids.project}/questions`, { repo: "acme/web", text: `Should ticket ${i} wait for the sign-off?` }, ids.keys.kim);
    await new Promise((r) => setTimeout(r, 5500));   // the daemon keeps /api/home for 5 seconds per credential
    for (const width of [390, 1440]) {
      const t = (s) => `@${width}: ${s}`;
      const { ctx, page } = await open("alex", width);
      const oneRead = expectOneCall(page, "/api/home");
      await page.goto(`${BASE}/`, { waitUntil: "load" });
      await page.getByTestId("factory-home").waitFor();
      await settle(page);
      await oneRead(ck, t("many items"));
      const rows = page.getByTestId("waiting-row");
      await page.waitForFunction(() => document.querySelectorAll('[data-testid="waiting-row"]').length >= 8, null, { timeout: 4000 }).catch(() => {});
      ck(t("a long list shows eight rows and Show all"), (await rows.count()) === 8 && (await page.getByRole("button", { name: /Show all/ }).count()) === 1,
         JSON.stringify({ rows: await rows.count(), head: await page.getByTestId("home-headline").innerText(), titles: (await rows.allInnerTexts()).map((x) => x.slice(0, 40)) }));
      await page.getByRole("button", { name: /Show all/ }).click();
      ck(t("Show all lists every item"), (await rows.count()) === 13, JSON.stringify((await rows.allInnerTexts()).map((x) => x.replace(/\s+/g, " ").slice(0, 70))));
      ck(t("no horizontal scroll with all of them"), (await overflow(page)) <= 0);
      await shot(page, "many-items", width);
      await checkWords(page, ck, t("many items"));
      await ctx.close();
    }

    // ── a cell of one person keeps the v2 list ──
    for (const width of [390, 1440]) {
      const t = (s) => `@${width}: ${s}`;
      const { ctx, page } = await open("solo", width);
      const oneRead = expectOneCall(page, "/api/home");
      await page.goto(`http://127.0.0.1:${SOLO}/`, { waitUntil: "load" });
      const soloUp = await page.getByTestId("factory-home").waitFor({ timeout: 15000 }).then(() => true, () => false);
      ck(t("a cell of one shows its home"), soloUp);
      await oneRead(ck, t("one person"));
      ck(t("the v2 list: rows with no sender square, Keep and Allow as before"),
         (await page.getByTestId("waiting-row").count()) >= 3 && (await page.getByTestId("waiting-from").count()) === 0
         && (await page.getByRole("button", { name: "Allow" }).count()) >= 1);
      await shot(page, "one-person", width);
      await checkWords(page, ck, t("one person"));
      await ctx.close();
    }
    // ── You are waiting on, from the sample states ──
    for (const width of [390, 1440]) {
      const t = (s) => `@${width}: ${s}`;
      const ctx = await browser.newContext({ viewport: { width, height: width === 390 ? 844 : 900 } });
      await ctx.addInitScript((tok) => { try { localStorage.setItem("taresSamples", "1"); localStorage.setItem("tares_token", tok); } catch { /* blocked */ } }, ids.keys.alex);
      const page = await ctx.newPage();
      const want = { threePeople: 2, longText: 1, nothingWaitingTeam: 0, manyItems: 0, privateNotOn: 2 };
      for (const [sample, n] of Object.entries(want)) {
        await page.goto(`${BASE}/?sample=${sample}`, { waitUntil: "load" });
        await page.getByTestId("factory-home").waitFor();
        await settle(page);
        ck(t(`${sample}: ${n} rows under You are waiting on`), (await page.getByTestId("waiting-on-row").count()) === n, String(await page.getByTestId("waiting-on-row").count()));
        if (!n) ck(t(`${sample}: no section when nothing is waited on`), (await page.getByTestId("waiting-on").count()) === 0);
        ck(t(`${sample}: no horizontal scroll`), (await overflow(page)) <= 0);
        await checkWords(page, ck, t(`${sample} waiting on`));
      }
      await page.goto(`${BASE}/?sample=threePeople`, { waitUntil: "load" });
      await page.getByTestId("waiting-on").waitFor();
      ck(t("no repo chips, no Hand over anyway and no Remind"), (await page.getByTestId("waiting-on-chip").count()) === 0
         && (await page.getByRole("button", { name: "Hand over anyway" }).count()) === 0
         && (await page.getByRole("button", { name: "Remind" }).count()) === 0);
      await ctx.close();
    }
    // ── the side column, from the sample states: your crew's limit, the team's crews, the repos you own ──
    for (const width of [390, 1440]) {
      const t = (s) => `@${width}: ${s}`;
      const ctx = await browser.newContext({ viewport: { width, height: width === 390 ? 844 : 900 } });
      await ctx.addInitScript((tok) => { try { localStorage.setItem("taresSamples", "1"); localStorage.setItem("tares_token", tok); } catch { /* blocked */ } }, ids.keys.alex);
      const page = await ctx.newPage();
      const go = async (sample) => {
        await page.goto(`${BASE}/?sample=${sample}`, { waitUntil: "load" });
        await page.getByTestId("factory-home").waitFor();
        await settle(page);
      };
      const n = (id) => page.getByTestId(id).count();
      const texts = (id) => page.getByTestId(id).allInnerTexts();
      const showAll = () => page.getByRole("button", { name: /Show all \d+ crews/ });

      await go("threePeople");
      ck(t("your crew shows the limit in words and the team's"), /The team allows up to merge\. Yours can be stricter, not looser\./.test((await texts("crew-limit-text"))[0] ?? "")
         && (await texts("crew-limit-mine"))[0] === "You approve each merge", JSON.stringify(await texts("crew-limit")));
      ck(t("Autonomy links to the crew settings"), (await page.getByRole("link", { name: "Autonomy" }).getAttribute("href")) === "/settings#crew");
      ck(t("one other crew is a row, and your own crew is not"), (await n("team-crew-row")) === 1
         && /Kim's crew/.test((await texts("team-crew-row"))[0] ?? "") && !/Your crew/.test((await texts("team-crew-row")).join()), JSON.stringify(await texts("team-crew-row")));
      ck(t("a crew row says the machine, when it was heard and what it does"), /kim-mbp\. Building project-x and project-w\./.test((await texts("team-crew-row"))[0] ?? "")
         && /1 minute ago/.test((await texts("team-crew-row"))[0] ?? ""));
      ck(t("a crew not seen in a day is on one line, not a row"), (await n("quiet-crews")) === 1
         && (await texts("quiet-crews"))[0].trim() === "Not seen in a day: Lee's crew" && !/Lee/.test((await texts("team-crew-row")).join()), JSON.stringify(await texts("quiet-crews")));
      ck(t("three crews or fewer have no Show all"), (await showAll().count()) === 0);
      ck(t("two repos are listed with what waits on each"), (await n("repo-you-own")) === 2
         && /example-org\/service-a/.test((await texts("repo-you-own"))[0]) && /1 question open/.test((await texts("repo-you-own"))[0])
         && /nothing waiting/.test((await texts("repo-you-own"))[1]), JSON.stringify(await texts("repo-you-own")));
      ck(t("All owners links to Settings, Advanced"), (await page.getByRole("link", { name: "All owners" }).getAttribute("href")) === "/settings/advanced?tab=areas");
      ck(t("the Claude plan and the connected tools stay"), (await page.locator('section[aria-label="Kim\'s Claude plan"]').count()) === 1
         && (await page.locator('section[aria-label="Connected"]').count()) === 1);
      ck(t("no horizontal scroll"), (await overflow(page)) <= 0, `${await overflow(page)}px over`);
      await shot(page, "side-three-people", width);
      await checkWords(page, ck, t("side, three people"));

      await go("manyItems");
      ck(t("six crews: three rows, then Show all 6 crews"), (await n("team-crew-row")) === 3 && (await showAll().count()) === 1
         && (await showAll().innerText()) === "Show all 6 crews", String(await n("team-crew-row")));
      ck(t("the quiet crew is still one line, not the seventh"), (await n("quiet-crews")) === 1);
      if ((await showAll().count()) === 1) await showAll().click();
      ck(t("Show all lists six crews and the button is gone"), (await n("team-crew-row")) === 6 && (await showAll().count()) === 0, String(await n("team-crew-row")));
      ck(t("no horizontal scroll with all of them"), (await overflow(page)) <= 0);
      await shot(page, "side-many-items", width);
      await checkWords(page, ck, t("side, many items"));

      await go("longText");
      ck(t("a long crew name, machine, repo and quiet crew wrap and never push the page wider"), (await overflow(page)) <= 0
         && (await n("team-crew-row")) === 1 && (await n("repo-you-own")) === 1 && (await n("quiet-crews")) === 1, `${await overflow(page)}px over`);
      await shot(page, "side-long-text", width);
      await checkWords(page, ck, t("side, long text"));

      await go("nothingWaitingTeam");
      ck(t("nothing waits on any repo you own"), (await texts("repo-you-own")).every((x) => /nothing waiting/.test(x)) && (await n("repo-you-own")) === 2);
      await shot(page, "side-nothing-waiting", width);
      await checkWords(page, ck, t("side, nothing waiting"));

      await go("noOwnedRepos");
      ck(t("no repo owned: there is no Repos you own block or All owners link"), (await n("repos-you-own")) === 0
         && (await page.getByRole("link", { name: "All owners" }).count()) === 0 && (await n("team-crews")) === 1);
      await checkWords(page, ck, t("side, no owned repos"));

      await go("ownCrewQuiet");
      ck(t("your own quiet crew is never named under Not seen in a day"), (await n("quiet-crews")) === 1
         && (await texts("quiet-crews"))[0].trim() === "Not seen in a day: Lee's crew", JSON.stringify(await texts("quiet-crews")));
      await checkWords(page, ck, t("side, own crew quiet"));

      await go("onePerson");
      ck(t("a cell of one keeps today's side: no limit foot, team crews or repos you own"),
         (await n("crew-limit")) === 0 && (await n("team-crews")) === 0 && (await n("team-crew-row")) === 0 && (await n("quiet-crews")) === 0 && (await n("repos-you-own")) === 0);
      ck(t("a cell of one still has Autonomy and the plan"), (await page.getByRole("link", { name: "Autonomy" }).count()) === 1
         && (await page.locator('section[aria-label="Your Claude plan"]').count()) === 1);
      await ctx.close();
    }
    // ── the Claude plan says whose it is (TR-1020): the reading the seed sent is Kim's ──
    {
      const openHome = async (who, width) => {
        const o = await open(who, width);
        await o.page.goto(`${BASE}/`, { waitUntil: "load" });
        await o.page.getByTestId("factory-home").waitFor();
        await settle(o.page);
        return o;
      };
      const planSection = (page) => page.locator('section[aria-label$="Claude plan"]');
      const heading = async (page) => (await planSection(page).locator("h2").innerText()).trim();
      const label = (page) => planSection(page).getAttribute("aria-label");
      for (const width of [390, 1440]) {
        const t = (s) => `@${width}: ${s}`;
        for (const [who, want] of [["kim", "Your Claude plan"], ["alex", "Kim's Claude plan"], ["lee", "Kim's Claude plan"]]) {
          const { ctx, page } = await openHome(who, width);
          ck(t(`${who} reads "${want}"`), (await heading(page)) === want, await heading(page));
          ck(t(`${who}: the section's label says the same`), (await label(page)) === want, await label(page));
          ck(t(`${who}: the limits show`), /27% used/.test(await planSection(page).innerText()));
          ck(t(`${who}: no horizontal scroll`), (await overflow(page)) <= 0, `${await overflow(page)}px over`);
          await shot(page, `plan-${who}`, width);
          await checkWords(page, ck, t(`the plan, read by ${who}`));
          await ctx.close();
        }
        const { ctx, page, ctl } = await openHome("alex", width);
        ctl.planTitle = "Alexandria Featherstonehaugh-Montgomery-Smythe's Claude plan";
        await page.reload({ waitUntil: "load" });
        await page.getByTestId("factory-home").waitFor();
        await settle(page);
        ck(t("a long name wraps and never pushes the page wider"), (await heading(page)) === ctl.planTitle && (await overflow(page)) <= 0, `${await overflow(page)}px over`);
        await shot(page, "plan-long-name", width);
        await ctx.close();
      }
      // a reading sent with the cell's own token names nobody: nobody is told it is theirs
      const unclaimed = await fetch(`${BASE}/ingest/claude_code`, { method: "POST", headers: { "content-type": "application/x-ndjson", authorization: `Bearer ${ROOT}` },
        body: JSON.stringify({ type: "claude_plan", timestamp: new Date().toISOString(), usage_report: { rate_limits: { limits: [
          { kind: "session", percent: 40, severity: "normal", is_active: true }, { kind: "weekly_all", percent: 9, severity: "normal", is_active: false }] } } }) + "\n" });
      ck("a plan reading with no person arrives", unclaimed.status === 202, String(unclaimed.status));
      await new Promise((r) => setTimeout(r, 5500));   // past the home's five seconds of keeping
      for (const width of [390, 1440]) {
        for (const who of ["kim", "alex"]) {
          const { ctx, page } = await openHome(who, width);
          ck(`@${width}: ${who} reads "Claude plan" when the cell cannot tell whose it is`, (await heading(page)) === "Claude plan" && (await label(page)) === "Claude plan", await heading(page));
          ck(`@${width}: ${who}: the limits still show`, /40% used/.test(await planSection(page).innerText()));
          await shot(page, `plan-unknown-${who}`, width);
          await checkWords(page, ck, `@${width}: the plan nobody claims, read by ${who}`);
          await ctx.close();
        }
      }
    }
    // a cell of one person on its own daemon has none of the side column's team parts
    {
      const { ctx, page } = await open("solo", 1440);
      await page.goto(`http://127.0.0.1:${SOLO}/`, { waitUntil: "load" });
      await page.getByTestId("factory-home").waitFor({ timeout: 15000 }).catch(() => {});
      await settle(page);
      ck("a live cell of one has no team crews, quiet line, repos you own or limit foot",
         (await page.getByTestId("team-crews").count()) + (await page.getByTestId("quiet-crews").count()) + (await page.getByTestId("repos-you-own").count()) + (await page.getByTestId("crew-limit").count()) === 0);
      const NEW = ["waiting-on", "waiting-on-row", "projects-switch", "projects-mine", "projects-team", "hidden-row", "hidden-summary", "project-lock", "project-lead", "handover-dialog"];
      const seen = [];
      for (const id of NEW) if ((await page.getByTestId(id).count()) > 0) seen.push(id);
      ck("a live cell of one shows none of the team home's testids", seen.length === 0, seen.join());
      await ctx.close();
    }
    const soloHome = await fetch(`http://127.0.0.1:${SOLO}/api/home`).then((r) => r.json());
    ck("a cell of one person has no team keys in /api/home", !("you_are_waiting_on" in soloHome) && !("crews_quiet" in soloHome));
    // ── three people and no licence: the single-person home, and no enterprise code (TR-932) ──
    for (const width of [390, 1440]) {
      const t = (s) => `@${width}: ${s}`;
      const { ctx, page } = await open("unlicensed", width);
      const eeAsked = [];
      page.on("request", (rq) => { if (/\/assets\/ee-/.test(rq.url())) eeAsked.push(rq.url()); });
      await page.goto(`http://127.0.0.1:${UNLIC}/`, { waitUntil: "load" });
      const up = await page.getByTestId("factory-home").waitFor({ timeout: 15000 }).then(() => true, () => false);
      ck(t("three people, no licence: the home shows"), up);
      await settle(page);
      ck(t("three people, no licence: the single-person rows, no sender square, Keep and Allow as before"),
         (await page.getByTestId("waiting-row").count()) >= 3 && (await page.getByTestId("waiting-from").count()) === 0
         && (await page.getByRole("button", { name: "Allow" }).count()) >= 1);
      const NEW_UNLIC = ["waiting-on", "waiting-on-row", "projects-switch", "team-crews", "repos-you-own", "crew-limit", "decline-dialog"];
      const seenUnlic = [];
      for (const id of NEW_UNLIC) if ((await page.getByTestId(id).count()) > 0) seenUnlic.push(id);
      ck(t("three people, no licence: none of the team home's parts"), seenUnlic.length === 0, seenUnlic.join());
      ck(t("three people, no licence: no enterprise chunk was asked for"), eeAsked.length === 0, eeAsked.join());
      // TR-1051: the team's parts are not drawn, and the team samples are not offered
      const NOT_DRAWN = ["projects-switch", "project-lock", "project-lead", "hidden-row", "hidden-summary", "project-member", "team-crews", "team-crew-row", "quiet-crews", "repos-you-own", "crew-limit"];
      const drawn = [];
      for (const id of NOT_DRAWN) if ((await page.getByTestId(id).count()) > 0) drawn.push(id);
      ck(t("three people, no licence: no Mine and Team switch, no other crews, no Repos you own"), drawn.length === 0, drawn.join());
      ck(t("three people, no licence: the Autonomy foot of the crew card is today's"), (await page.getByRole("link", { name: "Autonomy" }).count()) === 1);
      {
        const sctx = await browser.newContext({ viewport: { width, height: width === 390 ? 844 : 900 } });
        await sctx.addInitScript(() => { try { localStorage.setItem("taresSamples", "1"); } catch { /* blocked */ } });
        const sp = await sctx.newPage();
        const asked2 = [];
        sp.on("request", (rq) => { if (/\/assets\/ee-/.test(rq.url())) asked2.push(rq.url()); });
        const live = await fetch(`http://127.0.0.1:${UNLIC}/api/home`).then((r) => r.json());
        const headline = async (name) => {
          await sp.goto(`http://127.0.0.1:${UNLIC}/?sample=${name}`, { waitUntil: "load" });
          await sp.getByTestId("factory-home").waitFor({ timeout: 15000 }).catch(() => {});
          await sp.waitForLoadState("networkidle");
          return (await sp.getByTestId("home-headline").innerText().catch(() => "")).trim();
        };
        const SINGLE = { busy: /^4 things wait for you\. 2 projects are building/, empty: /^No projects yet/ };
        const wrongSingle = [];
        for (const [name, re] of Object.entries(SINGLE)) { const h = await headline(name); if (!re.test(h)) wrongSingle.push(`${name}: ${h}`); }
        ck(t("three people, no licence: the single-person samples are offered"), wrongSingle.length === 0, wrongSingle.join(" | "));
        const TEAM = ["threePeople", "nothingWaitingTeam", "longText", "manyItems", "privateNotOn", "privateOnly", "noOwnedRepos", "ownCrewQuiet", "onePerson"];
        const offered = [];
        for (const name of TEAM) {
          const h = await headline(name);
          // a name that is not a sample shows the cell's own home (the cell's headline can move with the clock, so ask again)
          const now = await fetch(`http://127.0.0.1:${UNLIC}/api/home`).then((r) => r.json());
          if (h !== live.headline.trim() && h !== now.headline.trim()) offered.push(`${name}: ${h}`);
          for (const id of NOT_DRAWN) if ((await sp.getByTestId(id).count()) > 0) offered.push(`${name}: ${id}`);
        }
        ck(t("three people, no licence: none of the team samples is offered, so the live home shows"), offered.length === 0, offered.join(" | "));
        ck(t("three people, no licence: samples did not ask for the enterprise chunk"), asked2.length === 0, asked2.join());
        await sctx.close();
      }
      await shot(page, "three-people-no-licence", width);
      await checkWords(page, ck, t("three people, no licence"));
      await ctx.close();
    }
    const unlicHome = await fetch(`http://127.0.0.1:${UNLIC}/api/home`).then((r) => r.json());
    ck("three people, no licence: no team keys in /api/home",
       !["summary", "crews", "crews_quiet", "areas", "counts", "you_are_waiting_on"].some((k) => k in unlicHome), Object.keys(unlicHome).join());

    // ── TR-1074: Allow on a project page is done there, and the page stays ──
    for (const width of [1440, 390]) {
      const t = (s) => `allow on the project page @${width}: ${s}`;
      const file = async (to, command) => (await api("POST", "/api/desk", { question: "A station was refused. Allow it?", recommendation: "yes", why: "it needs it",
                                                                           project: ids.project, station: "shop-build-m1", command, to })).body.label;
      const firstCmd = `gh pr merge ${width}1`, secondCmd = `gh pr merge ${width}2`, kimCmd = `gh pr merge ${width}3`;
      const first = await file(ids.people.alex, firstCmd), second = await file(ids.people.alex, secondCmd), kims = await file(ids.people.kim, kimCmd);
      const { ctx, page, ctl } = await open("alex", width);
      const here = `${BASE}/projects/${ids.project}`;
      await page.goto(here, { waitUntil: "load" });
      await page.getByTestId("factory-project").waitFor();
      await row(page, firstCmd).waitFor({ timeout: 20000 });
      await page.evaluate(() => { window.__sameDocument = true; });   // gone if the page loads again
      const ad = page.getByTestId("allow-dialog");
      const allow = async (cmd) => {
        await row(page, cmd).getByTestId("waiting-allow").click();
        await ad.waitFor();
        await ad.getByRole("button", { name: "Allow", exact: true }).click();
      };
      ck(t("Allow is a button with no arrow to Home"), (await row(page, firstCmd).getByTestId("waiting-allow").count()) === 1
         && !(await row(page, firstCmd).getByTestId("waiting-allow").innerText()).includes("↗"));
      await allow(firstCmd);
      await row(page, firstCmd).waitFor({ state: "detached" });
      await checkWords(page, ck, t("the first one allowed"));
      ck(t("the page stayed on the project"), page.url() === here, page.url());
      ck(t("the first request reached the cell"), ctl.posts.some((x) => x.path === `/api/desk/${first}/allow`), JSON.stringify(ctl.posts));
      await allow(secondCmd);
      await row(page, secondCmd).waitFor({ state: "detached" });
      ck(t("a second one in a row, with no trip back"), page.url() === here && ctl.posts.some((x) => x.path === `/api/desk/${second}/allow`), page.url());
      const answered = (await api("GET", "/api/desk?state=answered")).body.desk.map((d) => d.label);
      ck(t("both are answered on the cell"), answered.includes(first) && answered.includes(second), answered.join());
      const grants = await api("GET", `/api/grants?project=${ids.project}`);
      ck(t("both grants are kept"), [firstCmd, secondCmd].every((c) => JSON.stringify(grants.body).includes(c)), JSON.stringify(grants.body).slice(0, 300));
      // Kim's request is Kim's: on Alex's view it sits under Waiting for others, named for her, with
      // nothing to press (TR-1223 follow-up; before, it had an Allow the cell then refused)
      const others = page.getByTestId("waiting-other-row").filter({ hasText: kimCmd });
      ck(t("someone else's request is under Waiting for others, named for them, with no Allow"),
         (await others.count()) === 1 && /^Permission for Kim/.test((await others.textContent()).trim())
         && (await others.locator("button").count()) === 0 && (await row(page, kimCmd).count()) === 0,
         (await others.count()) ? await others.textContent() : "missing");
      await checkWords(page, ck, t("waiting for others"));
      const refused = await api("POST", `/api/desk/${kims}/allow`, { words: kimCmd, scope: "project" }, ids.keys.alex);
      ck(t("the cell still refuses Alex allowing it, and it stays open"),
         refused.status === 403 && (await api("GET", "/api/desk?state=open")).body.desk.some((d) => d.label === kims),
         `${refused.status}`);
      ck(t("the page never loaded again"), await page.evaluate(() => window.__sameDocument === true));
      ck(t("no horizontal scroll"), (await overflow(page)) <= 0, String(await overflow(page)));
      await ctx.close();
    }
  } finally {
    if (browser) await browser.close();
    for (const { daemon, home } of started) {
      try { process.kill(-daemon.pid); } catch { try { daemon.kill(); } catch { /* gone */ } }
      fs.rmSync(home, { recursive: true, force: true });
    }
  }
  console.log(`\n${pass} passed, ${fail} failed`);
  process.exit(fail ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(1); });
