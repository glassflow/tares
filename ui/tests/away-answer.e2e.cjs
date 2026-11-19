/*
 * M4 acceptance (TR-1462): a person away from the console answers from a Slack message.
 *
 * One scratch factory cell (TARES_EDITION=factory) on TARES_E2E_PORT (default 8871), MCP port +1, a fake
 * Slack on port +2 (TARES_SLACK_API_BASE), and the cell's public address set (TARES_PUBLIC_URL), so the
 * message carries a link. Sam is seeded with an email and a key (seed-away-answer.py), on a cell whose
 * reader has already read Sam's list once. Then, as on a real cell:
 *   1. the orchestrator asks Sam a desk question (POST /api/desk with the key the crew runs on, Sam's);
 *   2. within one read of the reader (a minute), Sam gets one Slack message: the ask, its project, and a
 *      button to /feed?item=<the item Sam's own Home lists>;
 *   3. Sam opens that link, as given in the message, in a browser with Sam's key: the card is first and
 *      marked, and Sam taps its Yes;
 *   4. the record says it: one answer to that item, by Sam, from the feed (GET /api/answers?item=);
 *   5. the orchestrator's wake sees it: /api/desk/answered goes from 0 to 1 since its baseline (the call the
 *      crew watch makes every few seconds, T32; tests/test_crew_wake_console.py covers the watch's side);
 *   6. within the next read, the Slack message is edited to "Answered by you from the feed." with no
 *      button, and nothing is sent a second time.
 * Takes about two and a half minutes: the reader reads once a minute and the run does not shorten it.
 * Run:  npm run test:away-answer     (builds first: the daemon serves ui/dist)
 * Set SHOTS_DIR to keep screenshots. TARES_E2E_CHROME points Playwright at another Chrome binary.
 */
const http = require("http");
const { spawn, execFileSync } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");

const UI_DIR = path.resolve(__dirname, "..");
const REPO = path.resolve(UI_DIR, "..");
const { chromium } = require("./browser.cjs");
const { checkWords } = require("./words.cjs");

const PORT = Number(process.env.TARES_E2E_PORT || 8871);
const BASE = `http://127.0.0.1:${PORT}`;
const SLACK_PORT = PORT + 2;
const SHOTS = process.env.SHOTS_DIR;
const TOKEN = "e2e-root-away-answer";
const QUESTION = "Which store should sessions use?";
const READ_MS = 80000;   // one read of the reader (60 s) and some room

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

async function api(token, method, p, body) {
  const r = await fetch(BASE + p, { method, headers: { "content-type": "application/json", authorization: `Bearer ${token}` },
                                    body: body ? JSON.stringify(body) : undefined });
  const text = await r.text();
  if (!r.ok) throw new Error(`${method} ${p} -> ${r.status} ${text.slice(0, 300)}`);
  return text ? JSON.parse(text) : null;
}

async function until(fn, ms) {
  const end = Date.now() + ms;
  for (;;) {
    const got = await fn();
    if (got) return got;
    if (Date.now() > end) return null;
    await new Promise((r) => setTimeout(r, 500));
  }
}

/** A fake Slack: knows Sam by email, makes each message in Sam's DM, records every call. */
function fakeSlack() {
  const calls = [];
  let n = 0;
  const server = http.createServer((req, res) => {
    let raw = "";
    req.on("data", (c) => { raw += c; });
    req.on("end", () => {
      const url = new URL(req.url, "http://slack");
      const method = url.pathname.replace(/^\/+/, "");
      let body = {};
      try { body = raw ? JSON.parse(raw) : {}; } catch { body = { raw }; }
      calls.push({ method, body, query: Object.fromEntries(url.searchParams), auth: req.headers.authorization });
      let out = { ok: true };
      if (method === "users.lookupByEmail") {
        out = url.searchParams.get("email") === "sam@example.com" ? { ok: true, user: { id: "U_SAM" } } : { ok: false, error: "users_not_found" };
      } else if (method === "chat.postMessage") {
        n += 1;
        out = { ok: true, channel: body.channel === "U_SAM" ? "D_SAM" : body.channel, ts: `1795000000.00010${n}` };
      }
      res.writeHead(200, { "content-type": "application/json" });
      res.end(JSON.stringify(out));
    });
  });
  return { server, calls, of: (m) => calls.filter((c) => c.method === m) };
}

/** Every link a message's buttons carry. */
const buttonUrls = (msg) => (msg.blocks || []).flatMap((b) => (b.elements || []).map((e) => e.url).filter(Boolean));
/** All of a message's words, blocks and fallback text. */
const words = (msg) => [msg.text || "", ...(msg.blocks || []).flatMap((b) => [b.text?.text || "", ...(b.elements || []).map((e) => e.text?.text || e.text || "")])].join("\n");

(async () => {
  const bin = path.join(REPO, ".venv/bin");
  const tares = fs.existsSync(path.join(bin, "tares")) ? path.join(bin, "tares") : "tares";
  const python = fs.existsSync(path.join(bin, "python")) ? path.join(bin, "python") : "python3";
  const dist = process.env.TARES_UI_DIST || path.join(UI_DIR, "dist");
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "tares-away-answer-e2e-"));
  const ids = JSON.parse(execFileSync(python, [path.join(__dirname, "seed-away-answer.py"), home],
    { cwd: REPO, encoding: "utf8", env: cleanEnv({ PYTHONPATH: REPO }) }).trim().split("\n").pop());
  ck("seeded Sam, with an email and a key", !!ids.sam && !!ids.sam_key);

  const slack = fakeSlack();
  await new Promise((r) => slack.server.listen(SLACK_PORT, "127.0.0.1", r));
  const env = cleanEnv({ TARES_EDITION: "factory", TARES_HOME: home, TARES_PORT: String(PORT), TARES_MCP_PORT: String(PORT + 1),
                         TARES_UI_DIST: dist, TARES_OTLP_GRPC_PORT: "off", TARES_AUTH_TOKEN: TOKEN, PYTHONPATH: REPO,
                         TARES_SLACK_BOT_TOKEN: "xoxb-e2e-away", TARES_SLACK_API_BASE: `http://127.0.0.1:${SLACK_PORT}`,
                         TARES_PUBLIC_URL: BASE });
  const daemon = spawn(tares, ["up", "--port", String(PORT), "--data-dir", home], { cwd: REPO, stdio: "ignore", env, detached: true });
  console.log(`  daemon: pid ${daemon.pid}, port ${PORT}; fake Slack on ${SLACK_PORT}`);
  let browser;
  try {
    ck("daemon up", await waitFor(`${BASE}/health`));
    const shop = (await api(TOKEN, "POST", "/api/projects", { template: "custom", name: "Shop", goal: "Sessions survive a restart",
                                                               objects: [], kind: "software_factory" })).id;
    const me = await api(ids.sam_key, "GET", "/api/me/slack");
    ck("Sam's messages are on and Slack is connected", me.on === true && me.connected === true, JSON.stringify(me));

    // 1. the orchestrator asks Sam
    const before = await api(TOKEN, "GET", "/api/desk/answered");
    ck("the orchestrator's baseline: nothing answered yet", before.count === 0, JSON.stringify(before));
    const asked = Date.now();
    // on a cell of one the crew writes with its person's key, so the question is Sam's (addressing someone
    // else is the enterprise team cell's; test_factory_team_home covers whose list it lands on there)
    const d = await api(ids.sam_key, "POST", "/api/desk", { question: QUESTION, options: ["Redis", "Postgres"], recommendation: "Postgres",
                                                            why: "We already back it up", project: shop });
    ck("the question is on the desk, for Sam", !!d.label, JSON.stringify(d));
    const homeItem = (await api(ids.sam_key, "GET", "/api/home")).waiting_for_you.find((i) => i.ask === QUESTION);
    ck("Sam's own Home lists it", !!homeItem, "not on Sam's Home");

    // 2. one Slack message, within one read
    const sent = await until(() => slack.of("chat.postMessage")[0], READ_MS);
    ck("Sam gets one Slack message within a read", !!sent && slack.of("chat.postMessage").length === 1,
       `${slack.of("chat.postMessage").length} sent`);
    if (!sent) throw new Error("no message: the rest cannot run");
    console.log(`  the message came ${Math.round((Date.now() - asked) / 1000)} s after the question`);
    const msg = sent.body;
    ck("it goes to Sam, found by email, with the cell's bot token", msg.channel === "U_SAM" && sent.auth === "Bearer xoxb-e2e-away"
       && slack.of("users.lookupByEmail").some((c) => c.query.email === "sam@example.com"), JSON.stringify(msg.channel));
    ck("it says the ask and the project", words(msg).includes(QUESTION) && words(msg).includes("Shop"), words(msg));
    const links = buttonUrls(msg);
    ck("its button opens the feed at that item", links.length === 1 && links[0] === `${BASE}/feed?item=${encodeURIComponent(homeItem?.id || "")}`,
       JSON.stringify(links));
    ck("no em dash in the message", !words(msg).includes("—"));

    // 3. Sam opens the link from the message and answers in the feed
    browser = await chromium.launch(process.env.TARES_E2E_CHROME ? { executablePath: process.env.TARES_E2E_CHROME } : {});
    const ctx = await browser.newContext({ viewport: { width: 390, height: 844 } });
    await ctx.addInitScript((t) => { try { localStorage.setItem("tares_token", t); } catch { /* blocked */ } }, ids.sam_key);
    const page = await ctx.newPage();
    await page.goto(links[0], { waitUntil: "load" });
    await page.getByTestId("factory-feed").waitFor();
    await page.waitForLoadState("networkidle");
    const first = page.getByTestId("feed-card").first();
    ck("the link lands on the card: first and marked", (await first.getAttribute("data-focus")) === "1"
       && (await first.getByTestId("feed-ask").innerText()) === QUESTION, await first.innerText().catch(() => "no card"));
    ck("its Yes is the suggested answer", (await first.getByTestId("feed-yes").innerText()).includes("Postgres"),
       await first.getByTestId("feed-yes").innerText());
    await checkWords(page, ck, "feed from the link");
    if (SHOTS) await page.screenshot({ path: path.join(SHOTS, "away-link.png"), fullPage: true });
    await first.getByTestId("feed-yes").click();
    await page.waitForFunction((q) => ![...document.querySelectorAll('[data-testid="feed-ask"]')].some((e) => e.innerText === q),
                               QUESTION, { timeout: 10000 }).catch(() => {});
    ck("the card leaves the feed", (await page.getByTestId("feed-ask").getByText(QUESTION, { exact: true }).count()) === 0);
    if (SHOTS) await page.screenshot({ path: path.join(SHOTS, "away-answered.png"), fullPage: true });

    // 4. the record: from the feed
    const rec = (await api(ids.sam_key, "GET", `/api/answers?item=${encodeURIComponent(homeItem.id)}`)).answers;
    ck("one answer to that item, by Sam, from the feed", rec.length === 1 && rec[0].via === "feed" && rec[0].by === "Sam" && rec[0].mine === true
       && /Postgres/.test(JSON.stringify(rec[0])), JSON.stringify(rec));

    // 5. the orchestrator's wake
    const after = await api(TOKEN, "GET", `/api/desk/answered?since=${encodeURIComponent("")}`);
    ck("the orchestrator's wake sees one new answer", after.count === 1 && after.latest > "", JSON.stringify(after));
    const row = (await api(TOKEN, "GET", "/api/desk?state=answered")).desk.find((x) => x.label === d.label);
    ck("the desk holds Sam's answer for the orchestrator to read", !!row && /Postgres/.test(JSON.stringify(row)), JSON.stringify(row));

    // 6. the Slack message says it was answered
    const edit = await until(() => slack.of("chat.update")[0], READ_MS);
    ck("the message is edited within a read", !!edit, "no chat.update");
    if (edit) {
      ck("the same message: its DM and its ts", edit.body.channel === "D_SAM" && edit.body.ts === "1795000000.000101", JSON.stringify(edit.body));
      ck("it says who answered and where", words(edit.body).includes("Answered by you from the feed."), words(edit.body));
      ck("the edited message has no button", buttonUrls(edit.body).length === 0, JSON.stringify(edit.body.blocks));
      ck("still the ask, so the DM reads on its own", words(edit.body).includes(QUESTION), words(edit.body));
    }
    ck("nothing was sent twice", slack.of("chat.postMessage").length === 1 && slack.of("chat.update").length === 1,
       `${slack.of("chat.postMessage").length} sent, ${slack.of("chat.update").length} edits`);
    await ctx.close();
  } finally {
    if (browser) await browser.close();
    try { process.kill(-daemon.pid); } catch { try { daemon.kill(); } catch { /* gone */ } }
    slack.server.close();
    fs.rmSync(home, { recursive: true, force: true });
  }
  console.log(`\n${pass} passed, ${fail} failed`);
  process.exit(fail ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(1); });
