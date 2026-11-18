/*
 * Browser e2e for the feed (P-TR-273): /feed drawn from its samples, then answered live.
 *
 * Starts one scratch daemon with an empty factory cell (TARES_EDITION=factory) on TARES_E2E_PORT
 * (default 8835), MCP port +1, in a clean environment and a fresh data directory, and stops only it.
 * Samples, with the browser flag taresSamples=1, at 1440 and 390 wide: the busy one's cards and
 * headline, one filled button on the top card (the PR's Merge, TR-1450), its checks one tap deeper
 * (TR-1449), Accept on the milestone, the crew's reason with its suggestion, a Yes on a sample that
 * sends nothing yet moves the card to Answered today; the empty and crew-down samples; no sideways
 * scroll; the words check.
 * Live: the script seeds the daemon through its API (a question with options, a project ready to
 * start, a milestone with a check for the person, ahead of one still open), opens /feed without the flag and answers each card
 * with one tap: Yes on the suggested option, No (Later) on the hand-over, Yes (Accept) on the
 * milestone. Each card leaves, and after a reload Answered today is the cell's own record
 * (GET /api/answers), every answer from the feed (TR-1451). Merging a PR from a card is covered by
 * tests/test_pr_one_tap.py, since the merge queue needs GitHub.
 * Run:  npm run test:factory-feed     (builds first: the daemon serves ui/dist)
 * Set SHOTS_DIR to keep screenshots. TARES_E2E_CHROME points Playwright at another Chrome binary.
 */
const http = require("http");
const { spawn } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");

const UI_DIR = path.resolve(__dirname, "..");
const REPO = path.resolve(UI_DIR, "..");
const { chromium } = require("./browser.cjs");
const { checkWords } = require("./words.cjs");

const PORT = Number(process.env.TARES_E2E_PORT || 8835);
const BASE = `http://127.0.0.1:${PORT}`;
const SHOTS = process.env.SHOTS_DIR;
const SAMPLE_NOTE = "This is a sample, so nothing was sent.";

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

async function api(method, p, body) {
  const r = await fetch(BASE + p, { method, headers: { "content-type": "application/json" }, body: body ? JSON.stringify(body) : undefined });
  const text = await r.text();
  if (!r.ok) throw new Error(`${method} ${p} -> ${r.status} ${text.slice(0, 300)}`);
  return text ? JSON.parse(text) : null;
}

/** The filled buttons and button links in the page, by their text (read by colour, as Home's e2e does). */
async function filledButtons(page) {
  return page.evaluate(() => {
    const probe = document.createElement("i"); probe.className = "bg-primary"; document.body.appendChild(probe);
    const fill = getComputedStyle(probe).backgroundColor; probe.remove();
    return [...document.querySelectorAll("main a, main button")]
      .filter((e) => e.getBoundingClientRect().width > 0 && getComputedStyle(e).backgroundColor === fill)
      .map((e) => e.innerText.trim());
  });
}

(async () => {
  const bin = path.join(REPO, ".venv/bin");
  const tares = fs.existsSync(path.join(bin, "tares")) ? path.join(bin, "tares") : "tares";
  const dist = process.env.TARES_UI_DIST || path.join(UI_DIR, "dist");
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "tares-factory-feed-e2e-"));
  const env = cleanEnv({ TARES_EDITION: "factory", TARES_HOME: home, TARES_PORT: String(PORT), TARES_MCP_PORT: String(PORT + 1),
                         TARES_UI_DIST: dist, PYTHONPATH: REPO });
  const daemon = spawn(tares, ["up", "--port", String(PORT), "--data-dir", home], { cwd: REPO, stdio: "ignore", env, detached: true });
  console.log(`  daemon: pid ${daemon.pid}, port ${PORT}`);
  let browser;
  try {
    ck("daemon up", await waitFor(`${BASE}/health`));
    browser = await chromium.launch(process.env.TARES_E2E_CHROME ? { executablePath: process.env.TARES_E2E_CHROME } : {});
    const context = async (width, flag) => {
      const ctx = await browser.newContext({ viewport: { width, height: width === 390 ? 844 : 900 } });
      if (flag) await ctx.addInitScript(() => { try { window.localStorage.setItem("taresSamples", "1"); } catch { /* blocked */ } });
      return { ctx, page: await ctx.newPage() };
    };
    const open = async (page, sample) => {
      await page.goto(`${BASE}/feed${sample ? `?sample=${sample}` : ""}`, { waitUntil: "load" });
      await page.getByTestId("factory-feed").waitFor();
      await page.waitForLoadState("networkidle");
    };
    const shot = async (page, name) => {
      if (SHOTS) await page.screenshot({ path: path.join(SHOTS, `factory-feed-${name}.png`), fullPage: true });
    };
    const overflow = (page) => page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    const cards = (page) => page.getByTestId("feed-card");
    const card = (page, ask) => cards(page).filter({ has: page.getByTestId("feed-ask").getByText(ask, { exact: true }) });

    // ── samples ──────────────────────────────────────────────────────────────────────────────────
    for (const width of [1440, 390]) {
      const t = (s) => `busy @${width}: ${s}`;
      const { ctx, page } = await context(width, true);
      await open(page, "busy");
      ck(t("six cards"), (await cards(page).count()) === 6, String(await cards(page).count()));
      ck(t("the headline counts them, what holds the crew up, and the longest wait"),
         (await page.getByTestId("feed-headline").innerText()) === "6 things wait for you. 3 hold the crew up. The oldest has waited 3 days.",
         await page.getByTestId("feed-headline").innerText());
      const lead = await filledButtons(page);
      ck(t("one filled button: the top card's Merge (TR-1450)"), lead.length === 1 && lead[0] === "Merge", JSON.stringify(lead));
      const top = cards(page).first();
      ck(t("the top card is the PR, asking to merge it"), (await top.getAttribute("data-kind")) === "prs_to_review"
         && (await top.getByTestId("feed-ask").innerText()) === "Merge PR 336");
      ck(t("its checks wait one tap deeper"), (await top.getByTestId("feed-checks").count()) === 0);
      await top.getByTestId("feed-more").click();
      ck(t("More shows the checks in one sentence (TR-1449)"),
         (await top.getByTestId("feed-checks").innerText()) === "Checks pass. main has 2 commits this branch lacks.");
      ck(t("the milestone's Yes is Accept"),
         (await cards(page).and(page.locator("[data-kind=milestone_to_check]")).getByTestId("feed-yes").innerText()) === "Accept");
      const rec = await cards(page).and(page.locator("[data-kind=question]")).getByTestId("feed-recommendation").innerText();
      ck(t("the suggestion carries the crew's reason, from the item itself"),
         rec.includes("Postgres") && rec.includes("Sessions are rows we already back up"), rec);
      await checkWords(page, ck, `feed busy @${width}`);
      ck(t("no sideways scroll"), (await overflow(page)) <= 0, `${await overflow(page)}px`);
      await shot(page, `busy-${width}`);

      const answered = page.getByTestId("feed-answered");
      const before = await answered.count();
      await top.getByTestId("feed-yes").click();
      await page.getByText(SAMPLE_NOTE).first().waitFor();
      ck(t("Merge on a sample says nothing was sent"), true);
      await page.waitForFunction(() => document.querySelectorAll('[data-testid="feed-card"]').length === 5);
      ck(t("and the card moves to Answered today, as yes, from the feed"), (await answered.count()) === before + 1
         && (await answered.first().getAttribute("data-choice")) === "yes"
         && (await answered.first().innerText()).includes("Merge") && (await answered.first().innerText()).includes("from the feed"),
         await answered.first().innerText());
      await ctx.close();
    }
    {
      const { ctx, page } = await context(390, true);
      await open(page, "empty");
      ck("empty: nothing to answer, and why", (await page.getByTestId("feed-empty").count()) === 1 && (await cards(page).count()) === 0
         && (await page.getByTestId("feed-headline").innerText()) === "Nothing waits for you.");
      await checkWords(page, ck, "feed empty");
      await open(page, "down");
      ck("down: says the crew is not running", (await page.getByTestId("feed-crew-down").count()) === 1);
      await checkWords(page, ck, "feed down");
      await ctx.close();
    }

    // ── live ─────────────────────────────────────────────────────────────────────────────────────
    const project = async (name, goal) => (await api("POST", "/api/projects", { template: "custom", name, goal, objects: [], kind: "software_factory" })).id;
    const atlas = await project("Atlas", "Sessions survive a restart");
    await api("POST", "/api/desk", { question: "Which store should sessions use?", options: ["Redis", "Postgres"],
                                     recommendation: "Postgres", why: "We already back it up", project: atlas });
    const billing = await project("Billing", "Customers download their invoices");
    await api("POST", `/api/projects/${billing}/docs`, { kind: "spec", title: "Billing spec", body: "# Billing\n\nA download button." });
    await api("POST", `/api/projects/${billing}/tickets`, { title: "Add the download button" });
    const cart = await project("Cart", "A reader keeps books in a cart");
    await api("POST", `/api/projects/${cart}/milestones`, {
      name: "M1: cart", checks: [{ check: "Add a book", expect: "The cart counts it", for: "person" }] });
    const t = await api("POST", `/api/projects/${cart}/tickets`, { title: "Cart model", milestone: "M1: cart" });
    await api("POST", `/api/projects/${cart}/tickets/${t.id}/stage`, { stage: "merged", reason: "seeded" });
    // a second milestone still open, so M1 is a milestone to check and not the end of the project
    await api("POST", `/api/projects/${cart}/milestones`, { name: "M2: checkout" });
    await api("POST", `/api/projects/${cart}/tickets`, { title: "Pay for the cart", milestone: "M2: checkout" });

    for (const width of [390]) {
      const { ctx, page } = await context(width, false);
      await open(page, "");
      const L = (s) => `live @${width}: ${s}`;
      ck(L("the live feed, not a sample"), (await page.getByTestId("factory-feed").getAttribute("data-live")) === "1");
      const asks = await page.getByTestId("feed-ask").allInnerTexts();
      const want = ["Which store should sessions use?", "Hand Billing to the crew", "Check M1 of Cart"];
      ck(L("a card for each thing seeded"), want.every((a) => asks.includes(a)), JSON.stringify(asks));
      await checkWords(page, ck, `feed live @${width}`);
      await shot(page, `live-${width}`);

      const answer = async (ask, button, label) => {
        const c = card(page, ask);
        const b = c.getByTestId(button);
        ck(L(`${ask}: the button reads ${label}`), (await b.innerText()) === label, await b.innerText());
        const n = await cards(page).count();
        await b.click();
        await page.waitForFunction((k) => document.querySelectorAll('[data-testid="feed-card"]').length === k, n - 1, { timeout: 10000 })
          .then(() => ck(L(`${ask}: the card leaves`), true), () => ck(L(`${ask}: the card leaves`), false));
      };
      await answer("Which store should sessions use?", "feed-yes", "Yes, Postgres");
      await answer("Hand Billing to the crew", "feed-no", "Later");
      await answer("Check M1 of Cart", "feed-yes", "Accept");

      const record = (await api("GET", "/api/answers")).answers;
      ck(L("the cell recorded three answers, every one from the feed"), record.length === 3 && record.every((a) => a.via === "feed"),
         JSON.stringify(record.map((a) => [a.id, a.via])));
      const by = Object.fromEntries(record.map((a) => [a.ask, a]));
      ck(L("the option picked, Later as no, Accept as yes"), by["Which store should sessions use?"]?.said === "Postgres"
         && by["Hand Billing to the crew"]?.choice === "no" && by["Check M1 of Cart"]?.choice === "yes",
         JSON.stringify(record));

      await open(page, "");
      const rows = page.getByTestId("feed-answered");
      ck(L("after a reload nothing is left to answer"), (await cards(page).count()) === 0);
      ck(L("Answered today is the cell's record: three, each from the feed"), (await rows.count()) === 3
         && (await rows.allInnerTexts()).every((x) => x.includes("from the feed")), JSON.stringify(await rows.allInnerTexts()));
      ck(L("Later reads as No, Accept as Yes"),
         (await rows.filter({ hasText: "Hand Billing to the crew" }).getAttribute("data-choice")) === "no"
         && (await rows.filter({ hasText: "Check M1 of Cart" }).getAttribute("data-choice")) === "yes");
      await checkWords(page, ck, `feed live answered @${width}`);
      await shot(page, `live-answered-${width}`);
      await ctx.close();
    }

    // without the flag the address alone shows no sample
    {
      const { ctx, page } = await context(1440, false);
      await open(page, "busy");
      ck("no flag: ?sample=busy shows the cell, not the sample", (await page.getByText("Merge PR 336").count()) === 0
         && (await page.getByTestId("factory-feed").getAttribute("data-live")) === "1");
      await ctx.close();
    }
  } finally {
    if (browser) await browser.close();
    try { process.kill(-daemon.pid); } catch { try { daemon.kill(); } catch { /* gone */ } }
    fs.rmSync(home, { recursive: true, force: true });
  }
  console.log(`\n${pass} passed, ${fail} failed`);
  process.exit(fail ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(1); });
