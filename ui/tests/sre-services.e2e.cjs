/*
 * Browser e2e for TR-750: the AI SRE Services list (/services) and a page per service (/services/:name).
 *
 * The list is every service as one row from one read of the cell (GET /api/sre/services): name, what
 * the person calls it, the state in words, signals and where they come from, a sparkline, events in
 * 24 hours and when it was last checked. The rows show a page at a time ("Showing N of M"); hidden
 * services stay out until "Show hidden (n)" is pressed. A service page shows its state, a chart of
 * errors and traffic on one axis with a dashed mark per deploy and the list of deploys, incidents,
 * what the watcher raised, blind spots, the repos its deploys come from, and its settings (rename,
 * hide, join another service, link repos). A silent service opens on the last week. Every state shows
 * only words from words.json and none scrolls sideways at 390 px.
 *
 * Seeds a scratch AI SRE cell (tests/seed-sre-services.py over tests/fixtures/sre_cell.py, invented
 * names), then runs the real daemon on it, serving the built console. The sample states (?sample=<name>,
 * with the sample flag in localStorage) need no data.
 * Run:  npm run build && node tests/sre-services.e2e.cjs     (from ui/, tares installed in ../.venv)
 * Set SHOTS_DIR to keep a screenshot of every state at 1440 and 390 px (default: a temporary directory).
 * TARES_E2E_PORT sets the daemon port (default 8863); the next port is its MCP endpoint.
 * TARES_E2E_CHROME points Playwright at another Chrome binary when its own download is missing.
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

const PORT = Number(process.env.TARES_E2E_PORT || 8863);
const BASE = `http://127.0.0.1:${PORT}`;
const SHOTS = process.env.SHOTS_DIR || fs.mkdtempSync(path.join(os.tmpdir(), "tares-sre-services-shots-"));
fs.mkdirSync(SHOTS, { recursive: true });
const LIST = "/api/sre/services";
const PAGE_SIZE = 20;
const LIST_SAMPLES = ["normal", "empty", "error", "long", "many", "load", "hidden"];
const SERVICE_SAMPLES = ["normal", "incidents", "long", "silent", "quiet", "error", "load", "missing", "readonly", "savefail", "join"];

let pass = 0, fail = 0;
const ck = (label, cond, detail = "") => {
  cond ? pass++ : fail++;
  console.log((cond ? "  ok   " : "  FAIL ") + label + (cond ? "" : `   ${detail}`));
};

async function waitFor(url, tries = 100) {
  for (; tries > 0; tries--) {
    const ok = await new Promise((res) => http.get(url, (r) => { r.resume(); res(true); }).on("error", () => res(false)));
    if (ok) return true;
    await new Promise((r) => setTimeout(r, 200));
  }
  return false;
}

const getJson = (p) => new Promise((res, rej) => http.get(BASE + p, (r) => {
  let b = "";
  r.on("data", (c) => (b += c));
  r.on("end", () => { try { res(JSON.parse(b)); } catch (e) { rej(e); } });
}).on("error", rej));

(async () => {
  const bin = path.join(REPO, ".venv/bin");
  const tares = fs.existsSync(path.join(bin, "tares")) ? path.join(bin, "tares") : "tares";
  const python = fs.existsSync(path.join(bin, "python")) ? path.join(bin, "python") : "python3";
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "tares-sre-services-e2e-"));
  const dist = process.env.TARES_UI_DIST || path.join(UI_DIR, "dist");
  const made = JSON.parse(execFileSync(python, [path.join(__dirname, "seed-sre-services.py"), home],
    { cwd: REPO, encoding: "utf8", env: { ...process.env, PYTHONPATH: REPO } }).trim().split("\n").pop());
  ck("seeded the cell", made.services.includes("edge-proxy") && made.services.length > PAGE_SIZE);

  const env = { ...process.env, TARES_EDITION: "sre", TARES_HOME: home, TARES_PORT: String(PORT), TARES_MCP_PORT: String(PORT + 1),
                TARES_UI_DIST: dist, TARES_OTLP_GRPC_PORT: "off", PYTHONPATH: REPO,
                TARES_TRIGGER_DEBOUNCE_SECONDS: "0", TARES_SCHEDULE_CLOCK_SECONDS: "3600", TARES_WATCH_SECONDS: "3600",
                // a made-up key: the cell counts a service as watched only when the watcher has a model; the
                // clocks above are an hour long, so no call is ever made with it
                ANTHROPIC_API_KEY: "sk-ant-test-not-a-real-key" };
  for (const k of ["TARES_AUTH_TOKEN", "TARES_DB", "TARES_CATALOG", "TARESD_URL", "OPENAI_API_KEY",
                   "TARES_SLACK_BOT_TOKEN", "TARES_SLACK_SIGNING_SECRET"]) delete env[k];
  const daemon = spawn(tares, ["up", "--port", String(PORT), "--data-dir", home], { cwd: REPO, stdio: "ignore", env });
  let browser;
  try {
    ck("daemon up", await waitFor(`${BASE}/health`));
    browser = await chromium.launch(process.env.TARES_E2E_CHROME ? { executablePath: process.env.TARES_E2E_CHROME } : {});
    const newPage = async (width, sampleFlag = false) => {
      const ctx = await browser.newContext({ viewport: { width, height: width < 600 ? 844 : 900 } });
      if (sampleFlag) await ctx.addInitScript(() => { try { localStorage.setItem("taresSamples", "1"); } catch (e) { /* none */ } });
      return { ctx, page: await ctx.newPage() };
    };
    const shot = async (page, n) => { await page.screenshot({ path: path.join(SHOTS, n), fullPage: true }); };
    const dark = async (page, n) => {
      await page.evaluate(() => document.documentElement.setAttribute("data-theme", "dark"));
      await page.waitForTimeout(600); // the controls fade between themes
      await shot(page, n);
      await page.evaluate(() => document.documentElement.setAttribute("data-theme", "light"));
      await page.waitForTimeout(300);
    };
    // the console's own stylesheet gives a list in the page bullets and an indent; the screens take both off
    const bare = (page) => page.evaluate(() => [...document.querySelectorAll("main ul, .main ul")]
      .every((u) => getComputedStyle(u).listStyleType === "none" && parseFloat(getComputedStyle(u).paddingLeft) === 0));
    const overflow = (page) => page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    const settle = async (page) => { await page.waitForLoadState("networkidle"); await page.waitForTimeout(150); };
    const text = (loc) => loc.innerText().then((t) => t.replace(/\s+/g, " ").trim());

    const list0 = await getJson(LIST);
    const total = list0.services.length;
    ck("the cell has more services than a page", total > PAGE_SIZE, String(total));

    // ── the list, live ───────────────────────────────────────────────────────
    const { ctx, page } = await newPage(1440);
    const rows = page.getByTestId("service-row");
    const names = () => page.getByTestId("service-name").allInnerTexts().then((a) => a.map((t) => t.trim()));
    const row = (n) => page.locator(`[data-testid=service-row][data-name="${n}"]`);
    const reads = [];
    page.on("request", (r) => { if (r.method() === "GET" && new URL(r.url()).pathname.startsWith("/api/") && new URL(r.url()).pathname !== "/api/licence") reads.push(r.url().replace(BASE, "")); });

    const one = expectOneCall(page, LIST);
    await page.goto(`${BASE}/services`, { waitUntil: "load" });
    await rows.first().waitFor();
    await one(ck, "/services");
    ck("the page has its heading", await page.getByRole("heading", { name: "Services", exact: true }).isVisible());
    ck("a page of rows, in the order the cell gives", JSON.stringify(await names()) === JSON.stringify(list0.services.slice(0, PAGE_SIZE).map((s) => s.name)), (await names()).join(","));
    ck("Showing N of M uses the rows shown", (await text(page.getByTestId("services-showing"))) === `Showing ${PAGE_SIZE} of ${total}`, await text(page.getByTestId("services-showing")));
    const first = list0.services[0];
    const firstText = await text(rows.first());
    ck("a row says the state, the events and when it was checked",
       firstText.includes(first.state_words) && firstText.includes(first.events_24h_words) && firstText.includes(first.last_words[0].toUpperCase() + first.last_words.slice(1)), firstText);
    ck("a row has a sparkline and a link to its page", (await rows.first().locator("svg").count()) >= 1 && (await rows.first().locator("a").getAttribute("href")) === `/services/${encodeURIComponent(first.name)}`);
    ck("names are marked as the person's own text", (await rows.first().locator("[data-testid=service-name][data-user-text]").count()) === 1);
    ck("no hidden services yet, so no Show hidden", (await page.getByTestId("toggle-hidden").count()) === 0 && list0.hidden_count === 0);
    await page.getByTestId("show-more").click();
    ck("Show more adds a page", (await rows.count()) === Math.min(total, PAGE_SIZE * 2) && (await text(page.getByTestId("services-showing"))) === `Showing ${Math.min(total, PAGE_SIZE * 2)} of ${total}`);
    await checkWords(page, ck, "live list");
    ck("the list has no bullets or indent", await bare(page));
    await shot(page, "live-list-1440.png");
    await dark(page, "live-list-dark-1440.png");

    async function showAll() { while (await page.getByTestId("show-more").count()) await page.getByTestId("show-more").click(); }
    // ── a service page, live ─────────────────────────────────────────────────
    const svc = page.getByTestId("sre-service");
    const putBodies = [];
    page.on("request", (r) => { if (r.method() === "PUT") putBodies.push({ url: new URL(r.url()).pathname, body: JSON.parse(r.postData() || "{}") }); });
    const edge = await getJson("/api/sre/services/edge-proxy");
    const oneEdge = expectOneCall(page, "/api/sre/services/edge-proxy");
    await row("edge-proxy").click();
    await page.getByTestId("service-title").waitFor();
    await oneEdge(ck, "edge-proxy");
    ck("the url is the service's own", new URL(page.url()).pathname === "/services/edge-proxy");
    ck("the title is the name", (await text(page.getByTestId("service-title"))) === "edge-proxy");
    ck("the state is in words", (await text(page.getByTestId("service-state"))) === edge.state_words, await text(page.getByTestId("service-state")));
    ck("the facts say events, watching, last check", (await text(page.getByTestId("service-facts"))).includes(edge.events_24h_words) && /Checked|checked/.test(await text(page.getByTestId("service-facts"))));
    ck("Last day is the chosen window", (await page.getByRole("tab", { name: "Last day" }).getAttribute("aria-selected")) === "true");
    const chart = page.getByTestId("service-chart");
    ck("the chart has errors and traffic on one axis", (await chart.locator("svg path").count()) === 2 && (await chart.locator("[data-y-axis]").count()) === 1);
    ck("the chart has a dashed mark for a deploy", (await chart.locator("svg [data-mark]").count()) >= 1, String(await chart.locator("svg [data-mark]").count()));
    const marks = chart.locator("[data-marks-list] li");
    ck("and the deploys are listed with a time and a label", (await marks.count()) >= 1 && (await marks.first().locator("time").count()) === 1 && (await text(marks.first())).length > 12, await text(marks.first()));
    ck("deploys on the page equal deploys from the cell", (await marks.count()) === edge.chart.deploys.length, `${await marks.count()} vs ${edge.chart.deploys.length}`);
    ck("Deploys from lists the linked repo", (await text(page.getByTestId("service-repos"))).includes("example-org/service-a") && (await text(page.getByTestId("service-repos"))).includes("Linked in settings"), await text(page.getByTestId("service-repos")));
    ck("Open the timeline links to the service", (await page.getByTestId("open-timeline").getAttribute("href")) === "/timeline?service=edge-proxy");
    ck("Incidents and Raised about it have their sections", (await page.getByTestId("service-incidents").count()) === 1 && (await page.getByTestId("service-raised").count()) === 1);
    ck("Blind spots says what it found", (await text(page.getByTestId("service-blind-spots"))).length > 0);
    await checkWords(page, ck, "live service");
    ck("the service page has no bullets or indent", await bare(page));
    await shot(page, "live-service-1440.png");
    await dark(page, "live-service-dark-1440.png");

    const win = page.waitForRequest((r) => r.url().includes("/api/sre/services/edge-proxy?window=1h"));
    await page.getByRole("tab", { name: "Last hour" }).click();
    await win;
    await page.locator("[data-testid=service-chart][data-window='1h']").waitFor();
    ck("Last hour asks the cell for that window", (await page.getByRole("tab", { name: "Last hour" }).getAttribute("aria-selected")) === "true");
    await page.getByRole("tab", { name: "Last week" }).click();
    await page.locator("[data-testid=service-chart][data-window='7d']").waitFor();
    ck("Last week draws that window", (await page.getByTestId("service-chart").getAttribute("data-window")) === "7d");

    // ── rename it ────────────────────────────────────────────────────────────
    await page.getByTestId("setting-name").fill("Front door");
    await page.getByTestId("settings-save").click();
    await page.getByTestId("settings-saved").waitFor();
    ck("Save sends only the name", putBodies.length === 1 && JSON.stringify(putBodies[0].body) === JSON.stringify({ display_name: "Front door" }) && putBodies[0].url === "/api/sre/services/edge-proxy/settings", JSON.stringify(putBodies));
    ck("the page shows the new name, and the real one under it", (await text(page.getByTestId("service-title"))) === "Front door" && (await text(page.getByTestId("service-real-name"))) === "edge-proxy");
    ck("the new name is marked as the person's own text", (await page.locator("[data-testid=service-title][data-user-text]").count()) === 1);
    ck("the answer is the day's page", (await page.getByTestId("service-chart").getAttribute("data-window")) === "24h");
    await checkWords(page, ck, "service renamed");
    await page.goto(`${BASE}/services`, { waitUntil: "load" });
    await rows.first().waitFor();
    await showAll();
    ck("the list shows the new name next to the real one", (await text(row("edge-proxy"))).includes("Front door") && (await text(row("edge-proxy").getByTestId("service-name"))) === "edge-proxy");

    // ── hide one ─────────────────────────────────────────────────────────────
    await page.goto(`${BASE}/services/service-b`, { waitUntil: "load" });
    await page.getByTestId("service-title").waitFor();
    putBodies.length = 0;
    await page.getByTestId("setting-hidden").click();
    await page.getByTestId("settings-save").click();
    await page.getByTestId("settings-saved").waitFor();
    ck("Hide sends hidden", putBodies.length === 1 && JSON.stringify(putBodies[0].body) === JSON.stringify({ hidden: true }), JSON.stringify(putBodies));
    ck("the page says it is hidden", (await page.getByTestId("service-hidden").count()) === 1);
    await page.goto(`${BASE}/services`, { waitUntil: "load" });
    await rows.first().waitFor();
    ck("the list has Show hidden (1)", (await text(page.getByTestId("toggle-hidden"))) === "Show hidden (1)", await text(page.getByTestId("toggle-hidden")));
    ck("and the hidden one is not in it", (await row("service-b").count()) === 0 && (await text(page.getByTestId("services-showing"))) === `Showing ${PAGE_SIZE} of ${total - 1}`, await text(page.getByTestId("services-showing")));
    const readHidden = page.waitForRequest((r) => r.url().endsWith(`${LIST}?hidden=1`));
    await page.getByTestId("toggle-hidden").click();
    await readHidden;
    await page.getByTestId("services-showing").waitFor();
    await settle(page);
    ck("Show hidden asks for them", (await text(page.getByTestId("services-showing"))) === `Showing ${PAGE_SIZE} of ${total}`, await text(page.getByTestId("services-showing")));
    await page.getByTestId("show-more").click();
    ck("the hidden one is marked Hidden", (await row("service-b").count()) === 1 && (await text(row("service-b"))).includes("Hidden"));
    await checkWords(page, ck, "list with hidden");
    await shot(page, "live-list-hidden-1440.png");

    // ── a silent service opens on the last week ─────────────────────────────
    reads.length = 0;
    await page.goto(`${BASE}/services/service-a-receiver`, { waitUntil: "load" });
    await page.locator("[data-testid=service-chart][data-window='7d']").waitFor();
    await settle(page);
    ck("a silent service opens on Last week", (await page.getByRole("tab", { name: "Last week" }).getAttribute("aria-selected")) === "true" && (await page.getByTestId("sre-service").getAttribute("data-state")) === "silent");
    await checkWords(page, ck, "live silent service");
    await shot(page, "live-service-silent-1440.png");

    // ── join one into another: asks first, has no undo ──────────────────────
    await page.goto(`${BASE}/services/extra-svc-22`, { waitUntil: "load" });
    await page.getByTestId("service-title").waitFor();
    putBodies.length = 0;
    ck("Join is off until a service is chosen", await page.getByTestId("merge-open").isDisabled());
    await page.getByTestId("setting-merge-select").click();
    await page.getByRole("option", { name: "extra-svc-21", exact: true }).click();
    await page.getByTestId("merge-open").click();
    const dialog = page.getByRole("dialog");
    await dialog.waitFor();
    ck("the dialog names the service and says there is no undo", (await text(dialog)).includes("Join into extra-svc-21?") && /no undo/.test(await text(dialog)), await text(dialog));
    await checkWords(page, ck, "join dialog");
    await shot(page, "live-join-dialog-1440.png");
    await dialog.getByRole("button", { name: "Cancel" }).click();
    await dialog.waitFor({ state: "hidden" });
    ck("Cancel joins nothing", putBodies.length === 0);
    await page.getByTestId("merge-open").click();
    await page.getByTestId("merge-confirm").click();
    await page.waitForURL("**/services/extra-svc-21");
    await page.getByTestId("service-title").waitFor();
    ck("Join sends the one change and opens the other service", putBodies.length === 1 && JSON.stringify(putBodies[0].body) === JSON.stringify({ merged_into: "extra-svc-21" }) && (await text(page.getByTestId("service-title"))) === "extra-svc-21", JSON.stringify(putBodies));
    ck("and it says what is counted under it", (await text(page.getByTestId("merged-names"))).includes("extra-svc-22"));
    await page.goto(`${BASE}/services`, { waitUntil: "load" });
    await rows.first().waitFor();
    ck("the list names what is shown as part of another service", /extra-svc-22 is shown as part of /.test(await text(page.getByTestId("services-merged"))), await text(page.getByTestId("services-merged")));

    // ── a name the cell does not know ───────────────────────────────────────
    await page.goto(`${BASE}/services/nope`, { waitUntil: "load" });
    await page.getByTestId("service-missing").waitFor();
    ck("an unknown name says so, with a way back", (await page.getByRole("link", { name: "Back to Services" }).getAttribute("href")) === "/services");
    await checkWords(page, ck, "live missing");
    await ctx.close();

    // ── 390 px, live ─────────────────────────────────────────────────────────
    for (const [label, route, wait] of [["list", "/services", "service-row"], ["service", "/services/edge-proxy", "service-title"], ["silent service", "/services/service-a-receiver", "service-title"]]) {
      const { ctx: c, page: p } = await newPage(390);
      await p.goto(`${BASE}${route}`, { waitUntil: "load" });
      await p.getByTestId(wait).first().waitFor();
      await settle(p);
      ck(`390: the ${label} does not scroll sideways`, (await overflow(p)) <= 0, String(await overflow(p)));
      await checkWords(p, ck, `390 ${label}`);
      ck(`390: the ${label} has no bullets or indent`, await bare(p));
      await shot(p, `live-${label.replace(" ", "-")}-390.png`);
      await dark(p, `live-${label.replace(" ", "-")}-dark-390.png`);
      await c.close();
    }

    // ── the sample states ────────────────────────────────────────────────────
    const waitList = { load: "services-loading", error: "services-error", empty: "services-empty" };
    for (const width of [1440, 390]) {
      for (const name of LIST_SAMPLES) {
        const { ctx: c, page: p } = await newPage(width, true);
        await p.goto(`${BASE}/services?sample=${name}`, { waitUntil: "load" });
        await p.getByTestId(waitList[name] || "service-row").first().waitFor();
        await settle(p);
        await checkWords(p, ck, `list ${name} ${width}`);
        ck(`list ${name} ${width}: does not scroll sideways`, (await overflow(p)) <= 0, String(await overflow(p)));
        if (width === 1440) {
          if (name === "normal") {
            ck("list normal: the rows", (await p.getByTestId("service-row").count()) === 8);
            ck("list normal: Show hidden (1) and the merged note", (await text(p.getByTestId("toggle-hidden"))) === "Show hidden (1)" && (await text(p.getByTestId("services-merged"))) === "service-a-receiver and service-a-worker are shown as part of service-a");
            await p.getByTestId("toggle-hidden").click();
            await p.locator("[data-testid=service-row][data-name=service-b]").waitFor();
            ck("list normal: Show hidden adds the hidden one, marked", (await p.getByTestId("service-row").count()) === 9 && (await text(p.locator("[data-testid=service-row][data-name=service-b]"))).includes("Hidden"));
            ck("list normal: the toggle then offers to hide them again", (await text(p.getByTestId("toggle-hidden"))) === "Hide hidden");
          } else if (name === "empty") {
            ck("list empty: says so, with a link to connect logs", (await text(p.getByTestId("services-empty"))).includes("No services yet") && (await p.getByTestId("connect-link").getAttribute("href")) === "/settings#tools" && (await text(p.getByTestId("connect-link"))) === "Connect where your logs come from");
          } else if (name === "error") {
            ck("list error: a Retry", (await p.getByRole("button", { name: "Retry" }).count()) === 1 && (await p.getByTestId("service-row").count()) === 0);
          } else if (name === "many") {
            ck("list many: a page, then Showing N of M", (await p.getByTestId("service-row").count()) === PAGE_SIZE && (await text(p.getByTestId("services-showing"))) === `Showing ${PAGE_SIZE} of 60`);
            await p.getByTestId("show-more").click();
            ck("list many: Show more adds a page", (await p.getByTestId("service-row").count()) === PAGE_SIZE * 2 && (await text(p.getByTestId("services-showing"))) === `Showing ${PAGE_SIZE * 2} of 60`);
          } else if (name === "long") {
            ck("list long: the whole long name is on the page", (await text(p.getByTestId("service-name").first())).length === 73 - 3);
          } else if (name === "hidden") {
            ck("list hidden: opens with the hidden ones", (await p.locator("[data-testid=service-row][data-name=service-b]").count()) === 1);
          }
        }
        const shotName = { load: "loading", error: "error" }[name] || name;
        await shot(p, `list-${shotName}-${width}.png`);
        await c.close();
      }
      for (const name of SERVICE_SAMPLES) {
        const { ctx: c, page: p } = await newPage(width, true);
        await p.goto(`${BASE}/services/service-a?sample=${name}`, { waitUntil: "load" });
        const gate = { load: "service-loading", error: "service-error", missing: "service-missing" }[name] || "service-title";
        await p.getByTestId(gate).waitFor();
        if (name === "silent") await p.locator("[data-testid=service-chart][data-window='7d']").waitFor();
        await settle(p);
        if (width === 1440) {
          if (name === "normal") {
            ck("service normal: a deploy mark and the deploy list", (await p.locator("[data-testid=service-chart] svg [data-mark]").count()) === 2 && (await p.locator("[data-marks-list] li").count()) === 2);
            ck("service normal: 1 open, 1 closed, as the rows listed", (await text(p.getByTestId("service-incidents"))).includes("1 open, 1 closed") && (await p.getByTestId("incident-row").count()) === 2 && (await p.getByTestId("incidents-showing").count()) === 0);
            ck("service normal: worth is text, not a control", (await p.getByTestId("raised-row").count()) === 3 && (await p.getByTestId("raised-worth").first().evaluate((e) => e.closest("a,button") === null && e.tagName === "SPAN")));
            ck("service normal: a blind spot with its action", (await p.getByTestId("blind-spot-action").getAttribute("href")) === "/settings#tools");
            ck("service normal: Matched by name", (await text(p.getByTestId("service-repos"))).includes("Matched by name"));
          } else if (name === "incidents") {
            ck("service incidents: 10 shown of 15, the counts of all", (await p.getByTestId("incident-row").count()) === 10 && (await text(p.getByTestId("incidents-showing"))) === "Showing 10 of 15" && (await text(p.getByTestId("service-incidents"))).includes("4 open, 11 closed"));
            ck("service incidents: 8 raised shown of 14", (await p.getByTestId("raised-row").count()) === 8 && (await text(p.getByTestId("raised-showing"))) === "Showing 8 of 14");
          } else if (name === "long") {
            ck("service long: the display name is the title and the real name is under it", (await p.locator("[data-testid=service-title][data-user-text]").count()) === 1 && (await text(p.getByTestId("service-real-name"))).length === 70);
          } else if (name === "silent") {
            ck("service silent: opens on Last week", (await p.getByRole("tab", { name: "Last week" }).getAttribute("aria-selected")) === "true");
          } else if (name === "quiet") {
            ck("service quiet: Not checked yet, not No blind spots", (await text(p.getByTestId("blind-spots-words"))) === "Not checked yet");
          } else if (name === "error") {
            ck("service error: a Retry", (await p.getByRole("button", { name: "Retry" }).count()) === 1);
          } else if (name === "missing") {
            ck("service missing: says no service has the name", (await text(p.getByTestId("service-missing"))).includes("No service with this name"));
          } else if (name === "readonly") {
            await p.getByTestId("setting-name").fill("A name");
            await p.getByTestId("settings-save").click();
            await p.getByTestId("settings-readonly").waitFor();
            ck("service readonly: a notice, and the controls are off", await p.getByTestId("setting-name").isDisabled() && await p.getByTestId("settings-save").isDisabled() && await p.getByTestId("setting-hidden").isDisabled());
            await checkWords(p, ck, `service readonly notice ${width}`);
          } else if (name === "savefail") {
            await p.getByTestId("setting-name").fill("A name");
            await p.getByTestId("settings-save").click();
            await p.getByTestId("settings-failed").waitFor();
            ck("service savefail: says it was not saved, and Retry stays", (await p.getByTestId("settings-failed").getByRole("button", { name: "Retry" }).count()) === 1 && (await p.getByTestId("setting-name").inputValue()) === "A name");
            await checkWords(p, ck, `service savefail notice ${width}`);
          } else if (name === "join") {
            await p.getByTestId("setting-merge-select").click();
            await p.getByRole("option", { name: "edge-proxy", exact: true }).click();
            await p.getByTestId("merge-open").click();
            await p.getByRole("dialog").waitFor();
            await p.getByTestId("merge-confirm").click();
            await p.getByRole("dialog").waitFor({ state: "hidden" });
            ck("service join: shows the service it joined", (await text(p.getByTestId("service-title"))) === "edge-proxy");
          }
        }
        await checkWords(p, ck, `service ${name} ${width}`);
        ck(`service ${name} ${width}: does not scroll sideways`, (await overflow(p)) <= 0, String(await overflow(p)));
        const shotName = { load: "loading", error: "error" }[name] || name;
        await shot(p, `service-${shotName}-${width}.png`);
        await c.close();
      }
    }

    // the address alone shows no sample on a live cell
    {
      const { ctx: c, page: p } = await newPage(1440);
      await p.goto(`${BASE}/services?sample=empty`, { waitUntil: "load" });
      await p.getByTestId("service-row").first().waitFor();
      ck("the sample address alone shows the live cell", (await p.getByTestId("services-empty").count()) === 0);
      await c.close();
    }
  } finally {
    if (browser) await browser.close();
    daemon.kill();
    fs.rmSync(home, { recursive: true, force: true });
  }
  console.log(`\n${pass} passed, ${fail} failed`);
  console.log(`screenshots: ${SHOTS}`);
  process.exit(fail ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(1); });
