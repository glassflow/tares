/*
 * Browser e2e for TR-752: the AI SRE Timeline page (/timeline).
 *
 * What happened on the cell, newest first and grouped by the hour, from one read
 * (GET /api/sre/timeline). The service, the range and the kinds live in the address, so a link
 * lands on the same list and a reload keeps it. A window or a folded quiet stretch has lines
 * behind it; they are read only when a person opens them (GET /api/sre/timeline/raw), 20 at
 * first and 200 more each time. The list refreshes every 30 seconds (replacing the first page, so a
 * quiet stretch whose start moved is not listed twice), except for a range that has an end, and
 * "Load earlier" asks for the page before the last one. A from and to in the address are a custom
 * range even without range=custom. Every state shows only words from words.json and none scrolls
 * sideways at 390 px.
 *
 * Seeds a scratch AI SRE cell (tests/seed-sre-timeline.py, invented names, times relative to now),
 * then runs the real daemon on it, serving the built console. All ten states of the mock are drawn
 * LIVE (normal, raw, service, kinds, empty, loading, error, rawerr, long, many) with one read
 * counted each, and again from the samples (?sample=<name>, with the sample flag in localStorage),
 * which need no data and make no call at all.
 * Run:  npm run build && node tests/sre-timeline.e2e.cjs     (from ui/, tares installed in ../.venv)
 * Set SHOTS_DIR to keep a screenshot of every state at 1440 and 390 px, light and dark.
 * TARES_E2E_PORT moves the daemon (default 8871). TARES_E2E_BIN is the bin directory that holds
 * tares and python when it is not ../.venv/bin. TARES_E2E_CHROME points Playwright at another
 * Chrome binary when its own download is missing.
 */
const http = require("http");
const { spawn, execFileSync } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");

const UI_DIR = path.resolve(__dirname, "..");
const REPO = path.resolve(UI_DIR, "..");
const { chromium } = require(path.join(UI_DIR, "node_modules", "playwright"));
const { checkWords, expectOneCall } = require("./words.cjs");

const PORT = Number(process.env.TARES_E2E_PORT || 8871);
const BASE = `http://127.0.0.1:${PORT}`;
const SHOTS = process.env.SHOTS_DIR;
const ROUTE = "/api/sre/timeline";
const RAW = "/api/sre/timeline/raw";
const TL_URL = /\/api\/sre\/timeline(\?.*)?$/;
const RAW_URL = /\/api\/sre\/timeline\/raw(\?.*)?$/;
const HOUR_RE = /^(Yesterday |\d{1,2} [A-Z][a-z]{2} )?\d\d:00$/;
const SAMPLES = ["normal", "raw", "service", "kinds", "empty", "load", "err", "rawerr", "long", "many"];

let pass = 0, fail = 0;
const ck = (label, cond, detail = "") => {
  cond ? pass++ : fail++;
  console.log((cond ? "  ok   " : "  FAIL ") + label + (cond ? "" : `   ${detail}`));
};
const eq = (label, got, want) => ck(label, JSON.stringify(got) === JSON.stringify(want), `got ${JSON.stringify(got)}, want ${JSON.stringify(want)}`);

async function waitFor(url, tries = 100) {
  for (; tries > 0; tries--) {
    const ok = await new Promise((res) => http.get(url, (r) => { r.resume(); res(true); }).on("error", () => res(false)));
    if (ok) return true;
    await new Promise((r) => setTimeout(r, 200));
  }
  return false;
}
const api = (p) => fetch(BASE + p).then((r) => r.json());
const fmt = (n) => n.toLocaleString("en-US");
const two = (n) => String(n).padStart(2, "0");
const stamp = (d) => d.toISOString().replace(/\.\d+Z$/, "Z");
const local = (d) => `${d.getUTCFullYear()}-${two(d.getUTCMonth() + 1)}-${two(d.getUTCDate())}T${two(d.getUTCHours())}:${two(d.getUTCMinutes())}`;

(async () => {
  const bin = process.env.TARES_E2E_BIN || path.join(REPO, ".venv/bin");
  const tares = fs.existsSync(path.join(bin, "tares")) ? path.join(bin, "tares") : "tares";
  const python = fs.existsSync(path.join(bin, "python")) ? path.join(bin, "python") : "python3";
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "tares-sre-timeline-e2e-"));
  const dist = process.env.TARES_UI_DIST || path.join(UI_DIR, "dist");
  const seedEnv = { ...process.env, PYTHONPATH: REPO };
  for (const k of ["TARES_AUTH_TOKEN", "TARES_DB", "TARES_CATALOG", "TARESD_URL", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"]) delete seedEnv[k];
  const seeded = JSON.parse(execFileSync(python, [path.join(__dirname, "seed-sre-timeline.py"), home],
    { cwd: REPO, encoding: "utf8", env: seedEnv }).trim().split("\n").pop());
  ck("seeded the cell", !!seeded.base && !!seeded.incident && seeded.services.includes("burst-03"));
  const B = new Date(seeded.base);

  const env = { ...process.env, TARES_EDITION: "sre", TARES_HOME: home, TARES_PORT: String(PORT), TARES_MCP_PORT: String(PORT + 1),
                TARES_UI_DIST: dist, TARES_OTLP_GRPC_PORT: "off", TARES_WATCH_SECONDS: "3600", PYTHONPATH: REPO };   // this runs the code under test
  for (const k of ["TARES_AUTH_TOKEN", "TARES_DB", "TARES_CATALOG", "TARESD_URL", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"]) delete env[k];
  const daemon = spawn(tares, ["up", "--port", String(PORT), "--data-dir", home], { cwd: REPO, stdio: "ignore", env });
  let browser;
  const pageErrors = [];
  try {
    ck("daemon up", await waitFor(`${BASE}/health`));
    browser = await chromium.launch(process.env.TARES_E2E_CHROME ? { executablePath: process.env.TARES_E2E_CHROME } : {});

    const newPage = async (width, sampleFlag = false) => {
      const ctx = await browser.newContext({ viewport: { width, height: width < 600 ? 844 : 900 } });
      if (sampleFlag) await ctx.addInitScript(() => { try { localStorage.setItem("taresSamples", "1"); } catch (e) { /* none */ } });
      const page = await ctx.newPage();
      page.on("pageerror", (e) => pageErrors.push(e.message));
      return { ctx, page };
    };
    const theme = async (page, t) => { await page.evaluate((x) => document.documentElement.setAttribute("data-theme", x), t); await page.waitForTimeout(t === "dark" ? 600 : 100); };
    // a screenshot of a state in both themes, named as the mock names them
    const shots = async (page, prefix, state, width) => {
      if (!SHOTS) return;
      for (const t of ["light", "dark"]) {
        await theme(page, t);
        await page.screenshot({ path: path.join(SHOTS, `${prefix}tl-${state}-${width}-${t}.png`), fullPage: true });
      }
      await theme(page, "light");
    };
    const overflow = (page) => page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    const settle = async (page) => { await page.waitForLoadState("networkidle"); await page.waitForTimeout(150); };
    // A fake clock for a page that fast-forwards. The page asks for "the last 6 hours" as a from of
    // its own clock minus 6 hours, while the cell answers by its own clock, which does not move. The
    // cell groups events into 10-minute stretches from the start of the range, so a fast-forward that
    // carries the page's clock over a 10-minute mark moves the first stretch and the list changes
    // between two reads of the same data. Start the page's clock in the middle of a 10-minute mark,
    // and no fast-forward in this file (95 seconds at most) reaches the next one.
    const installClock = (page) => page.clock.install({ time: Math.floor(Date.now() / 600e3) * 600e3 + 300e3 });

    const items = (page) => page.getByTestId("timeline-item");
    const ids = (page) => items(page).evaluateAll((e) => e.map((x) => x.dataset.id));
    const kindsOf = (page) => items(page).evaluateAll((e) => [...new Set(e.map((x) => x.dataset.kind))].sort());
    const tags = (page) => page.getByTestId("timeline-tag").allInnerTexts();
    const texts = (page) => page.getByTestId("timeline-text").evaluateAll((e) => e.map((x) => x.firstElementChild ? x.firstElementChild.textContent.trim() : x.textContent.trim()));
    const heads = (page) => page.getByTestId("timeline-hour").allInnerTexts().then((a) => a.map((t) => t.trim()));
    const sub = (page) => page.getByTestId("timeline-sub").innerText().then((t) => t.replace(/\s+/g, " ").trim());
    const pressed = (page, id) => page.getByTestId(id).getAttribute("aria-pressed");
    const row = (page, id) => page.locator(`[data-testid=timeline-item][data-id="${id}"]`);
    const text = (loc) => loc.innerText().then((t) => t.replace(/\s+/g, " ").trim());
    const tlReads = [];   // every GET of the list on a page: its search params
    const track = (page) => {
      const seen = { list: [], raw: [], other: [] };
      page.on("request", (req) => {
        if (req.method() !== "GET") return;
        const u = new URL(req.url());
        if (u.pathname === ROUTE) seen.list.push(u.searchParams);
        else if (u.pathname === RAW) seen.raw.push(u.searchParams);
        else if (u.pathname.startsWith("/api/") && u.pathname !== "/api/whoami") seen.other.push(u.pathname);
      });
      return seen;
    };
    const common = async (page, label, width) => {
      await checkWords(page, ck, label);
      if (width === 390) ck(`${label}: no horizontal scroll`, (await overflow(page)) <= 0, String(await overflow(page)));
    };
    const readOf = async (page, action) => {   // the list read an action causes
      const [resp] = await Promise.all([page.waitForResponse((r) => TL_URL.test(r.url()) && r.request().method() === "GET"), action()]);
      await page.waitForTimeout(150);
      return new URL(resp.url()).searchParams;
    };

    // ── what the live cell holds, from its own API (the times are relative to the seed)
    const six = await api(`${ROUTE}?from=${encodeURIComponent(stamp(new Date(Date.now() - 6 * 3600e3)))}`);
    const win = (list, min) => list.items.filter((i) => i.kind === "window" && i.service === "edge-proxy" && i.raw.count >= min)
      .sort((a, b) => b.raw.count - a.raw.count);
    const bad = win(six, 200)[0];                                   // the bad window: 200+ lines
    const warn = six.items.find((i) => i.kind === "window" && i.service === "edge-proxy" && i.raw.count === 61);
    ck("the cell has a bad window with more than 220 lines and a warn window with 61", !!bad && bad.raw.count > 220 && !!warn, JSON.stringify(bad && bad.raw));
    ck("the cell has every kind of item in 6 hours",
       JSON.stringify([...new Set(six.items.map((i) => i.kind))].sort()) === JSON.stringify(["alert", "check", "deploy", "incident", "metric", "quiet", "window"]),
       [...new Set(six.items.map((i) => i.kind))].join());
    const incident = six.items.find((i) => i.kind === "incident" && i.link);

    // 1. normal: the last 6 hours, all services
    for (const width of [1440, 390]) {
      const { ctx, page } = await newPage(width);
      const seen = track(page);
      const one = expectOneCall(page, ROUTE);
      await page.goto(`${BASE}/timeline`, { waitUntil: "load" });
      await items(page).first().waitFor();
      await one(ck, `normal @${width}`);
      ck(`normal @${width}: no lines were read`, seen.raw.length === 0);
      eq(`normal @${width}: the heading`, await page.getByRole("heading", { level: 1 }).innerText(), "Timeline");
      eq(`normal @${width}: the subtitle names the range and the services`, await sub(page), "Last 6 hours, all services.");
      eq(`normal @${width}: 6 hours is the chosen range`, [await pressed(page, "timeline-range-6h"), await pressed(page, "timeline-range-1h"),
         await pressed(page, "timeline-range-24h"), await pressed(page, "timeline-range-7d"), await pressed(page, "timeline-range-custom")],
         ["true", "false", "false", "false", "false"]);
      eq(`normal @${width}: every kind is on`, await Promise.all(["logs", "metrics", "alerts", "deploys", "checks"].map((k) => pressed(page, `timeline-kind-${k}`))),
         ["true", "true", "true", "true", "true"]);
      eq(`normal @${width}: the filter bar shows no service`, (await text(page.getByTestId("timeline-service"))), "Service All services");
      eq(`normal @${width}: all seven kinds of item are drawn`, await kindsOf(page), ["alert", "check", "deploy", "incident", "metric", "quiet", "window"]);
      const have = await ids(page);
      eq(`normal @${width}: the list is the cell's, in the cell's order`, have, six.items.map((i) => i.id)
         .filter((id) => have.includes(id)));
      ck(`normal @${width}: and nothing the cell sent is missing`, have.length >= six.items.length - 1, `${have.length} of ${six.items.length}`);
      const hs = await heads(page);
      ck(`normal @${width}: hour headings read HH:00, Yesterday HH:00 or D Mon HH:00`, hs.length > 1 && hs.every((h) => HOUR_RE.test(h)), hs.join());
      ck(`normal @${width}: newest hour first`, hs.length === new Set(hs).size);
      const inc = row(page, incident.id);
      eq(`normal @${width}: an incident item says what happened`, await texts(page).then((t) => t.filter((x) => /^Incident opened:/.test(x)).length), 1);
      eq(`normal @${width}: and links to its incident`, await inc.getByTestId("timeline-incident-link").getAttribute("href"), incident.link);
      eq(`normal @${width}: the link says what it opens`, (await inc.getByTestId("timeline-incident-link").innerText()).trim(), "Open the incident");
      ck(`normal @${width}: the bad alert is red and the deploy is plain`,
         (await items(page).evaluateAll((e) => e.filter((x) => x.dataset.kind === "alert").map((x) => x.dataset.tone))).includes("bad")
         && (await items(page).evaluateAll((e) => e.filter((x) => x.dataset.kind === "deploy").every((x) => x.dataset.tone === "info"))));
      eq(`normal @${width}: a metric tag is amber whatever the cell says`, await row(page, six.items.find((i) => i.kind === "metric").id).getByTestId("timeline-tag").getAttribute("data-tone"), "warn");
      eq(`normal @${width}: tags are in words`, [...new Set(await tags(page))].sort(), ["ALERT", "CHECK", "DEPLOY", "INCIDENT", "LOGS", "METRIC"].map((x) => x));
      const deployText = await texts(page).then((t) => t.filter((x) => /example-org\/edge-proxy pushed/.test(x)));
      eq(`normal @${width}: the deploy is the one the incident follows`, deployText, ["example-org/edge-proxy pushed to main by Kim: 3 commits"]);
      eq(`normal @${width}: a window offers its lines`, (await text(row(page, bad.id).getByTestId("timeline-open-raw"))), `Show the ${fmt(bad.raw.count)} lines`);
      ck(`normal @${width}: a time reads as the cell words it`, (await page.getByTestId("timeline-time").allInnerTexts()).every((t) => /^\d\d:\d\d( to \d\d:\d\d)?$/.test(t.trim())));
      ck(`normal @${width}: the earlier page is not offered when there is none`, (await page.getByTestId("timeline-earlier").count()) === 0);
      await common(page, `normal @${width}`, width);
      await shots(page, "", "normal", width);
      eq(`normal @${width}: no script error`, pageErrors, []);
      await ctx.close();
    }

    // 2. raw: the lines behind a window, read only when opened
    for (const width of [1440, 390]) {
      const { ctx, page } = await newPage(width);
      const seen = track(page);
      const one = expectOneCall(page, ROUTE);
      await page.goto(`${BASE}/timeline`, { waitUntil: "load" });
      await row(page, warn.id).waitFor();
      await one(ck, `raw @${width}`);
      const r = row(page, warn.id);
      const gotRaw = page.waitForResponse((x) => RAW_URL.test(x.url()));
      await r.getByTestId("timeline-open-raw").click();
      const resp = await (await gotRaw).json();
      await r.getByTestId("timeline-raw-footer").waitFor();
      eq(`raw @${width}: one read of the lines, for that window and that service, 20 of them`, seen.raw.map((p) => [p.get("service"), p.get("from"), p.get("to"), p.get("limit")]),
         [["edge-proxy", warn.raw.from, warn.raw.to, "20"]]);
      eq(`raw @${width}: 20 lines are shown`, await r.getByTestId("timeline-raw-row").count(), 20);
      eq(`raw @${width}: the footer counts them`, await text(r.getByTestId("timeline-raw-count")), `Showing 20 of ${warn.raw.count} lines.`);
      const clocks = await r.getByTestId("timeline-raw-row").evaluateAll((e) => e.map((x) => x.firstElementChild.textContent.trim()));
      eq(`raw @${width}: each line starts with its time in seconds`, clocks, resp.rows.map((x) => x.at.slice(11, 19)));
      ck(`raw @${width}: newest first`, resp.rows.every((x, i) => i === 0 || resp.rows[i - 1].at >= x.at));
      ck(`raw @${width}: every line is inside the window`, resp.rows.every((x) => x.at >= warn.raw.from && x.at < warn.raw.to), warn.raw.from);
      ck(`raw @${width}: a line carries its fields as k=v`, /\bservice=edge-proxy\b/.test(await r.getByTestId("timeline-raw-row").first().innerText()));
      ck(`raw @${width}: the lines are exempt from the word check`, (await r.getByTestId("timeline-raw-row").evaluateAll((e) => e.every((x) => x.hasAttribute("data-words-exempt")))));
      ck(`raw @${width}: the open link is gone while the lines are open`, (await r.getByTestId("timeline-open-raw").count()) === 0);
      await common(page, `raw @${width}`, width);
      await shots(page, "", "raw", width);
      await r.getByTestId("timeline-raw-more").click();
      await r.getByTestId("timeline-raw-footer").filter({ hasText: "Showing all" }).waitFor();
      eq(`raw @${width}: Show 200 more asks for 220 and gets them all`, [seen.raw.length, seen.raw[1].get("limit"), await r.getByTestId("timeline-raw-row").count()], [2, "220", warn.raw.count]);
      eq(`raw @${width}: the footer says all of them`, await text(r.getByTestId("timeline-raw-count")), `Showing all ${warn.raw.count} lines.`);
      eq(`raw @${width}: no more to show`, await r.getByTestId("timeline-raw-more").count(), 0);
      await r.getByTestId("timeline-raw-hide").click();
      eq(`raw @${width}: Hide closes the lines`, [await r.getByTestId("timeline-raw").count(), await r.getByTestId("timeline-open-raw").count()], [0, 1]);
      eq(`raw @${width}: and reading them again was never needed`, seen.list.length, 1);
      await ctx.close();
    }

    // 2b. a line with no timestamp (TR-894): the page keeps its other lines and shows the odd one without a time
    {
      const { ctx, page } = await newPage(1440);
      await page.route((u) => u.pathname === RAW, async (route) => {
        const res = await route.fetch();
        const body = await res.json();
        body.rows[0].at = null;
        await route.fulfill({ response: res, json: body });
      });
      await page.goto(`${BASE}/timeline`, { waitUntil: "load" });
      await row(page, warn.id).waitFor();
      const r = row(page, warn.id);
      await r.getByTestId("timeline-open-raw").click();
      await r.getByTestId("timeline-raw-footer").waitFor({ timeout: 8000 }).catch(() => {});
      eq("no timestamp: the lines still show, 20 of them", await r.getByTestId("timeline-raw-row").count(), 20);
      eq("no timestamp: the odd line has an empty time", (await r.getByTestId("timeline-raw-row").first().evaluate((e) => e.firstElementChild.textContent.trim())), "");
      ck("no timestamp: the other lines keep theirs", /^\d\d:\d\d:\d\d$/.test(await r.getByTestId("timeline-raw-row").nth(1).evaluate((e) => e.firstElementChild.textContent.trim())));
      ck("no timestamp: the page is still there", (await page.getByTestId("timeline-sub").count()) === 1);
      await ctx.close();
    }

    // 3. service: one service, the last hour, picked on the page
    for (const width of [1440, 390]) {
      const { ctx, page } = await newPage(width);
      const seen = track(page);
      await page.goto(`${BASE}/timeline`, { waitUntil: "load" });
      await items(page).first().waitFor();
      await page.getByTestId("timeline-service").click();
      const options = await page.getByRole("option").allInnerTexts();
      eq(`service @${width}: the picker lists All services and every service the cell knows`, options.map((o) => o.trim()), ["All services", ...six.services]);
      let p = await readOf(page, () => page.getByRole("option", { name: "edge-proxy", exact: true }).click());
      eq(`service @${width}: picking a service reads the list for it`, p.get("service"), "edge-proxy");
      eq(`service @${width}: and puts it in the address`, new URL(page.url()).searchParams.get("service"), "edge-proxy");
      p = await readOf(page, () => page.getByTestId("timeline-range-1h").click());
      eq(`service @${width}: a range reads again`, [p.get("service"), new URL(page.url()).searchParams.get("range")], ["edge-proxy", "1h"]);
      const gap = Date.now() - Date.parse(p.get("from"));
      ck(`service @${width}: the last hour starts an hour back`, Math.abs(gap - 3600e3) < 30e3, String(gap));
      await items(page).first().waitFor();
      eq(`service @${width}: the subtitle`, await sub(page), "Last hour, edge-proxy.");
      eq(`service @${width}: the picker shows the service`, await text(page.getByTestId("timeline-service")), "Service edge-proxy");
      const body = await texts(page);
      ck(`service @${width}: only edge-proxy is on the page`, body.length > 3 && body.every((t) => t.includes("edge-proxy")) && !body.some((t) => t.includes("service-a")), body.join(" | "));
      const hour = await api(`${ROUTE}?service=edge-proxy&from=${encodeURIComponent(stamp(new Date(Date.now() - 3600e3)))}`);
      const have = await ids(page);
      ck(`service @${width}: the list is the cell's last hour for it`, have.length >= hour.items.length - 1 && have.every((id) => hour.items.some((i) => i.id === id)), `${have.length} vs ${hour.items.length}`);
      ck(`service @${width}: nothing older than an hour (the folded quiet stretch begins before it and shows from where the hour begins)`,
         hour.items.every((i) => Date.parse(i.end || i.at) >= Date.now() - 3600e3 - 60e3));
      await common(page, `service @${width}`, width);
      await shots(page, "", "service", width);
      // back and forward walk the filters
      await page.goBack();
      await page.waitForFunction(() => !new URL(location.href).searchParams.has("range"));
      eq(`service @${width}: back drops the range and keeps the service`, [new URL(page.url()).searchParams.get("service"), await pressed(page, "timeline-range-6h")], ["edge-proxy", "true"]);
      await page.goForward();
      await page.waitForFunction(() => new URL(location.href).searchParams.get("range") === "1h");
      eq(`service @${width}: forward brings it back`, await pressed(page, "timeline-range-1h"), "true");
      // a reload keeps the filters
      await page.reload({ waitUntil: "load" });
      await items(page).first().waitFor();
      eq(`service @${width}: a reload keeps service and range`, [await sub(page), await text(page.getByTestId("timeline-service")), await pressed(page, "timeline-range-1h")],
         ["Last hour, edge-proxy.", "Service edge-proxy", "true"]);
      await ctx.close();
      void seen;
    }

    // 4. kinds: turning kinds on and off
    for (const width of [1440, 390]) {
      const { ctx, page } = await newPage(width);
      const seen = track(page);
      const one = expectOneCall(page, ROUTE);
      await page.goto(`${BASE}/timeline?range=24h&kinds=alerts,deploys`, { waitUntil: "load" });
      await items(page).first().waitFor();
      await one(ck, `kinds @${width}`);
      eq(`kinds @${width}: the read asks for the kinds in the address`, [seen.list[0].get("kinds")], ["alerts,deploys"]);
      eq(`kinds @${width}: the subtitle`, await sub(page), "Last 24 hours, all services.");
      eq(`kinds @${width}: only alerts and deploys are on`, await Promise.all(["logs", "metrics", "alerts", "deploys", "checks"].map((k) => pressed(page, `timeline-kind-${k}`))),
         ["false", "false", "true", "true", "false"]);
      eq(`kinds @${width}: only alerts and deploys are drawn`, await kindsOf(page), ["alert", "deploy"]);
      const body = await texts(page);
      ck(`kinds @${width}: an alert that resolved hours ago is there`, body.some((t) => /^Alert BuildFailing on docs-site resolved$/.test(t)) && body.some((t) => /^Alert BuildFailing is active on docs-site/.test(t)));
      ck(`kinds @${width}: hour headings read HH:00, Yesterday HH:00 or D Mon HH:00`, (await heads(page)).every((h) => HOUR_RE.test(h)));
      await common(page, `kinds @${width}`, width);
      await shots(page, "", "kinds", width);
      // switch a kind on: logs come back, and the address says so
      let p = await readOf(page, () => page.getByTestId("timeline-kind-logs").click());
      eq(`kinds @${width}: turning Logs on reads again with it`, [p.get("kinds"), new URL(page.url()).searchParams.get("kinds")], ["logs,alerts,deploys", "logs,alerts,deploys"]);
      ck(`kinds @${width}: and windows are drawn`, (await kindsOf(page)).includes("window"));
      // turn the rest on: all kinds is no kinds in the address
      for (const k of ["metrics", "checks"]) p = await readOf(page, () => page.getByTestId(`timeline-kind-${k}`).click());
      eq(`kinds @${width}: all five on leaves kinds out of the address`, [new URL(page.url()).searchParams.has("kinds"), p.has("kinds")], [false, false]);
      // and down to one: the last one stays on
      for (const k of ["logs", "metrics", "deploys", "checks"]) p = await readOf(page, () => page.getByTestId(`timeline-kind-${k}`).click());
      eq(`kinds @${width}: down to Alerts only`, [p.get("kinds"), await kindsOf(page).then((k) => k.join())], ["alerts", "alert"]);
      const before = seen.list.length;
      await page.getByTestId("timeline-kind-alerts").click({ force: true });   // aria-disabled: Playwright would wait for it
      await page.waitForTimeout(300);
      eq(`kinds @${width}: the last kind stays on, with no read`, [await pressed(page, "timeline-kind-alerts"), await page.getByTestId("timeline-kind-alerts").getAttribute("aria-disabled"), seen.list.length === before],
         ["true", "true", true]);
      await page.reload({ waitUntil: "load" });
      await items(page).first().waitFor();
      eq(`kinds @${width}: a reload keeps the kinds`, [await kindsOf(page).then((k) => k.join()), await pressed(page, "timeline-kind-alerts"), await pressed(page, "timeline-kind-logs")], ["alert", "true", "false"]);
      await ctx.close();
    }

    // 5. empty: a service that sent nothing in the range
    for (const width of [1440, 390]) {
      const { ctx, page } = await newPage(width);
      const one = expectOneCall(page, ROUTE);
      await page.goto(`${BASE}/timeline?service=service-a-receiver&range=6h`, { waitUntil: "load" });
      await page.getByTestId("timeline-empty").waitFor();
      await one(ck, `empty @${width}`);
      const want = (await api(`${ROUTE}?service=service-a-receiver&from=${encodeURIComponent(stamp(new Date(Date.now() - 6 * 3600e3)))}`)).empty_words;
      eq(`empty @${width}: the cell's sentence and a hint`, await text(page.getByTestId("timeline-empty")), `Nothing arrived ${want} Try a longer range or all services.`);
      eq(`empty @${width}: no list, no error`, [await page.getByTestId("timeline-list").count(), await page.getByTestId("timeline-error").count()], [0, 0]);
      eq(`empty @${width}: the picker still shows the service`, await text(page.getByTestId("timeline-service")), "Service service-a-receiver");
      await common(page, `empty @${width}`, width);
      await shots(page, "", "empty", width);
      // a kind filter that leaves nothing says so in its own words
      await page.goto(`${BASE}/timeline?service=docs-site&range=1h&kinds=deploys`, { waitUntil: "load" });
      await page.getByTestId("timeline-empty").waitFor();
      ck(`empty @${width}: a narrowed list says Nothing to show`, (await text(page.getByTestId("timeline-empty"))).startsWith("Nothing to show"), await text(page.getByTestId("timeline-empty")));
      await ctx.close();
    }

    // 6. loading: the first read has not come back
    for (const width of [1440, 390]) {
      const { ctx, page } = await newPage(width);
      let release;
      const gate = new Promise((r) => { release = r; });
      await page.route(TL_URL, async (route) => { await gate; await route.continue(); });
      const one = expectOneCall(page, ROUTE);
      await page.goto(`${BASE}/timeline`, { waitUntil: "load" });
      await page.getByTestId("timeline-loading").waitFor();
      await page.waitForTimeout(400);
      eq(`loading @${width}: skeleton rows, and no list, error or empty state`,
         [await page.getByTestId("timeline-loading").count(), await page.getByTestId("timeline-list").count(), await page.getByTestId("timeline-error").count(), await page.getByTestId("timeline-empty").count()], [1, 0, 0, 0]);
      eq(`loading @${width}: the filter bar is already there`, [await page.getByTestId("timeline-range").count(), await page.getByTestId("timeline-kinds").count()], [1, 1]);
      ck(`loading @${width}: the subtitle holds its line`, (await page.getByTestId("timeline-sub").evaluate((e) => e.getBoundingClientRect().height)) > 10);
      eq(`loading @${width}: the loading area is announced`, await page.getByTestId("timeline-loading").getAttribute("aria-busy"), "true");
      await common(page, `loading @${width}`, width);
      await shots(page, "", "loading", width);
      release();
      await items(page).first().waitFor();
      await one(ck, `loading @${width}`);
      eq(`loading @${width}: the list replaces it`, [await page.getByTestId("timeline-loading").count(), (await items(page).count()) > 3], [0, true]);
      await ctx.close();
    }

    // 7. error: the list did not load; it tries again by itself and on a click
    for (const width of [1440, 390]) {
      const { ctx, page } = await newPage(width);
      await installClock(page);
      let down = true;
      await page.route(TL_URL, (route) => down
        ? route.fulfill({ status: 500, contentType: "application/json", body: JSON.stringify({ error: "down" }) })
        : route.continue());
      const seen = track(page);
      const one = expectOneCall(page, ROUTE);
      await page.goto(`${BASE}/timeline`, { waitUntil: "load" });
      await page.getByTestId("timeline-error").waitFor();
      await one(ck, `error @${width}`);
      eq(`error @${width}: the sentence, with the real poll interval`, await text(page.getByTestId("timeline-error")),
         "The timeline did not load. Tares could not read it just now. It will try again in 30 seconds. Try now");
      eq(`error @${width}: no list, no loading, no empty state`, [await page.getByTestId("timeline-list").count(), await page.getByTestId("timeline-loading").count(), await page.getByTestId("timeline-empty").count()], [0, 0, 0]);
      eq(`error @${width}: the filter bar stays`, await page.getByTestId("timeline-kinds").count(), 1);
      await common(page, `error @${width}`, width);
      await shots(page, "", "error", width);
      // 30 seconds later it reads again by itself, and recovers
      down = false;
      await page.clock.fastForward(30_000);
      await items(page).first().waitFor();
      eq(`error @${width}: after 30 seconds it tried again and the list is back`, [seen.list.length, await page.getByTestId("timeline-error").count()], [2, 0]);
      // and Try now reads at once
      down = true;
      await page.clock.fastForward(30_000);
      await page.getByTestId("timeline-error").waitFor();
      down = false;
      const n = seen.list.length;
      await page.getByTestId("timeline-retry").click();
      await page.getByTestId("timeline-error").waitFor({ state: "detached" });   // a failed poll keeps the list it had under the banner
      eq(`error @${width}: Try now reads once and recovers`, [seen.list.length - n, await page.getByTestId("timeline-error").count()], [1, 0]);
      await ctx.close();
    }

    // 8. rawerr: the lines of a window did not load
    for (const width of [1440, 390]) {
      const { ctx, page } = await newPage(width);
      let down = true;
      await page.route(RAW_URL, (route) => down
        ? route.fulfill({ status: 500, contentType: "application/json", body: JSON.stringify({ error: "down" }) })
        : route.continue());
      const seen = track(page);
      const one = expectOneCall(page, ROUTE);
      await page.goto(`${BASE}/timeline`, { waitUntil: "load" });
      await row(page, warn.id).waitFor();
      await one(ck, `rawerr @${width}`);
      const r = row(page, warn.id);
      await r.getByTestId("timeline-open-raw").click();
      await r.getByTestId("timeline-raw-error").waitFor();
      eq(`rawerr @${width}: the sentence and a button`, await text(r.getByTestId("timeline-raw-error")), "The lines did not load. Try again");
      eq(`rawerr @${width}: the list around it is untouched`, [seen.list.length, (await items(page).count()) > 3, await page.getByTestId("timeline-error").count()], [1, true, 0]);
      await common(page, `rawerr @${width}`, width);
      await shots(page, "", "rawerr", width);
      down = false;
      await r.getByTestId("timeline-raw-retry").click();
      await r.getByTestId("timeline-raw-footer").waitFor();
      eq(`rawerr @${width}: Try again reads the lines again and shows them`, [seen.raw.length, await r.getByTestId("timeline-raw-row").count(), await r.getByTestId("timeline-raw-error").count()], [2, 20, 0]);
      await ctx.close();
    }

    // 9. long: a line of 500 characters among the lines, and more than a box holds
    for (const width of [1440, 390]) {
      const { ctx, page } = await newPage(width);
      const seen = track(page);
      await page.goto(`${BASE}/timeline`, { waitUntil: "load" });
      await row(page, bad.id).waitFor();
      const r = row(page, bad.id);
      await r.getByTestId("timeline-open-raw").click();
      await r.getByTestId("timeline-raw-footer").waitFor();
      const rows = r.getByTestId("timeline-raw-row");
      const long = await rows.evaluateAll((e) => e.map((x) => x.textContent.length));
      ck(`long @${width}: the long line is among the first 20`, long.some((n) => n > 450), long.join());
      const fit = await rows.evaluateAll((e) => e.every((x) => x.scrollWidth <= x.clientWidth + 1));
      ck(`long @${width}: it wraps inside its row`, fit);
      ck(`long @${width}: and inside the page`, (await overflow(page)) <= 0, String(await overflow(page)));
      eq(`long @${width}: the footer`, await text(r.getByTestId("timeline-raw-count")), `Showing 20 of ${fmt(bad.raw.count)} lines.`);
      await common(page, `long @${width}`, width);
      await shots(page, "", "long", width);
      await r.getByTestId("timeline-raw-more").click();
      await r.getByTestId("timeline-raw-footer").filter({ hasText: "of" }).filter({ hasText: "220" }).waitFor();
      eq(`long @${width}: Show 200 more gives 220 of them, and more is still offered`, [await rows.count(), await text(r.getByTestId("timeline-raw-count")), await r.getByTestId("timeline-raw-more").count()],
         [220, `Showing 220 of ${fmt(bad.raw.count)} lines.`, 1]);
      await r.getByTestId("timeline-raw-more").click();
      await r.getByTestId("timeline-raw-footer").filter({ hasText: "Showing all" }).waitFor();
      eq(`long @${width}: and once more gives all of them`, [await rows.count(), seen.raw.map((p) => p.get("limit")).join()], [bad.raw.count, "20,220,420"]);
      await ctx.close();
    }

    // 10. many: a full page and an older one behind it
    for (const width of [1440, 390]) {
      const { ctx, page } = await newPage(width);
      const pages = [];
      page.on("response", async (resp) => { if (TL_URL.test(resp.url()) && resp.request().method() === "GET") { try { pages.push(await resp.json()); } catch (e) { /* page closed */ } } });
      const seen = track(page);
      const one = expectOneCall(page, ROUTE);
      await page.goto(`${BASE}/timeline?range=24h`, { waitUntil: "load" });
      await items(page).first().waitFor();
      await one(ck, `many @${width}`);
      await page.waitForTimeout(300);
      eq(`many @${width}: a full page of 200 and an older page behind it`, [pages[0].items.length, !!pages[0].next_before, await items(page).count()], [200, true, 200]);
      eq(`many @${width}: the subtitle`, await sub(page), "Last 24 hours, all services.");
      eq(`many @${width}: the list is in the cell's order`, await ids(page), pages[0].items.map((i) => i.id));
      const hs = await heads(page);
      ck(`many @${width}: hours read HH:00, Yesterday HH:00 or D Mon HH:00, each once`, hs.every((h) => HOUR_RE.test(h)) && hs.length === new Set(hs).size, hs.join());
      await common(page, `many @${width}`, width);
      await shots(page, "", "many", width);
      // the older page asks for the cursor of the first, fails once, then comes
      let down = true;
      await page.route(TL_URL, (route) => down && route.request().url().includes("before=")
        ? route.fulfill({ status: 500, contentType: "application/json", body: JSON.stringify({ error: "down" }) })
        : route.continue());
      await page.getByTestId("timeline-earlier").click();
      await page.getByTestId("timeline-earlier-error").waitFor();
      eq(`many @${width}: a failed older page says so and offers a retry`, [await text(page.getByTestId("timeline-earlier-error")), await text(page.getByTestId("timeline-earlier")), await items(page).count()],
         ["The earlier items did not load.", "Try again", 200]);
      down = false;
      await page.getByTestId("timeline-earlier").click();
      await page.waitForFunction(() => document.querySelectorAll("[data-testid=timeline-item]").length > 200);
      const askedBefore = seen.list.map((p) => p.get("before")).filter(Boolean);
      eq(`many @${width}: the older page is asked for with the cursor of the page before`, [askedBefore.length, askedBefore[0]], [2, pages[0].next_before]);
      await page.waitForTimeout(300);
      const second = pages.filter((x) => Array.isArray(x.items))[1];
      eq(`many @${width}: the older items follow the first 200 in the cell's order`, await ids(page), [...pages[0].items, ...second.items].map((i) => i.id));
      ck(`many @${width}: nothing repeats`, (await ids(page)).length === new Set(await ids(page)).size);
      eq(`many @${width}: no older page is offered after the last one`, [await page.getByTestId("timeline-earlier").count(), second.next_before], [0, null]);
      await ctx.close();
    }

    // 11. custom range: two times in UTC, applied together
    for (const width of [1440, 390]) {
      const { ctx, page } = await newPage(width);
      const seen = track(page);
      await page.goto(`${BASE}/timeline`, { waitUntil: "load" });
      await items(page).first().waitFor();
      await page.getByTestId("timeline-range-custom").click();
      await page.getByTestId("timeline-custom").waitFor();
      await settle(page);   // the read that Custom itself causes must be over before Apply is watched
      eq(`custom @${width}: Custom puts range=custom in the address and opens two time fields`, [new URL(page.url()).searchParams.get("range"), await page.getByTestId("timeline-custom-apply").isDisabled()], ["custom", true]);
      const from = new Date(B.getTime() - 120 * 60e3), to = new Date(B.getTime() - 10 * 60e3);
      await page.getByTestId("timeline-custom-from").fill(local(from));
      await page.getByTestId("timeline-custom-to").fill(local(new Date(from.getTime() - 60e3)));
      ck(`custom @${width}: an end before the start cannot be applied, and the hint says why`, (await page.getByTestId("timeline-custom-apply").isDisabled()) && (await page.getByText("Pick a start before the end, up to 31 days.").count()) === 1);
      await page.getByTestId("timeline-custom-to").fill(local(new Date(from.getTime() + 32 * 86400e3)));
      ck(`custom @${width}: more than 31 days cannot be applied either`, await page.getByTestId("timeline-custom-apply").isDisabled());
      await page.getByTestId("timeline-custom-to").fill(local(to));
      const p = await readOf(page, () => page.getByTestId("timeline-custom-apply").click());
      eq(`custom @${width}: Apply reads the range in UTC`, [p.get("from"), p.get("to")], [`${local(from)}:00Z`, `${local(to)}:00Z`]);
      eq(`custom @${width}: and keeps it in the address`, [new URL(page.url()).searchParams.get("from"), new URL(page.url()).searchParams.get("to")], [local(from), local(to)]);
      await items(page).first().waitFor();
      const want = (await api(`${ROUTE}?from=${local(from)}:00Z&to=${local(to)}:00Z`)).range_words;
      eq(`custom @${width}: the subtitle is the cell's words for the range`, await sub(page), `${want}, all services.`);
      ck(`custom @${width}: the list is not empty and holds nothing from after the end`, (await items(page).count()) > 3);
      await page.reload({ waitUntil: "load" });
      await items(page).first().waitFor();
      eq(`custom @${width}: a reload keeps the range and fills the fields`, [await pressed(page, "timeline-range-custom"), await page.getByTestId("timeline-custom-from").inputValue(), await page.getByTestId("timeline-custom-to").inputValue()],
         ["true", local(from), local(to)]);
      await common(page, `custom @${width}`, width);
      // a preset takes the custom times out of the address
      await readOf(page, () => page.getByTestId("timeline-range-24h").click());
      eq(`custom @${width}: a preset clears from and to`, [new URL(page.url()).searchParams.has("from"), new URL(page.url()).searchParams.has("to"), await page.getByTestId("timeline-custom").count()], [false, false, 0]);
      await ctx.close();
      void seen;
    }

    // 12. the list reads again every 30 seconds, and nothing else does
    {
      const { ctx, page } = await newPage(1440);
      await installClock(page);
      const seen = track(page);
      await page.goto(`${BASE}/timeline`, { waitUntil: "load" });
      await items(page).first().waitFor();
      const before = await ids(page);
      await page.clock.fastForward(29_000);
      await page.waitForTimeout(300);
      eq("poll: no read before 30 seconds", seen.list.length, 1);
      await page.clock.fastForward(2_000);
      await page.waitForFunction(() => true);
      await page.waitForTimeout(400);
      eq("poll: one read at 30 seconds, and no other call", [seen.list.length, seen.raw.length, seen.other.length], [2, 0, 0]);
      eq("poll: the same list stays", await ids(page), before);
      // an open box of lines survives a refresh
      await row(page, warn.id).getByTestId("timeline-open-raw").click();
      await row(page, warn.id).getByTestId("timeline-raw-footer").waitFor();
      await page.clock.fastForward(30_000);
      await page.waitForTimeout(400);
      eq("poll: open lines stay open through a refresh", [seen.list.length, await row(page, warn.id).getByTestId("timeline-raw-row").count()], [3, 20]);
      await ctx.close();
    }

    // 12b. a refresh replaces the first page: a quiet stretch whose start moved is one line, not two
    {
      const { ctx, page } = await newPage(1440);
      await installClock(page);
      let reads = 0;
      await page.route(TL_URL, async (route) => {
        const resp = await route.fetch();
        const body = await resp.json();
        reads++;
        body.items[0] = { ...body.items[0], id: `quiet:burst-01:${reads === 1 ? "A" : "B"}` };   // the same stretch, its start moved
        await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
      });
      await page.goto(`${BASE}/timeline`, { waitUntil: "load" });
      await items(page).first().waitFor();
      const first = await ids(page);
      eq("refresh: the first read has the stretch as it started", [first.includes("quiet:burst-01:A"), first.includes("quiet:burst-01:B")], [true, false]);
      await page.clock.fastForward(31_000);
      await page.waitForTimeout(500);
      const after = await ids(page);
      eq("refresh: after a poll only the new stretch is listed, and the list is no longer", [reads, after.includes("quiet:burst-01:A"), after.includes("quiet:burst-01:B"), after.length], [2, false, true, first.length]);
      await ctx.close();
    }

    // 12b2. a list of two pages: a poll before "Load earlier" must not leave the old stretch behind
    {
      const { ctx, page } = await newPage(1440);
      await installClock(page);
      let firstPageReads = 0, earlierReads = 0;
      let head = null;
      await page.route(TL_URL, async (route) => {
        const resp = await route.fetch();
        const body = await resp.json();
        const asked = new URL(route.request().url()).searchParams.has("before");
        if (asked) {
          earlierReads++;
          body.items = [{ ...head.tail, id: "quiet:burst-01:B" }];   // the older page: the same stretch, start moved
          body.next_before = null;
        } else {
          firstPageReads++;
          head = head || { items: body.items.slice(0, 3), tail: body.items[3] };
          body.items = firstPageReads === 1 ? [...head.items, { ...head.tail, id: "quiet:burst-01:A" }]
            : firstPageReads === 3 ? [...head.items, { ...head.items[0], id: "quiet:burst-01:B" }]   // the older stretch has moved up into the first page
            : head.items;
          body.next_before = "2020-01-01T00:00:00Z";
        }
        await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
      });
      await page.goto(`${BASE}/timeline`, { waitUntil: "load" });
      await items(page).first().waitFor();
      eq("two pages: the first read lists the stretch as it started", (await ids(page)).includes("quiet:burst-01:A"), true);
      await page.clock.fastForward(31_000);
      await page.waitForTimeout(500);
      const polled = await ids(page);
      eq("two pages: a poll before any earlier page replaces the list", [firstPageReads, polled.includes("quiet:burst-01:A"), polled.length], [2, false, 3]);
      await page.getByTestId("timeline-earlier").click();
      await page.waitForTimeout(500);
      const both = await ids(page);
      eq("two pages: Load earlier adds the stretch once", [earlierReads, both.filter((i) => i.startsWith("quiet:burst-01:")), both.length], [1, ["quiet:burst-01:B"], 4]);
      await page.clock.fastForward(31_000);
      await page.waitForTimeout(500);
      const again = await ids(page);
      eq("two pages: a poll after Load earlier keeps the older page and still lists the stretch once", [firstPageReads, again.filter((i) => i.startsWith("quiet:burst-01:")), again.length], [3, ["quiet:burst-01:B"], 4]);
      await ctx.close();
    }

    // 12c. a range in the address without range=custom is a custom range; a range that ends is not polled
    {
      const from = new Date(B.getTime() - 120 * 60e3), to = new Date(B.getTime() - 10 * 60e3);
      const { ctx, page } = await newPage(1440);
      await installClock(page);
      const seen = track(page);
      await page.goto(`${BASE}/timeline?service=edge-proxy&from=${local(from)}&to=${local(to)}`, { waitUntil: "load" });
      await page.getByTestId("timeline-custom").waitFor();
      await settle(page);
      eq("deep link: from and to alone open the custom range", [await pressed(page, "timeline-range-custom"), await page.getByTestId("timeline-custom-from").inputValue(), await page.getByTestId("timeline-custom-to").inputValue()],
         ["true", local(from), local(to)]);
      eq("deep link: the cell is asked for that range and that service", [seen.list.length, seen.list[0].get("from"), seen.list[0].get("to"), seen.list[0].get("service")],
         [1, `${local(from)}:00Z`, `${local(to)}:00Z`, "edge-proxy"]);
      await page.clock.fastForward(95_000);
      await page.waitForTimeout(500);
      eq("fixed range: no read after 95 seconds, it cannot change", seen.list.length, 1);
      await ctx.close();
    }
    {
      const from = new Date(B.getTime() - 120 * 60e3);
      const { ctx, page } = await newPage(1440);
      await installClock(page);
      const seen = track(page);
      await page.goto(`${BASE}/timeline?range=custom&from=${local(from)}`, { waitUntil: "load" });
      await page.getByTestId("timeline-custom").waitFor();
      await settle(page);
      await page.clock.fastForward(31_000);
      await page.waitForTimeout(500);
      eq("open-ended range: a start with no end still reads every 30 seconds", seen.list.length, 2);
      await ctx.close();
    }

    // 13. the samples: all ten states from ?sample=, no call at all
    const sampleDone = {};
    for (const width of [1440, 390]) {
      for (const name of SAMPLES) {
        const { ctx, page } = await newPage(width, true);
        const seen = track(page);
        await page.goto(`${BASE}/timeline?sample=${name}`, { waitUntil: "load" });
        const label = `sample ${name} @${width}`;
        const anchor = { normal: "timeline-item", raw: "timeline-raw-footer", service: "timeline-item", kinds: "timeline-item", empty: "timeline-empty", load: "timeline-loading",
                         err: "timeline-error", rawerr: "timeline-raw-error", long: "timeline-raw-footer", many: "timeline-item" }[name];
        await page.getByTestId(anchor).first().waitFor();
        await settle(page);
        eq(`${label}: no call to the cell`, [seen.list.length, seen.raw.length, seen.other.length], [0, 0, 0]);
        const t = await texts(page);
        switch (name) {
          case "normal":
            eq(`${label}: the heading, subtitle, and hours`, [await sub(page), await heads(page)], ["Last 6 hours, all services.", ["15:00", "14:00", "13:00"]]);
            eq(`${label}: ten items, newest end first, in the cell's order`, await tags(page), ["INCIDENT", "INCIDENT", "LOGS", "ALERT", "LOGS", "DEPLOY", "CHECK", "LOGS", "METRIC", "LOGS"]);
            ck(`${label}: the deploy and the incident agree`, t.includes("example-org/edge-proxy pushed to main by Kim: 3 commits") && t.some((x) => /edge-proxy errors rose after the 14:52 deploy$/.test(x)));
            ck(`${label}: the incident is opened where its window ends`, (await page.getByTestId("timeline-time").allInnerTexts()).filter((x) => x.trim() === "15:10" || x.trim() === "15:00 to 15:10").length === 2);
            eq(`${label}: the incident links`, await page.getByTestId("timeline-incident-link").evaluateAll((e) => e.map((x) => x.getAttribute("href"))), ["/incidents/inc-sample-1", "/incidents/inc-sample-1"]);
            eq(`${label}: window counts and their lines agree`, [t[2], await text(page.getByTestId("timeline-open-raw").first())], ["edge-proxy: 214 events, 41 errors, errors up 10x on the 10 minutes before", "Show the 214 lines"]);
            break;
          case "raw": {
            const times = await page.getByTestId("timeline-raw-row").evaluateAll((e) => e.map((x) => x.firstElementChild.textContent.trim()));
            eq(`${label}: four lines are open`, [times.length, await text(page.getByTestId("timeline-raw-count"))], [4, "Showing 4 of 214 lines."]);
            ck(`${label}: each line is inside the window it sits under, newest first`, times.every((x, i) => x >= "15:00:00" && x < "15:10:00" && (i === 0 || times[i - 1] >= x)), times.join());
            await page.getByTestId("timeline-raw-more").click();
            await page.getByTestId("timeline-raw-footer").filter({ hasText: "Showing all" }).waitFor();
            eq(`${label}: Show 200 more shows them all`, [await page.getByTestId("timeline-raw-row").count(), await text(page.getByTestId("timeline-raw-count"))], [214, "Showing all 214 lines."]);
            await page.getByTestId("timeline-raw-hide").click();
            break;
          }
          case "service":
            eq(`${label}: the last hour of one service`, [await sub(page), await heads(page), await text(page.getByTestId("timeline-service"))], ["Last hour, edge-proxy.", ["15:00", "14:00"], "Service edge-proxy"]);
            ck(`${label}: no other service's deploy, nothing before the hour began`, !t.some((x) => /service-a|docs-site|api-server/.test(x))
               && (await page.getByTestId("timeline-time").allInnerTexts()).every((x) => x.trim() >= "14:16"));
            break;
          case "kinds":
            eq(`${label}: alerts and deploys over a day`, [await sub(page), [...new Set(await tags(page))].sort(), await heads(page)], ["Last 24 hours, all services.", ["ALERT", "DEPLOY"], ["15:00", "14:00", "Yesterday 17:00"]]);
            eq(`${label}: only those kinds are on`, await Promise.all(["logs", "metrics", "alerts", "deploys", "checks"].map((k) => pressed(page, `timeline-kind-${k}`))), ["false", "false", "true", "true", "false"]);
            break;
          case "empty":
            eq(`${label}: the sentence and the hint`, await text(page.getByTestId("timeline-empty")), "Nothing arrived Nothing arrived for edge-proxy in the last 6 hours. Try a longer range or all services.");
            break;
          case "load":
            eq(`${label}: skeleton rows only`, [await page.getByTestId("timeline-loading").count(), await page.getByTestId("timeline-list").count(), await page.getByTestId("timeline-error").count()], [1, 0, 0]);
            break;
          case "err":
            eq(`${label}: the sentence with the real poll interval`, await text(page.getByTestId("timeline-error")), "The timeline did not load. Tares could not read it just now. It will try again in 30 seconds. Try now");
            break;
          case "rawerr":
            eq(`${label}: the sentence`, await text(page.getByTestId("timeline-raw-error")), "The lines did not load. Try again");
            break;
          case "long": {
            const lens = await page.getByTestId("timeline-raw-row").evaluateAll((e) => e.map((x) => x.textContent.length));
            eq(`${label}: three lines, the third is long`, [lens.length, lens[2] > 450], [3, true]);
            ck(`${label}: and wraps inside its row`, await page.getByTestId("timeline-raw-row").evaluateAll((e) => e.every((x) => x.scrollWidth <= x.clientWidth + 1)));
            break;
          }
          case "many": {
            eq(`${label}: a full page`, [await items(page).count(), await sub(page)], [200, "Last 24 hours, all services."]);
            const nums = (await texts(page)).map((x) => (x.match(/\(#(\d+)\)$/) || [])[1]).filter(Boolean);
            ck(`${label}: no pull request number repeats`, nums.length > 20 && nums.length === new Set(nums).size, `${nums.length} vs ${new Set(nums).size}`);
            const order = await page.evaluate(() => {
              const out = []; let cur = null;
              for (const el of document.querySelectorAll("[data-testid=timeline-hour],[data-testid=timeline-time]")) {
                if (el.dataset.testid === "timeline-hour") { cur = []; out.push(cur); } else { const m = el.textContent.trim().match(/(\d\d):(\d\d)$/); cur.push(Number(m[1]) * 60 + Number(m[2])); }
              }
              return out;
            });
            ck(`${label}: inside an hour, newest first`, order.every((g) => g.every((v, i) => i === 0 || g[i - 1] >= v)));
            await page.getByTestId("timeline-earlier").click();
            await page.waitForFunction(() => document.querySelectorAll("[data-testid=timeline-item]").length > 200);
            eq(`${label}: Load earlier adds the older page and then goes`, [(await items(page).count()) > 200, await page.getByTestId("timeline-earlier").count()], [true, 0]);
            break;
          }
        }
        if (name === "many" || name === "raw") await page.reload({ waitUntil: "load" }), await page.getByTestId(anchor).first().waitFor(), await settle(page);
        await common(page, label, width);
        await shots(page, "sample-", name, width);
        sampleDone[name] = true;
        await ctx.close();
      }
    }
    eq("every state of the mock was drawn from a sample", Object.keys(sampleDone), SAMPLES);

    // the address alone shows no sample on a live cell
    {
      const { ctx, page } = await newPage(1440);
      await page.goto(`${BASE}/timeline?sample=empty`, { waitUntil: "load" });
      await items(page).first().waitFor();
      eq("the sample address alone shows the live cell", [await page.getByTestId("timeline-empty").count(), (await items(page).count()) > 3], [0, true]);
      await ctx.close();
    }
    eq("no script error on any page", pageErrors, []);
  } finally {
    if (browser) await browser.close();
    daemon.kill();
    fs.rmSync(home, { recursive: true, force: true });
  }
  console.log(`\n${pass} passed, ${fail} failed`);
  process.exit(fail ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(1); });
