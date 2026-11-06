/*
 * Browser e2e for TR-741: the factory home on a cell with more than one person, "Waiting for you" with its actions.
 *
 * Starts a scratch daemon (TARES_EDITION=factory, auth on) on TARES_E2E_PORT (default 8857), MCP port +20, and a second
 * one of a single person on port +2, MCP +22, each in a clean environment and a fresh data directory; stops only those.
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

const PORT = Number(process.env.TARES_E2E_PORT || 8857);
const SOLO = PORT + 2;
const BASE = `http://127.0.0.1:${PORT}`;
const ROOT = "root-token-team-home-e2e";
const SHOTS = process.env.SHOTS_DIR;
const PY = process.env.TARES_E2E_PYTHON || "python3";

let pass = 0, fail = 0;
const ck = (label, cond, detail = "") => {
  cond ? pass++ : fail++;
  console.log((cond ? "  ok   " : "  FAIL ") + label + (cond ? "" : `   ${detail}`));
};

// The Yours / All dropdown on the home's Projects (TR-1023): what it shows, its two options, and choosing one.
const switchLabel = async (page) => (await page.getByTestId("projects-switch").innerText()).trim();
async function switchOptions(page) {
  await page.getByTestId("projects-switch").click();
  const texts = (await page.getByRole("option").allInnerTexts()).map((x) => x.trim());
  await page.keyboard.press("Escape");
  return texts;
}
async function switchTo(page, view) {
  await page.getByTestId("projects-switch").click();
  await page.getByRole("option", { name: view === "team" ? /^All/ : /^Yours/ }).click();
}

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
function startDaemon(port, extra) {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "tares-team-home-e2e-"));
  const env = cleanEnv({ TARES_EDITION: "factory", TARES_HOME: home, TARES_PORT: String(port), TARES_MCP_PORT: String(port + 20),
                         TARES_OTLP_GRPC_PORT: "off", TARES_UI_DIST: process.env.TARES_UI_DIST || path.join(UI_DIR, "dist"),
                         PYTHONPATH: REPO, ...extra });
  const daemon = spawn(tares, ["up", "--port", String(port), "--data-dir", home], { cwd: REPO, stdio: "ignore", env, detached: true });
  started.push({ daemon, home });
  console.log(`  daemon: pid ${daemon.pid}, port ${port}`);
  return daemon;
}

const overflow = (page) => page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);

(async () => {
  startDaemon(PORT, { TARES_AUTH_TOKEN: ROOT });
  startDaemon(SOLO, {});
  let browser;
  try {
    ck("daemons up", (await waitFor(`${BASE}/health`)) && (await waitFor(`http://127.0.0.1:${SOLO}/health`)));
    const seeded = spawnSync(PY, [path.join(__dirname, "seed-team-home.py"), String(PORT), ROOT], { encoding: "utf8" });
    ck("seed ran", seeded.status === 0, (seeded.stderr || seeded.stdout || "").slice(-300));
    const solo = spawnSync(PY, [path.join(__dirname, "seed-factory-home.py"), String(SOLO)], { encoding: "utf8" });
    ck("seed of one person ran", solo.status === 0, (solo.stderr || solo.stdout || "").slice(-300));
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
      const token = who === "solo" ? null : ids.keys[who];
      await ctx.addInitScript((t) => { try { if (t) localStorage.setItem("tares_token", t); } catch { /* blocked */ } }, token);
      const page = await ctx.newPage();
      const ctl = { fail: false, hold: null, reads: 0, posts: [], more: 0 };
      await page.route(/\/api\/home(\?|$)/, async (route) => {
        ctl.reads++;
        if (ctl.hold) await ctl.hold;
        if (ctl.fail) return route.fulfill({ status: 500, contentType: "application/json", body: JSON.stringify({ detail: "down" }) });
        if (ctl.more) {   // a sign-off over more tickets than the daemon lists by title
          const res = await route.fetch();
          const data = await res.json();
          for (const it of data.waiting_for_you || []) for (const a of it.actions || []) if (a.do === "sign_off") a.more_tickets = ctl.more;
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
        const rows = page.getByTestId("waiting-row");
        ck(t("seven things wait for Alex"), (await rows.count()) === 7, String(await rows.count()));
        const senders = await rows.evaluateAll((els) => els.map((e) => e.querySelector('[data-testid="waiting-from"]')?.getAttribute("aria-label")));
        ck(t("each row says who it is from: Kim four times, the crew three"),
           senders.filter((s) => s === "From Kim").length === 4 && senders.filter((s) => s === "From Your crew").length === 3, JSON.stringify(senders));
        ck(t("the headline counts them"), /7 things wait for you/.test(await page.getByTestId("home-headline").innerText()), await page.getByTestId("home-headline").innerText());
        const buttons = async (text) => (await row(page, text).getByRole("button").allInnerTexts()).map((s) => s.trim()).sort();
        ck(t("the question has Answer"), (await buttons("Which page does the checkout")).join() === "Answer");
        ck(t("the sign-off has Sign off"), (await buttons("Kim cannot hand the project")).includes("Sign off"));
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


    // ── TR-761, live: what Alex waits on, Projects (Mine and Team), the side column; Kim's view of Shop ──
    const TEAM_NAMES = ["Shop", "Portal", "Notes", "Billing", "Search", "Mobile"];
    const chipsOf = (loc) => loc.getByTestId("waiting-on-chip").evaluateAll((els) => els.map((e) => `${e.getAttribute("data-state")}:${e.textContent}`).sort());
    let kim = null;   // Kim's page at 1440 stays open: she reads Shop again once Alex has signed off
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
      ck(t("Portal: acme/api waits for Kim, acme/docs has no owner"), JSON.stringify(await chipsOf(portal)) === JSON.stringify(["no_owner:acme/docs · no owner", "waiting:acme/api · Kim · waiting"]), JSON.stringify(await chipsOf(portal)));
      ck(t("Notes: acme/api waits for Kim, acme/web is Alex's own"), JSON.stringify(await chipsOf(notes)) === JSON.stringify(["waiting:acme/api · Kim · waiting", "yours:acme/web · you"]), JSON.stringify(await chipsOf(notes)));
      const btns = async (loc) => (await loc.getByRole("button").allInnerTexts()).map((x) => x.trim());
      ck(t("both rows have Hand over anyway, and Remind is not offered"), (await btns(portal)).includes("Hand over anyway") && (await btns(notes)).includes("Hand over anyway")
         && (await page.getByRole("button", { name: "Remind" }).count()) === 0);
      ck(t("Set an owner is on Portal only, and links to Settings, Advanced"), (await portal.getByRole("link", { name: "Set an owner" }).getAttribute("href")) === "/settings/advanced?tab=areas"
         && (await notes.getByRole("link", { name: "Set an owner" }).count()) === 0);
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

      // d. Projects: Mine and Team
      const rowsN = () => page.getByTestId("project-row").count();
      const prow = (name) => page.getByTestId("project-row").filter({ hasText: name });
      const everything = () => page.evaluate(() => document.body.innerText);
      const opts = await switchOptions(page);
      ck(t("the dropdown offers Yours (3) and All (6)"), JSON.stringify(opts) === JSON.stringify(["Yours (3)", "All (6)"]), JSON.stringify(opts));
      ck(t("Yours is chosen and lists Shop, Portal and Notes"), (await switchLabel(page)) === "Yours (3)" && (await rowsN()) === 3
         && (await prow("Shop").count()) === 1 && (await prow("Portal").count()) === 1 && (await prow("Notes").count()) === 1, String(await rowsN()));
      ck(t("Mine has one line about the hidden project and no stub row"), (await n("hidden-summary")) === 1 && (await n("hidden-row")) === 0
         && (await texts("hidden-summary"))[0].trim() === "1 private project you are not on, led by Lee", JSON.stringify(await texts("hidden-summary")));
      const private_ = ids.private;
      let bodyText = await everything();
      const leaks = async () => [private_.name, private_.goal].filter((w) => bodyText.includes(w));
      ck(t("Mine names nothing of the private project"), (await leaks()).length === 0 && !JSON.stringify(alexHome).includes(private_.name) && !JSON.stringify(alexHome).includes(private_.goal), (await leaks()).join());
      await shot(page, "live-projects-mine", width);
      await checkWords(page, ck, t("live mine"));
      const asked = page.waitForRequest((r) => r.url().endsWith("/api/home?projects=team"), { timeout: 5000 }).then(() => true, () => false);
      await switchTo(page, "team");
      ck(t("choosing All makes the page read /api/home?projects=team"), await asked);
      await page.waitForFunction(() => document.querySelectorAll('[data-testid="project-row"]').length === 6, null, { timeout: 4000 }).catch(() => {});
      ck(t("Team lists six projects"), (await rowsN()) === 6 && (await Promise.all(TEAM_NAMES.map((x) => prow(x).count()))).every((c) => c === 1), String(await rowsN()));
      ck(t("Team has the stub, Private, led by Lee, and no summary line"), (await n("hidden-row")) === 1 && (await texts("hidden-row"))[0].trim() === "Private, led by Lee"
         && (await n("hidden-summary")) === 0, JSON.stringify(await texts("hidden-row")));
      bodyText = await everything();
      ck(t("Team names nothing of the private project either"), (await leaks()).length === 0, (await leaks()).join());
      const rowText = async (name) => ((await prow(name).count()) ? prow(name).first().innerText() : "");
      ck(t("Team shows Led by Lee on Search and Mobile and Led by Kim on Shop"), /Led by Lee/.test(await rowText("Search")) && /Led by Lee/.test(await rowText("Mobile"))
         && /Led by Kim/.test(await rowText("Shop")));
      ck(t("no horizontal scroll"), (await overflow(page)) <= 0, `${await overflow(page)}px over`);
      await shot(page, "live-projects-team", width);
      await checkWords(page, ck, t("live team"));

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
      await page.waitForURL(/\/settings\/advanced/, { timeout: 10000 }).catch(() => {});
      const areasTab = page.locator('button.pnav-link[aria-current="page"]');
      await areasTab.first().waitFor({ timeout: 10000 }).catch(() => {});
      ck(t("All owners opens Settings with the Areas tab selected"), (await areasTab.allInnerTexts()).join("|").trim() === "Areas", (await areasTab.allInnerTexts()).join("|"));
      await ctx.close();
    }
    {
      // Kim leads Shop, and waits on Alex for acme/web and on nobody for acme/docs
      const { ctx, page } = await open("kim", 1440);
      await page.goto(`${BASE}/`, { waitUntil: "load" });
      await page.getByTestId("factory-home").waitFor();
      await settle(page);
      const shopRow = page.getByTestId("waiting-on-row").filter({ hasText: "Shop" });
      const chips = await chipsOf(shopRow);
      ck("kim: Shop waits for Alex on acme/web, acme/docs has no owner, acme/api is her own", (await page.getByTestId("waiting-on-row").count()) === 1
         && JSON.stringify(chips) === JSON.stringify(["no_owner:acme/docs · no owner", "waiting:acme/web · Alex · waiting", "yours:acme/api · you"]), JSON.stringify(chips));
      await shot(page, "live-kim", 1440);
      await checkWords(page, ck, "kim: live");
      kim = { ctx, page };
    }

    // ── acting, as Alex at 1440 ──
    // ── F6: a sign-off over more tickets than the daemon lists by title says how many more ──
    {
      const { ctx, page, ctl } = await open("alex", 1440);
      const t = (s) => `more tickets: ${s}`;
      ctl.more = 3;
      await page.goto(`${BASE}/`, { waitUntil: "load" });
      await page.getByTestId("factory-home").waitFor();
      await settle(page);
      await row(page, "Kim cannot hand the project").getByRole("button", { name: "Sign off" }).click();
      const sd = page.getByTestId("signoff-dialog");
      await sd.waitFor();
      ck(t("the box lists the titles and then says 'and 3 more'"), (await sd.getByTestId("signoff-more").count()) === 1
         && (await sd.getByTestId("signoff-more").innerText()).trim() === "and 3 more", await sd.getByTestId("signoff-tickets").innerText());
      await checkWords(page, ck, t("sign-off with more"));
      await ctx.close();
    }
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
      await row(page, "project-x").filter({ hasText: "Sign-off" }).getByRole("button", { name: "Sign off" }).click();
      const sd = page.getByTestId("signoff-dialog");
      ck(t("Sign off opens its box"), await sd.waitFor({ timeout: 3000 }).then(() => true, () => false));
      await sd.getByTestId("signoff-confirm").click();
      await note.first().waitFor({ timeout: 3000 }).catch(() => {});
      ck(t("confirming says nothing was sent, and the box closes"), (await note.count()) >= 1 && (await sd.count()) === 0, String(await note.count()));
      ck(t("the row stays and the cell was not asked to change anything"), (await row(page, "project-x").filter({ hasText: "Sign-off" }).count()) === 1 && posts.length === 0, JSON.stringify(posts));
      await note.first().waitFor({ state: "detached", timeout: 15000 }).catch(() => {});   // let the first one go, so the next is its own
      await row(page, "Try migrations on staging").getByRole("button", { name: "Agree" }).click();
      await note.first().waitFor({ timeout: 3000 }).catch(() => {});
      ck(t("Agree says so too and sends nothing"), (await note.count()) >= 1 && posts.length === 0, JSON.stringify(posts));
      await checkWords(page, ck, t("after a sample action"));
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
      ck(t("Cancel closes it and asks the daemon for nothing"), ctl.posts.length === 0 && (await rows.count()) === 7);
      await q.getByRole("button", { name: "Answer" }).click();
      await panel.getByTestId("answer-text").fill("The checkout lives on /pay.");
      await panel.getByRole("button", { name: "Send" }).click();
      await q.waitFor({ state: "detached" });
      const qs = (await api("GET", `/api/projects/${ids.project}/questions`)).body.questions;
      ck(t("the answer reached the spec question, from the console"), qs[0].answer === "The checkout lives on /pay." && qs[0].via === "console" && qs[0].state === "answered", JSON.stringify(qs[0]));
      ck(t("its row is gone and six remain"), (await rows.count()) === 6, String(await rows.count()));

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
      ck(t("five remain"), (await rows.count()) === 5, String(await rows.count()));

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
      ck(t("TR-1021: five remain again"), (await rows.count()) === 5, String(await rows.count()));

      // Sign off
      const so = row(page, "Kim cannot hand the project");
      await so.getByRole("button", { name: "Sign off" }).click();
      const sd = page.getByTestId("signoff-dialog");
      await sd.waitFor();
      ck(t("Sign off lists the ticket it covers"), (await sd.getByTestId("signoff-tickets").innerText()).trim() === "Web page");
      await checkWords(page, ck, t("sign-off dialog"));
      if (SHOTS) await shot(page, "sign-off-dialog", 1440);
      await sd.getByRole("button", { name: "Cancel" }).click();
      await sd.waitFor({ state: "detached" });

      // F14, F8: Kim gives acme/web to Lee while the page is open; the daemon then refuses Alex's sign-off (403)
      const moved = await api("PUT", "/api/areas/acme/web", { person_id: ids.people.lee }, ids.keys.kim);
      ck(t("acme/web has a new owner, Lee, and the page still lists the sign-off"), moved.status === 200 && (await so.count()) === 1, JSON.stringify(moved));
      ctl.posts.length = 0;
      await so.getByRole("button", { name: "Sign off" }).click();
      await sd.waitFor();
      await sd.getByTestId("signoff-confirm").click();
      const derr = sd.getByTestId("dialog-error");
      await derr.waitFor({ timeout: 5000 }).catch(() => {});
      const derrText = (await derr.count()) ? (await derr.innerText()).trim() : "";
      ck(t("F14: a non-owner is told so in the box, in the daemon's words"), /Lee's: only the area owner signs off/.test(derrText), derrText);
      ck(t("F14: the box stays open, the row stays and nothing was signed off"), (await sd.count()) === 1 && (await so.count()) === 1
         && ctl.posts.some((x) => x.path === `/api/projects/${ids.project}/signoffs`), JSON.stringify(ctl.posts));
      ck(t("F8: one alert on the page while the box is open, not a second one under the row"), (await page.locator('[role="alert"]').count()) === 1, String(await page.locator('[role="alert"]').count()));
      await checkWords(page, ck, t("sign-off refusal"));
      await sd.getByRole("button", { name: "Cancel" }).click();
      await sd.waitFor({ state: "detached" });
      ck(t("F8: closing the box leaves the page without an alert"), (await page.locator('[role="alert"]').count()) === 0, String(await page.locator('[role="alert"]').count()));
      const back = await api("PUT", "/api/areas/acme/web", { person_id: ids.people.alex }, ids.keys.kim);
      ck(t("Kim gives acme/web back to Alex"), back.status === 200, JSON.stringify(back));

      await so.getByRole("button", { name: "Sign off" }).click();
      await sd.waitFor();
      await sd.getByTestId("signoff-confirm").click();
      await so.waitFor({ state: "detached" });
      const owed = (await api("GET", `/api/projects/${ids.project}/signoffs`, undefined, ids.keys.kim)).body;
      const webRow = owed.signoffs.find((r) => r.repo === "acme/web");
      const apiRow = owed.signoffs.find((r) => r.repo === "acme/api");
      ck(t("acme/web is signed off by its owner, from the console"), !!webRow && webRow.signed_off === true && !!webRow.owner && webRow.owner.id === ids.people.alex && !!webRow.via && !!webRow.at, JSON.stringify(webRow));
      ck(t("acme/api, which Alex does not own, is not signed off"), !!apiRow && apiRow.signed_off === false, JSON.stringify(apiRow));
      ck(t("four remain"), (await rows.count()) === 4, String(await rows.count()));

      // Kim, who leads Shop, sees acme/web signed off once her home is read again
      await new Promise((r) => setTimeout(r, 5500));   // the daemon keeps /api/home for 5 seconds per credential
      await kim.page.reload({ waitUntil: "load" });
      await kim.page.getByTestId("factory-home").waitFor();
      await settle(kim.page);
      const kimChips = await chipsOf(kim.page.getByTestId("waiting-on-row").filter({ hasText: "Shop" }));
      ck(t("Kim now sees acme/web signed off by Alex"), JSON.stringify(kimChips) === JSON.stringify(["no_owner:acme/docs · no owner", "signed_off:acme/web · Alex · signed off", "yours:acme/api · you"]), JSON.stringify(kimChips));
      await shot(kim.page, "live-kim-signed-off", 1440);
      await checkWords(kim.page, ck, t("kim after the sign-off"));
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

      // An item with an open assumption: the daemon wants each one kept or overturned, and says so under the row
      const pay = row(page, "Is provider-x fine");
      await pay.getByRole("button", { name: "Answer" }).click();
      await pay.getByTestId("answer-panel").getByTestId("answer-text").fill("Yes.");
      await pay.getByTestId("answer-panel").getByRole("button", { name: "Send" }).click();
      await pay.getByTestId("row-error").waitFor();
      const why = (await pay.getByTestId("row-error").innerText()).trim();
      ck(t("an answer on an item with an open assumption shows the daemon's sentence and the row stays"), /assumption/i.test(why) && (await pay.count()) === 1, why);
      const still = (await api("GET", "/api/desk?state=open")).body.desk;
      ck(t("the item is still open on the daemon"), still.some((d) => /provider-x/.test(d.question || JSON.stringify(d))), JSON.stringify(still).slice(0, 200));
      await checkWords(page, ck, t("a refusal under a row"));
      await ctx.close();
    }

    // ── Hand over anyway, live: Notes has a new lead after the page listed it (403), Portal goes to the crew ──
    {
      const { ctx, page, ctl } = await open("alex", 1440);
      const t = (s) => `hand over: ${s}`;
      await page.goto(`${BASE}/`, { waitUntil: "load" });
      await page.getByTestId("factory-home").waitFor();
      await settle(page);
      const won = page.getByTestId("waiting-on-row");
      const portal = won.filter({ hasText: "Portal" }), notes = won.filter({ hasText: "Notes" });
      const dialog = page.getByTestId("handover-dialog"), confirm = page.getByTestId("handover-confirm"), reason = page.getByTestId("handover-reason");
      const crewOf = async (id) => (await api("GET", `/api/projects/${id}/crew`)).body.handover;
      ck(t("both rows are listed before anything is done"), (await portal.count()) === 1 && (await notes.count()) === 1 && !(await crewOf(ids.projects.portal)) && !(await crewOf(ids.projects.notes)));

      // F4: while the page is open, Alex gives Notes to Kim; the daemon then refuses him (403)
      const lead = await api("PATCH", `/api/projects/${ids.projects.notes}`, { lead_person_id: ids.people.kim }, ids.keys.alex);
      ck(t("Notes has a new lead, Kim, and the page still lists it"), lead.status === 200 && (await notes.count()) === 1, JSON.stringify(lead));
      await notes.getByRole("button", { name: "Hand over anyway" }).click();
      await dialog.waitFor();
      await reason.fill("Kim is out until Monday.");
      await confirm.click();
      const err = page.getByTestId("dialog-error");
      await err.waitFor({ timeout: 5000 }).catch(() => {});
      const errText = (await err.count()) ? (await err.innerText()).trim() : "";
      ck(t("a non-lead is told so: Only the lead of Notes can hand it over."), errText === "Only the lead of Notes can hand it over.", errText);
      ck(t("the box stays open, its row stays and nothing was handed over"), (await dialog.count()) === 1 && (await notes.count()) === 1 && !(await crewOf(ids.projects.notes)));
      await checkWords(page, ck, t("refusal"));
      await page.getByRole("button", { name: "Cancel" }).click();
      await dialog.waitFor({ state: "detached" });
      const back = await api("PATCH", `/api/projects/${ids.projects.notes}`, { lead_person_id: ids.people.alex }, ids.keys.kim);   // Alex leads Notes again, so the later counts hold
      ck(t("Kim gives Notes back to Alex"), back.status === 200, JSON.stringify(back));
      ctl.posts.length = 0;

      // TR-895 F2: a key that can only read is told so, not that the lead must do it
      {
        const made = await api("POST", "/api/keys", { name: "reader-laptop", scopes: ["read"], person: { email: "alex-seed@example.com", name: "Alex" } });
        ck(t("a read key was made for Alex"), made.status === 200 || made.status === 201, JSON.stringify(made));
        ids.keys.reader = made.body.secret;
        const rd = await open("reader", 1440);
        await rd.page.goto(`${BASE}/`, { waitUntil: "load" });
        await rd.page.getByTestId("factory-home").waitFor();
        await settle(rd.page);
        const rrow = rd.page.getByTestId("waiting-on-row").filter({ hasText: "Portal" });
        await rrow.getByRole("button", { name: "Hand over anyway" }).click();
        await rd.page.getByTestId("handover-dialog").waitFor();
        await rd.page.getByTestId("handover-reason").fill("Kim is out until Monday.");
        await rd.page.getByTestId("handover-confirm").click();
        const rerr = rd.page.getByTestId("dialog-error");
        await rerr.waitFor({ timeout: 5000 }).catch(() => {});
        const rtext = (await rerr.count()) ? (await rerr.innerText()).trim() : "";
        ck(t("a read key is told it can only read, naming the key"), rtext === "The key reader-laptop can only read, so it cannot hand a project over. Sign in with a key that can.", rtext);
        await checkWords(rd.page, ck, t("read key refusal"));
        await rd.ctx.close();
      }

      // F6: Portal, as its lead
      await portal.getByRole("button", { name: "Hand over anyway" }).click();
      await dialog.waitFor();
      ck(t("Hand over is off with no reason, and with only spaces"), (await confirm.isDisabled()) && (await reason.fill("   "), await confirm.isDisabled()));
      const why = "Kim is out until Monday, so the ticket should not wait.";
      await reason.fill(why);
      ck(t("Hand over is on with a reason"), await confirm.isEnabled());
      await shot(page, "live-hand-over-dialog", 1440);
      await checkWords(page, ck, t("dialog"));
      await api("GET", "/api/home", undefined, ids.keys.alex);   // the daemon holds this answer for 5 seconds: it still lists Portal
      await confirm.click();
      await dialog.waitFor({ state: "detached", timeout: 5000 }).catch(() => {});
      ck(t("the row leaves at once, though the daemon's cached answer still lists it"), (await portal.count()) === 0 && (await notes.count()) === 1, String(await portal.count()));
      const post = ctl.posts.find((x) => x.path === `/api/projects/${ids.projects.portal}/handover`);
      ck(t("the page asked /api/projects/<id>/handover with {override_reason}"), !!post && JSON.stringify(JSON.parse(post.body || "{}")) === JSON.stringify({ override_reason: why }), JSON.stringify(post));
      const done = await crewOf(ids.projects.portal);
      ck(t("the daemon recorded the hand-over with the reason"), !!done && JSON.stringify(done).includes(why), JSON.stringify(done));
      ck(t("Notes was not handed over"), !(await crewOf(ids.projects.notes)));
      await shot(page, "live-handed-over", 1440);
      await checkWords(page, ck, t("handed over"));
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
      ck(t("Show all lists every item"), (await rows.count()) === 14, JSON.stringify((await rows.allInnerTexts()).map((x) => x.replace(/\s+/g, " ").slice(0, 70))));
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
    // ── You are waiting on, from the sample states: the section, its chips and the Hand over anyway box ──
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
      const chips = await page.getByTestId("waiting-on-chip").evaluateAll((els) => els.map((e) => `${e.getAttribute("data-state")}:${e.textContent}`));
      ck(t("a chip per repo, with its state and who"), chips.some((c) => c.startsWith("waiting:")) && chips.some((c) => c.startsWith("signed_off:")) && chips.some((c) => c.startsWith("no_owner:") && /no owner/.test(c)), JSON.stringify(chips));
      ck(t("Remind is not offered"), (await page.getByRole("button", { name: "Remind" }).count()) === 0);
      await page.getByRole("button", { name: "Hand over anyway" }).first().click();
      const confirm = page.getByTestId("handover-confirm");
      await page.getByTestId("handover-dialog").waitFor();
      ck(t("Hand over is off with no reason"), await confirm.isDisabled());
      await page.getByTestId("handover-reason").fill("   ");
      ck(t("Hand over is off with only spaces"), await confirm.isDisabled());
      await page.getByTestId("handover-reason").fill("Kim is out until Monday, so the ticket should not wait.");
      ck(t("Hand over is on with a reason"), await confirm.isEnabled());
      await shot(page, "waiting-on-dialog", width);
      await page.getByRole("button", { name: "Cancel" }).click();
      ck(t("Cancel closes the box"), (await page.getByTestId("handover-dialog").count()) === 0);
      await page.getByRole("button", { name: "Hand over anyway" }).first().click();
      await page.getByTestId("handover-dialog").waitFor();
      await page.getByTestId("handover-reason").fill("Kim is out until Monday, so the ticket should not wait.");
      await confirm.click();
      const sampleNote = page.getByText("This is a sample, so nothing was sent.");
      await sampleNote.first().waitFor({ timeout: 3000 }).catch(() => {});
      ck(t("F7: confirming a hand-over in a sample says nothing was sent, and the box closes"), (await sampleNote.count()) >= 1 && (await page.getByTestId("handover-dialog").count()) === 0, String(await sampleNote.count()));
      await ctx.close();
    }
    // ── Projects, from the sample states: Mine and Team, the lock, the lead line and the hidden line ──
    for (const width of [390, 1440]) {
      const t = (s) => `@${width}: ${s}`;
      const ctx = await browser.newContext({ viewport: { width, height: width === 390 ? 844 : 900 } });
      await ctx.addInitScript((tok) => { try { localStorage.setItem("taresSamples", "1"); localStorage.setItem("tares_token", tok); } catch { /* blocked */ } }, ids.keys.alex);
      const page = await ctx.newPage();
      const calls = [];
      page.on("request", (req) => { if (new URL(req.url()).pathname.startsWith("/api/")) calls.push(req.url()); });
      const go = async (sample, view) => {
        await page.goto(`${BASE}/?sample=${sample}`, { waitUntil: "load" });
        await page.getByTestId("projects").waitFor();
        await settle(page);
        if (view === "team") await switchTo(page, "team");
      };
      const prow = (name) => page.getByTestId("project-row").filter({ hasText: name });
      const rowsN = () => page.getByTestId("project-row").count();

      await go("threePeople", "mine");
      ck(t("the dropdown shows both counts"), JSON.stringify(await switchOptions(page)) === JSON.stringify(["Yours (3)", "All (6)"]));
      ck(t("Yours is the one chosen and lists three projects"), (await switchLabel(page)) === "Yours (3)" && (await rowsN()) === 3, String(await rowsN()));
      const before = calls.length;
      await switchTo(page, "team");
      ck(t("Team lists all six"), (await rowsN()) === 6, String(await rowsN()));
      ck(t("All is now the one chosen"), (await switchLabel(page)) === "All (6)", await switchLabel(page));
      ck(t("a sample switches without calling the cell"), calls.length === before, calls.slice(before).join(", "));
      ck(t("a private project has a lock and the others none"), (await page.getByTestId("project-lock").count()) === 1 && (await prow("project-y").getByTestId("project-lock").count()) === 1);
      ck(t("a row says who leads it"), (await prow("project-x").getByTestId("project-lead").innerText()).trim() === "Led by Kim" && /^Led by (you|Alex)$/.test((await prow("project-y").getByTestId("project-lead").innerText()).trim()));
      ck(t("a row shows the members besides the lead"), (await prow("project-x").getByTestId("project-member").evaluateAll((els) => els.map((e) => e.getAttribute("aria-label")))).join() === "Alex");
      ck(t("a row says how many tickets are yours"), (await prow("project-x").innerText()).includes("3 tickets yours, 4 Kim's"));
      ck(t("no line about hidden projects when none are hidden"), (await page.getByTestId("hidden-summary").count()) === 0);
      await shot(page, "projects-team", width);
      await go("onePerson", "mine");
      ck(t("a cell of one has no switch, lock or lead line"), (await page.getByTestId("projects-switch").count()) === 0 && (await page.getByTestId("project-lock").count()) === 0 && (await page.getByTestId("project-lead").count()) === 0);

      for (const view of ["mine", "team"]) {
        await go("privateNotOn", view);
        const label = t(`privateNotOn, ${view}`);
        ck(`${label}: the hidden project is not a project row`, (await rowsN()) === (view === "mine" ? 3 : 6), String(await rowsN()));
        if (view === "team") {
          ck(`${label}: it stands as one quiet row, lock, lead and nothing else`, (await page.getByTestId("hidden-row").count()) === 1
             && (await page.getByTestId("hidden-row").innerText()).trim() === "Private, led by Lee", await page.getByTestId("hidden-row").allInnerTexts().then(String));
          ck(`${label}: the row comes after every project row`, await page.getByTestId("projects").evaluate((el) => {
            const rows = [...el.querySelectorAll('[data-testid="project-row"]')], stub = el.querySelector('[data-testid="hidden-row"]');
            return !!stub && rows.every((r) => r.compareDocumentPosition(stub) & Node.DOCUMENT_POSITION_FOLLOWING);
          }));
          ck(`${label}: the row is not counted, six projects need no Show all`, (await page.getByRole("button", { name: /Show all/ }).count()) === 0);
          ck(`${label}: no summary line besides the row`, (await page.getByTestId("hidden-summary").count()) === 0);
        } else {
          ck(`${label}: one line says how many are hidden and who leads them`, (await page.getByTestId("hidden-summary").count()) === 1
             && (await page.getByTestId("hidden-summary").innerText()).trim() === "1 private project you are not on, led by Lee");
          ck(`${label}: no stub row`, (await page.getByTestId("hidden-row").count()) === 0);
        }
        ck(`${label}: the switch counts leave the hidden one out`, (await switchOptions(page))[1] === "All (6)");
        ck(`${label}: no horizontal scroll`, (await overflow(page)) <= 0, `${await overflow(page)}px over`);
        await shot(page, `projects-private-${view}`, width);
        await checkWords(page, ck, label);
      }
      // F9: a Team list of nothing but private projects is quiet rows, not an empty list
      await go("privateOnly", "team");
      ck(t("privateOnly, team: only the quiet row, and no project row"), (await page.getByTestId("hidden-row").count()) === 1 && (await rowsN()) === 0, `${await page.getByTestId("hidden-row").count()} / ${await rowsN()}`);
      ck(t("privateOnly, team: it does not say No projects yet."), (await page.getByText("No projects yet.").count()) === 0 && (await page.getByText("Start one and hand it to the crew.").count()) === 0);
      await shot(page, "projects-private-only-team", width);
      await checkWords(page, ck, t("privateOnly, team"));
      await go("privateOnly", "mine");
      ck(t("privateOnly, mine: No projects of yours yet., and the one line about the hidden project"), (await page.getByText("No projects of yours yet.").count()) === 1
         && (await page.getByTestId("hidden-summary").count()) === 1 && (await page.getByTestId("hidden-row").count()) === 0);
      await checkWords(page, ck, t("privateOnly, mine"));

      for (const view of ["mine", "team"]) {
        await go("longText", view);
        ck(t(`longText, ${view}: a long name and a long lead wrap and never push the page wider`), (await overflow(page)) <= 0 && (await rowsN()) === (view === "mine" ? 1 : 2), `${await overflow(page)}px over`);
        await shot(page, `projects-long-${view}`, width);
        await checkWords(page, ck, t(`longText, ${view}`));
        await go("manyItems", view);
        const n = view === "mine" ? 4 : 12;
        ck(t(`manyItems, ${view}: ${Math.min(n, 6)} rows${n > 6 ? " and Show all 12 projects" : ""}`), (await rowsN()) === Math.min(n, 6)
           && (await page.getByRole("button", { name: /Show all 12 projects/ }).count()) === (n > 6 ? 1 : 0), String(await rowsN()));
        if (n > 6) {
          await page.getByRole("button", { name: /Show all 12 projects/ }).click();
          ck(t("Show all lists every project"), (await rowsN()) === 12, String(await rowsN()));
        }
        ck(t(`manyItems, ${view}: no horizontal scroll`), (await overflow(page)) <= 0);
        await shot(page, `projects-many-${view}`, width);
        await checkWords(page, ck, t(`manyItems, ${view}`));
      }
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
         && /example-org\/service-a/.test((await texts("repo-you-own"))[0]) && /1 sign-off asked/.test((await texts("repo-you-own"))[0])
         && /nothing waiting/.test((await texts("repo-you-own"))[1]), JSON.stringify(await texts("repo-you-own")));
      ck(t("All owners links to Settings, Advanced"), (await page.getByRole("link", { name: "All owners" }).getAttribute("href")) === "/settings/advanced?tab=areas");
      ck(t("the Claude plan and the connected tools stay"), (await page.locator('section[aria-label="Your Claude plan"]').count()) === 1
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
    // a cell of one person on its own daemon has none of the side column's team parts
    {
      const { ctx, page } = await open("solo", 1440);
      await page.goto(`http://127.0.0.1:${SOLO}/`, { waitUntil: "load" });
      await page.getByTestId("factory-home").waitFor({ timeout: 15000 }).catch(() => {});
      await settle(page);
      ck("a live cell of one has no team crews, quiet line, repos you own or limit foot",
         (await page.getByTestId("team-crews").count()) + (await page.getByTestId("quiet-crews").count()) + (await page.getByTestId("repos-you-own").count()) + (await page.getByTestId("crew-limit").count()) === 0);
      const NEW = ["waiting-on", "waiting-on-row", "waiting-on-chip", "projects-switch", "projects-mine", "projects-team", "hidden-row", "hidden-summary", "project-lock", "project-lead", "handover-dialog"];
      const seen = [];
      for (const id of NEW) if ((await page.getByTestId(id).count()) > 0) seen.push(id);
      ck("a live cell of one shows none of the team home's testids", seen.length === 0, seen.join());
      await ctx.close();
    }
    const soloHome = await fetch(`http://127.0.0.1:${SOLO}/api/home`).then((r) => r.json());
    ck("a cell of one person has no team keys in /api/home", !("you_are_waiting_on" in soloHome) && !("crews_quiet" in soloHome));
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
