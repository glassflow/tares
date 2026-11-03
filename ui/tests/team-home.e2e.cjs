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
      const ctl = { fail: false, hold: null, reads: 0, posts: [] };
      await page.route("**/api/home", async (route) => {
        ctl.reads++;
        if (ctl.hold) await ctl.hold;
        if (ctl.fail) return route.fulfill({ status: 500, contentType: "application/json", body: JSON.stringify({ detail: "down" }) });
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
        await page.goto(`${BASE}/`, { waitUntil: "load" });
        await page.getByTestId("factory-home-error").waitFor();
        await shot(page, "error", width);
        await checkWords(page, ck, t("error"));
        ck(t("a first read that fails offers Retry and shows no rows"), (await page.getByRole("button", { name: "Retry" }).count()) === 1
           && (await page.getByTestId("waiting-row").count()) === 0);
        await ctx.close();
      }
      {
        const { ctx, page } = await open("alex", width);
        await page.goto(`${BASE}/`, { waitUntil: "load" });
        await page.getByTestId("factory-home").waitFor();
        await settle(page);
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

    // ── acting, as Alex at 1440 ──
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
         && (await optPanel.getByRole("button", { name: "green", exact: true }).count()) === 1 && (await optPanel.getByTestId("answer-text").count()) === 0);
      ck(t("a crew question can stand for next time"), (await optPanel.getByTestId("answer-standing").count()) === 1);
      await optPanel.getByRole("button", { name: "green", exact: true }).click();
      await colour.waitFor({ state: "detached" });
      const decided = (await api("GET", `/api/projects/${ids.project}/decisions`)).body.decisions.find((d) => /Which colour for the links/.test(d.question));
      ck(t("the option clicked is the recorded answer, made in the console"), !!decided && decided.words === "green" && decided.via === "console", JSON.stringify(decided));
      const desk = (await api("GET", "/api/desk?state=answered")).body.desk;
      ck(t("the desk item is answered"), desk.some((d) => d.label === ids.desk.options), JSON.stringify(desk.map((d) => d.label)));
      ck(t("five remain"), (await rows.count()) === 5, String(await rows.count()));

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

    // ── a long question, a long list ──
    for (const width of [390, 1440]) {
      const t = (s) => `@${width}: ${s}`;
      const { ctx, page } = await open("alex", width);
      await page.goto(`${BASE}/`, { waitUntil: "load" });
      await page.getByTestId("factory-home").waitFor();
      await settle(page);
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
      await page.goto(`${BASE}/`, { waitUntil: "load" });
      await page.getByTestId("factory-home").waitFor();
      await settle(page);
      const rows = page.getByTestId("waiting-row");
      await page.waitForFunction(() => document.querySelectorAll('[data-testid="waiting-row"]').length >= 8, null, { timeout: 4000 }).catch(() => {});
      ck(t("a long list shows eight rows and Show all"), (await rows.count()) === 8 && (await page.getByRole("button", { name: /Show all/ }).count()) === 1,
         JSON.stringify({ rows: await rows.count(), head: await page.getByTestId("home-headline").innerText(), titles: (await rows.allInnerTexts()).map((x) => x.slice(0, 40)) }));
      await page.getByRole("button", { name: /Show all/ }).click();
      ck(t("Show all lists every item"), (await rows.count()) === 14, String(await rows.count()));
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
      await page.getByTestId("factory-home").waitFor();
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
        if (view === "team") await page.getByTestId("projects-team").click();
      };
      const prow = (name) => page.getByTestId("project-row").filter({ hasText: name });
      const rowsN = () => page.getByTestId("project-row").count();

      await go("threePeople", "mine");
      ck(t("the switch shows both counts"), (await page.getByTestId("projects-mine").innerText()) === "Mine, 3" && (await page.getByTestId("projects-team").innerText()) === "Team, 6");
      ck(t("Mine is the one chosen and lists three projects"), (await page.getByTestId("projects-mine").getAttribute("aria-selected")) === "true" && (await rowsN()) === 3, String(await rowsN()));
      const before = calls.length;
      await page.getByTestId("projects-team").click();
      ck(t("Team lists all six"), (await rowsN()) === 6, String(await rowsN()));
      ck(t("Team is now the one chosen"), (await page.getByTestId("projects-team").getAttribute("aria-selected")) === "true" && (await page.getByTestId("projects-mine").getAttribute("aria-selected")) === "false");
      ck(t("a sample switches without calling the cell"), calls.length === before, calls.slice(before).join(", "));
      ck(t("a private project has a lock and the others none"), (await page.getByTestId("project-lock").count()) === 1 && (await prow("project-y").getByTestId("project-lock").count()) === 1);
      ck(t("a row says who leads it"), (await prow("project-x").getByTestId("project-lead").innerText()).trim() === "Led by Kim" && /^Led by (you|Alex)$/.test((await prow("project-y").getByTestId("project-lead").innerText()).trim()));
      ck(t("a row shows the members besides the lead"), (await prow("project-x").getByTestId("project-member").evaluateAll((els) => els.map((e) => e.getAttribute("aria-label")))).join() === "Alex");
      ck(t("a row says how many tickets are yours"), (await prow("project-x").innerText()).includes("3 tickets in your repo"));
      ck(t("no line about hidden projects when none are hidden"), (await page.getByTestId("hidden-summary").count()) === 0);
      await shot(page, "projects-team", width);
      await go("onePerson", "mine");
      ck(t("a cell of one has no switch, lock or lead line"), (await page.getByTestId("projects-switch").count()) === 0 && (await page.getByTestId("project-lock").count()) === 0 && (await page.getByTestId("project-lead").count()) === 0);

      for (const view of ["mine", "team"]) {
        await go("privateNotOn", view);
        const label = t(`privateNotOn, ${view}`);
        ck(`${label}: the hidden project is not a row`, (await rowsN()) === (view === "mine" ? 3 : 6) && (await page.getByText("Private, led by").count()) === 0, String(await rowsN()));
        ck(`${label}: one line says how many are hidden and who leads them`, (await page.getByTestId("hidden-summary").count()) === 1 && (await page.getByTestId("hidden-summary").innerText()).trim() === "1 private project you are not on, led by Lee");
        ck(`${label}: the switch counts leave the hidden one out`, (await page.getByTestId("projects-team").innerText()) === "Team, 6");
        ck(`${label}: no horizontal scroll`, (await overflow(page)) <= 0, `${await overflow(page)}px over`);
        await shot(page, `projects-private-${view}`, width);
        await checkWords(page, ck, label);
      }
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
