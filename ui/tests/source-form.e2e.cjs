/*
 * Browser e2e for the connection form (TR-1394): the kit form a person fills to connect Tares to a system,
 * on /sources/new for each connector and on a connection's Configuration tab. Discover, test and the GitHub
 * token calls are mocked at the network, so each connector's flow runs without the system behind it; the
 * save is captured and its body checked. The page around the form is still legacy, so the words check reads
 * the form's own text.
 * Run:  npm run build && node tests/source-form.e2e.cjs     (from ui/, tares installed in ../.venv)
 * Set SHOTS_DIR to keep screenshots.
 */
const path = require("path");
const { chromium } = require(path.join(path.resolve(__dirname, ".."), "node_modules", "playwright"));
const { startDaemon } = require("./licensed.cjs");
const { offendersIn } = require("./words.cjs");
const { kindButton } = require("./source-new.fixture.cjs");

const PORT = Number(process.env.TARES_E2E_PORT || 9964);
const SHOTS = process.env.SHOTS_DIR;
let pass = 0, fail = 0;
const ck = (label, cond, detail = "") => { cond ? pass++ : fail++; console.log((cond ? "  ok   " : "  FAIL ") + label + (cond ? "" : `   ${detail}`)); };
const json = (route, status, body) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });

// The form's own text, minus what a person typed or a system returned.
const FORM_TEXT = () => {
  const form = document.querySelector("[data-testid=connection-form]");
  const style = document.createElement("style");
  style.textContent = "[data-words-exempt],[data-user-text]{display:none !important}";
  document.head.appendChild(style);
  try { return form ? form.innerText : ""; } finally { style.remove(); }
};
async function formWords(page, label) {
  const found = offendersIn(await page.evaluate(FORM_TEXT));
  ck(`${label}: no internal names in the form`, !found.filter((w) => w !== "em dash").length, found.join(", "));
  ck(`${label}: no em dashes in the form`, !found.includes("em dash"), found.join(", "));
}
const noSideways = async (page) => page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);

(async () => {
  const d = await startDaemon({ port: PORT, edition: "factory", licence: "valid" });
  process.on("exit", () => d.stop());
  const browser = await chromium.launch(process.env.TARES_E2E_CHROME ? { executablePath: process.env.TARES_E2E_CHROME } : {});

  // A page with every system call mocked. `calls` records each discover, test and save body.
  const open = async (connector, { width = 1440, dark = false } = {}) => {
    const ctx = await browser.newContext({ viewport: { width, height: width < 600 ? 844 : 900 }, ...(dark ? { colorScheme: "dark" } : {}) });
    const page = await ctx.newPage();
    const calls = { discover: [], test: [], save: [], env: 0 };
    await page.route("**/api/capabilities", async (route) => {
      const r = await route.fetch();
      json(route, 200, { ...(await r.json()), discover_docker: true });
    });
    await page.route("**/api/sources/discover", (route) => {
      const body = JSON.parse(route.request().postData() || "{}");
      calls.discover.push(body);
      return json(route, 200, DISCOVER[body.connector](body.config));
    });
    await page.route("**/api/sources/test", (route) => {
      calls.test.push(JSON.parse(route.request().postData() || "{}"));
      return json(route, 200, calls.test.length === 1
        ? { ok: true, events: 3, sample: ['{"level":"error","msg":"disk full"}', '{"level":"warn","msg":"slow reply"}'] }
        : { ok: false, error: "connection refused by 10.0.0.4" });
    });
    await page.route("**/api/sources", (route) => {
      if (route.request().method() !== "POST") return route.fallback();
      const body = JSON.parse(route.request().postData() || "{}");
      calls.save.push(body);
      return json(route, 200, { ok: true, name: body.name, ingest_key: null });
    });
    await page.route("**/api/discover/environment*", (route) => { calls.env++; return json(route, 200, {
      provider: "docker", summary: { containers: 2, proposed: 0 }, proposed_sources: [], skipped: [],
      containers: [
        { name: "shop-api-1", image: "ghcr.io/acme/api:1.4", ports: "8080", project: "shop", service: "api" },
        { name: "shop-db-1", image: "postgres:16", ports: "5432", project: "shop", service: "db" }] }); });
    await page.route("**/api/integrations/github", (route) => route.request().method() !== "GET" ? route.fallback() : json(route, 200, {
      credentials: [{ name: "team-gh", kind: "token", api_url: "", account: "octo", token_configured: true, created_at: "", updated_at: "", sources: [] }] }));
    await page.route("**/api/integrations/github/team-gh/test", (route) => json(route, 200, { ok: true, login: "octo", scopes: [] }));
    await page.route("**/api/integrations/github/team-gh/repos*", (route) => json(route, 200, { repos: [
      { full_name: "acme/shop", default_branch: "main", private: true, pushed_at: null },
      { full_name: "acme/docs", default_branch: "trunk", private: false, pushed_at: null }] }));
    if (connector) {
      await page.goto(`${d.base}/sources/new`, { waitUntil: "load" });
      await kindButton(page, connector).click({ timeout: 15000 });
      await page.getByTestId("connection-form").waitFor({ timeout: 15000 });
    }
    return { page, calls, close: () => ctx.close() };
  };
  const pick = async (page, label, option, scope = page) => {
    await scope.getByRole("combobox", { name: label }).click();
    await page.getByRole("option", { name: option, exact: true }).click();
  };
  const shot = async (page, name) => { if (SHOTS) await page.screenshot({ path: path.join(SHOTS, `${name}.png`), fullPage: true }); };

  try {
    // ── Loki: required fields, the poll interval, testing and saving ─────────────────────────────
    {
      const { page, calls, close } = await open("loki");
      ck("loki: every field has a visible label in words", await page.getByLabel("Bearer token").count() === 1 && await page.getByLabel("Tenant ID").count() === 1);
      await page.getByRole("button", { name: "Create connection" }).click();
      await page.getByTestId("form-error").waitFor({ timeout: 5000 });
      ck("a missing required field is named as a sentence", (await page.getByTestId("form-error").innerText()).includes("URL is required."), await page.getByTestId("form-error").innerText());
      ck("the missing field turns red and says so", await page.getByTestId("field-url").evaluate((e) => e.classList.contains("missing"))
        && (await page.getByTestId("field-url").innerText()).includes("Fill this in."));
      ck("the missing field is invalid to a screen reader", (await page.getByLabel("URL (required)").getAttribute("aria-invalid")) === "true");
      await page.getByLabel("URL (required)").fill("http://loki:3100");
      await page.getByLabel("Query (required)").fill('{app="shop"}');
      await page.getByLabel("Name (required)").fill("shop-logs");
      await page.getByLabel("Poll every").fill("2");
      await pick(page, "Unit", "Minutes");
      await page.getByRole("button", { name: "Test connection" }).click();
      await page.getByTestId("test-result").waitFor({ timeout: 5000 });
      ck("a test that works says how many events and shows them", (await page.getByTestId("test-result").innerText()).includes("The connection works. 3 events on a test poll.")
        && (await page.getByTestId("test-result").locator("pre").innerText()).includes("disk full"));
      ck("the test sends the poll interval as typed", calls.test[0]?.poll === "2m", JSON.stringify(calls.test[0]?.poll));
      await formWords(page, "loki");
      await shot(page, "loki-1440");
      await page.getByRole("button", { name: "Test connection" }).click();
      await page.getByText("The test failed:").waitFor({ timeout: 5000 });
      ck("a failed test says why, as a sentence", (await page.getByTestId("test-result").innerText()).includes("The test failed: Connection refused by 10.0.0.4."));
      await page.getByLabel("Name (required)").fill("");
      await page.getByRole("button", { name: "Create connection" }).click();
      await page.getByTestId("field-name").getByText("Fill this in.").waitFor({ timeout: 5000 });
      ck("a missing name is said under the name", (await page.getByLabel("Name (required)").getAttribute("aria-invalid")) === "true");
      ck("nothing is saved while a required field is empty", calls.save.length === 0);
      await page.getByLabel("Name (required)").fill("shop-logs");
      await page.getByRole("button", { name: "Create connection" }).click();
      await page.waitForTimeout(500);
      const saved = calls.save[0];
      ck("the save carries the connection as filled", saved?.name === "shop-logs" && saved?.poll === "2m" && saved?.config?.url === "http://loki:3100"
        && saved?.config?.query === '{app="shop"}' && !("bearer_token" in (saved?.config ?? {})), JSON.stringify(saved));
      await close();
    }

    // ── HTTP API: credentials, headers, Discover and the event mapping ───────────────────────────
    {
      const { page, calls, close } = await open("http_poll");
      ck("http: before Discover the mapping says what will fill it", await page.getByTestId("http-mapping-empty").isVisible());
      ck("http: no header fields until the API wants a credential", await page.getByLabel("Header value").count() === 0);
      await page.getByLabel("URL (required)").fill("https://status.example.test/api/checks");
      await pick(page, "Credentials", "A key or a token");
      await page.getByLabel("Header name").fill("X-API-Key");
      await page.getByLabel("Header value").fill("k-123");
      await page.getByTestId("field-headers").getByRole("button", { name: "Add header" }).click();
      await page.getByLabel("Header name", { exact: true }).nth(1).waitFor({ timeout: 3000 }).catch(() => {});
      await page.getByTestId("kv-row").getByLabel("Header name").fill("Accept");
      await page.getByTestId("kv-row").getByLabel("Value").fill("application/json");
      // a row added and left blank costs nothing: it is not sent as a header named ""
      await page.getByTestId("field-headers").getByRole("button", { name: "Add header" }).click();
      await page.getByRole("button", { name: "Discover", exact: true }).click();
      await page.getByTestId("sample-reply").waitFor({ timeout: 5000 });
      const sent = calls.discover[0]?.config ?? {};
      ck("Discover sends the credential and the extra headers", sent.credential === "k-123" && sent.credential_header === "X-API-Key" && sent.headers?.Accept === "application/json", JSON.stringify(sent));
      ck("a blank header row is left out", JSON.stringify(Object.keys(sent.headers ?? {})) === '["Accept"]', JSON.stringify(sent.headers));
      ck("the sample reply is shown, its fields counted", (await page.getByTestId("sample-reply").locator("summary").first().innerText()).includes("What Tares fetched (4 fields)"));
      ck("the payload key is filled and explained", (await page.getByLabel("Payload key").inputValue()) === "checks"
        && (await page.getByText("Discover found a list at checks, 2 entries, one event each.").count()) === 1);
      ck("the summary previews the first entry", (await page.getByTestId("summary-preview").innerText()).includes("Reads as: checkout is down"));
      await page.getByLabel("Event summary").fill("{name} reads {status}");
      ck("the preview follows what is typed", (await page.getByTestId("summary-preview").innerText()).includes("checkout reads down"));
      ck("a fixed kind of event takes a type", await page.getByLabel("Event type").isVisible());
      await pick(page, "Kind of event", "status");
      ck("a kind from a field shows an example instead", (await page.getByTestId("event-kind").innerText()).includes("For example down."));
      ck("Discover's summary reads as the test result", (await page.getByTestId("test-result").innerText()).includes("The connection works. Found a list at checks."));
      await formWords(page, "http");
      await shot(page, "http-1440");
      await close();
    }

    // ── Postgres: tables, then columns, then a summary of the connection ─────────────────────────
    {
      const { page, calls, close } = await open("postgres");
      await page.getByLabel("Connection URL (required)").fill("postgresql://reader:pw@db.example.test:5432/shop");
      await page.getByRole("button", { name: "Discover", exact: true }).click();
      await page.getByTestId("pg-tables").waitFor({ timeout: 5000 });
      ck("postgres: Discover lists the tables to pick from", (await page.getByTestId("pg-tables").locator("tbody tr").count()) === 2);
      await page.getByRole("button", { name: "Use public.orders" }).click();
      await page.getByTestId("columns-proposal").waitFor({ timeout: 5000 });
      ck("picking a table asks for its columns", calls.discover[1]?.config?.table === "public.orders", JSON.stringify(calls.discover[1]));
      ck("the connection collapses to where it points", (await page.getByTestId("conn-summary").innerText()).includes("Connected to shop, db.example.test."));
      ck("the name is proposed from the table", (await page.getByLabel("Name (required)").inputValue()) === "orders-table");
      await page.getByRole("button", { name: "Apply to form" }).click();
      ck("applied, the proposal and the Discover panel close", (await page.getByTestId("columns-proposal").count()) === 0 && (await page.getByTestId("discover-panel").count()) === 0);
      ck("applying fills the cursor column", (await page.getByRole("combobox", { name: /How we find new rows/ }).innerText()).includes("id"));
      ck("the poll section names the table", (await page.getByTestId("pg-poll").innerText()).includes("What Tares reads from public.orders"));
      await formWords(page, "postgres");
      await shot(page, "postgres-1440");
      await page.getByRole("button", { name: "Edit connection" }).click();
      ck("Edit connection opens the URL again", await page.getByLabel("Connection URL (required)").isVisible());
      await close();
    }

    // ── Prometheus: the metric basket, then the chosen metrics ───────────────────────────────────
    {
      const { page, calls, close } = await open("prometheus");
      ck("prometheus: no metrics settings before Discover", await page.getByRole("button", { name: "Create connection" }).count() === 0);
      await page.getByLabel("URL (required)").fill("http://prom:9090");
      await page.getByRole("button", { name: "Discover", exact: true }).click();
      await page.getByTestId("metric-basket").waitFor({ timeout: 5000 });
      await page.getByLabel("Metric name or prefix").fill("node_*");
      await page.getByRole("button", { name: "Add all 2" }).click();
      ck("adding a prefix fills the basket", (await page.getByTestId("basket-count").innerText()) === "2 metrics chosen");
      await page.getByRole("tab", { name: "By label" }).click();
      await page.getByTestId("metric-labels").getByRole("button", { name: "job" }).click();
      await page.getByTestId("metric-results").getByRole("checkbox", { name: "up", exact: true }).waitFor({ timeout: 5000 });
      ck("a label lists the metrics that carry it", calls.discover.some((c) => c.config?.for_label === "job") && (await page.getByTestId("metric-results").getByRole("checkbox", { name: "up", exact: true }).count()) === 1);
      await formWords(page, "prometheus basket");
      await shot(page, "prometheus-basket-1440");
      await page.getByRole("button", { name: "Use these 2 metrics" }).click();
      await page.getByTestId("metrics-summary").waitFor({ timeout: 5000 });
      const fin = calls.discover.find((c) => c.config?.selected);
      ck("confirming sends the chosen metrics", JSON.stringify(fin?.config?.selected?.sort()) === JSON.stringify(["node_cpu_seconds_total", "node_load1"]), JSON.stringify(fin));
      ck("the basket collapses to a count", (await page.getByTestId("metrics-summary").innerText()).includes("2 metrics chosen."));
      ck("the queries list what will be fetched", (await page.getByTestId("prom-queries").innerText()).includes("node_load1"));
      await formWords(page, "prometheus chosen");
      await page.getByRole("button", { name: "Change metrics" }).click();
      ck("Change metrics opens the basket again", await page.getByTestId("metric-basket").isVisible());
      await close();
    }

    // ── Prometheus alerts: severity and pending, then a one-line summary ─────────────────────────
    {
      const { page, close } = await open("prometheus_alerts");
      await page.getByLabel("URL (required)").fill("http://prom:9090");
      await page.getByRole("button", { name: "Discover", exact: true }).click();
      await page.getByTestId("alert-curation").waitFor({ timeout: 5000 });
      await page.getByRole("button", { name: "critical", exact: true }).click();
      ck("alerts: a severity filters the rules", (await page.getByText("1 of 3 rules shown").count()) === 1);
      await page.getByRole("checkbox", { name: "Also receive pending alerts" }).click();
      await formWords(page, "alerts");
      await shot(page, "alerts-1440");
      await page.getByRole("button", { name: "Receive critical alerts" }).click();
      ck("the filter collapses to a sentence", (await page.getByTestId("alerts-summary").innerText()).includes("Receiving critical alerts, pending ones included."));
      ck("the name is proposed", (await page.getByLabel("Name (required)").inputValue()) === "prometheus-alerts");
      await close();
    }

    // ── Docker logs: pick a container, its labels follow ─────────────────────────────────────────
    {
      const { page, calls, close } = await open("docker_logs");
      await page.getByRole("button", { name: "Discover containers" }).click();
      await page.getByTestId("containers").waitFor({ timeout: 5000 });
      ck("docker: the containers are listed", calls.env === 1 && (await page.getByTestId("containers").locator("tbody tr").count()) === 2);
      await page.getByRole("button", { name: "Use api" }).click();
      ck("picking a container fills the name and the container", (await page.getByLabel("Name (required)").inputValue()) === "api_logs"
        && (await page.getByLabel("Container (required)").inputValue()) === "shop-api-1");
      ck("its service becomes the key label", (await page.getByTestId("label-row").count()) === 2 && (await page.getByTestId("label-key-chip").count()) === 1);
      await formWords(page, "docker");
      await close();
    }

    // ── GitHub: a stored token, its repos, and adding a new one ──────────────────────────────────
    {
      const { page, close } = await open("github");
      await page.getByRole("combobox", { name: "Stored token" }).waitFor({ timeout: 5000 });
      await pick(page, "Stored token", "team-gh (octo)");
      await page.getByText("Pick one of the 2 repos the token can see, or type owner/name.").waitFor({ timeout: 5000 });
      ck("github: a stored token lists its repos", true);
      await page.getByRole("button", { name: "Test", exact: true }).click();
      await page.getByTestId("github-test").waitFor({ timeout: 5000 });
      ck("Test says who the token signs in as", (await page.getByTestId("github-test").innerText()).includes("Signed in as octo."));
      await formWords(page, "github stored");
      await shot(page, "github-1440");
      await page.getByRole("button", { name: "Add a new token" }).click();
      ck("a new token gets a password field", (await page.getByLabel("GitHub token").getAttribute("type")) === "password");
      await page.getByRole("button", { name: "Which permissions does the token need?" }).click();
      const dlg = page.getByRole("dialog", { name: "Token permissions" });
      await dlg.waitFor({ timeout: 3000 });
      ck("the permissions open in a dialog", (await dlg.innerText()).includes("Contents, Read-only"));
      await page.keyboard.press("Escape");
      await dlg.waitFor({ state: "hidden", timeout: 3000 });
      await page.getByRole("checkbox", { name: /Save it in this cell/ }).click();
      ck("saving it in the cell asks for a name", await page.getByLabel("Token name").isVisible());
      ck("Save token waits for a token and a name", await page.getByRole("button", { name: "Save token" }).isDisabled());
      await formWords(page, "github new");
      await page.getByRole("button", { name: "Cancel" }).click();
      ck("Cancel goes back to the stored tokens", await page.getByRole("combobox", { name: "Stored token" }).isVisible());
      await close();
    }

    // ── Reference: documents with labels ─────────────────────────────────────────────────────────
    {
      const { page, calls, close } = await open("reference");
      await page.getByLabel("Name (required)").fill("runbooks");
      await page.getByRole("button", { name: "Create connection" }).click();
      await page.getByTestId("form-error").waitFor({ timeout: 5000 });
      ck("reference: saving with no document says what is missing", (await page.getByTestId("form-error").innerText()).includes("Add at least one document."));
      await page.getByTestId("ref-upload").setInputFiles({ name: "runbook.md", mimeType: "text/markdown", buffer: Buffer.from("# Disk full\nClear /var/log.") });
      await page.getByTestId("ref-row").waitFor({ timeout: 5000 });
      ck("an uploaded document is listed with its size", (await page.getByTestId("ref-row").innerText()).includes("md, 27 characters"));
      await page.getByTestId("ref-row").getByRole("button", { name: "Add label" }).click();
      await page.getByTestId("ref-pair").getByRole("textbox", { name: "Label", exact: true }).fill("service");
      await page.getByTestId("ref-pair").getByRole("textbox", { name: "Value", exact: true }).fill("shop");
      await page.getByRole("button", { name: "Done" }).click();
      ck("a filled label reads as one chip", (await page.getByTestId("ref-row").getByText("service=shop").count()) === 1);
      await formWords(page, "reference");
      await shot(page, "reference-1440");
      await page.getByRole("button", { name: "Create connection" }).click();
      await page.waitForTimeout(500);
      const att = calls.save[0]?.config?.attachments?.[0];
      ck("the save carries the document and its label", att?.name === "runbook.md" && att?.format === "md" && JSON.stringify(att?.labels) === '{"service":"shop"}', JSON.stringify(calls.save[0]));
      await close();
    }

    // ── Editing a saved connection: a stored secret, kept or removed, and label changes ──────────
    {
      const r = await fetch(`${d.base}/api/sources`, { method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ name: "edge-logs", type: "logs", connector: "loki", poll: "30s",
          config: { url: "http://loki:3100", query: '{app="edge"}', username: "reader", password: "s3cret" } }) });
      ck("edit: a connection with a stored secret is created", r.ok, `${r.status} ${await r.text()}`);
      const { page, close } = await open(null);
      const puts = [];
      await page.route("**/api/sources/edge-logs", (route) => {
        if (route.request().method() !== "PUT") return route.fallback();
        puts.push(JSON.parse(route.request().postData() || "{}"));
        return json(route, 200, { ok: true });
      });
      await page.goto(`${d.base}/sources/edge-logs`, { waitUntil: "load" });
      await page.getByRole("tab", { name: "Configuration" }).click({ timeout: 15000 });
      await page.getByTestId("connection-form").waitFor({ timeout: 15000 });
      ck("the name is locked when editing", await page.getByLabel("Name (required)").isDisabled());
      const pw = page.getByLabel("Password", { exact: true });
      ck("a stored secret is never shown, only said to be set", (await pw.inputValue()) === "" && (await pw.getAttribute("placeholder")) === "Leave blank to keep");
      await formWords(page, "edit");
      await shot(page, "edit-1440");
      await page.getByRole("button", { name: "Save changes" }).click();
      await page.waitForTimeout(500);
      ck("left blank, the secret is kept (not sent)", puts[0] && !("password" in puts[0].config), JSON.stringify(puts[0]));
      await page.getByRole("tab", { name: "Configuration" }).click();
      await page.getByRole("button", { name: "Remove the stored value" }).click();
      ck("Remove says what saving will do", (await page.getByText("Removed when you save.").count()) === 1 && await page.getByLabel("Password", { exact: true }).isDisabled());
      await page.getByRole("button", { name: "Add label" }).click();
      await page.getByTestId("label-row").last().getByLabel("Label name").fill("team");
      await page.getByTestId("labels-changed").waitFor({ timeout: 3000 });
      ck("a label change warns that stored events keep theirs", (await page.getByTestId("labels-changed").innerText()).includes("Events already stored keep the labels they arrived with."));
      await page.getByRole("button", { name: "Save changes" }).click();
      await page.waitForTimeout(500);
      ck("removed, the secret is sent empty", puts[1]?.config?.password === "", JSON.stringify(puts[1]));
      await close();
    }

    // ── Dark and phone ───────────────────────────────────────────────────────────────────────────
    for (const [w, dark] of [[1440, true], [390, false]]) {
      const tag = `${w}${dark ? "-dark" : ""}`;
      const { page, close } = await open("http_poll", { width: w, dark });
      await page.getByLabel("URL (required)").fill("https://status.example.test/api/checks");
      await page.getByRole("button", { name: "Discover", exact: true }).click();
      await page.getByTestId("sample-reply").waitFor({ timeout: 5000 });
      if (w < 600) ck(`${tag}: no sideways scroll`, (await noSideways(page)) <= 0, `${await noSideways(page)}px`);
      await formWords(page, `http ${tag}`);
      await shot(page, `http-${tag}`);
      await close();
      const pg = await open("postgres", { width: w, dark });
      await pg.page.getByLabel("Connection URL (required)").fill("postgresql://reader:pw@db.example.test:5432/shop");
      await pg.page.getByRole("button", { name: "Discover", exact: true }).click();
      await pg.page.getByRole("button", { name: "Use public.orders" }).click();
      await pg.page.getByTestId("columns-proposal").waitFor({ timeout: 5000 });
      if (w < 600) ck(`${tag}: postgres, no sideways scroll`, (await noSideways(pg.page)) <= 0, `${await noSideways(pg.page)}px`);
      await shot(pg.page, `postgres-${tag}`);
      await pg.close();
    }
  } finally {
    await browser.close();
    d.stop();
  }
  console.log(`\n${pass} passed, ${fail} failed`);
  process.exit(fail ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(1); });

// What each connector's Discover returns, by what was asked.
const DISCOVER = {
  http_poll: (cfg) => ({
    connector: "http_poll", summary: "found a list at checks",
    proposed_config: { url: cfg.url, payload_key: "checks", event_time_field: "checked_at", id_field: "id", text_template: "{name} is {status}" },
    fields: [{ name: "id", example: 41 }, { name: "name", example: "checkout" }, { name: "status", example: "down" }, { name: "checked_at", example: "2026-11-17T09:00:00Z" }],
    sample_item: { id: 41, name: "checkout", status: "down", checked_at: "2026-11-17T09:00:00Z" },
    sample_reply: { checks: [{ id: 41, name: "checkout", status: "down", checked_at: "2026-11-17T09:00:00Z" }] }, per_poll: 2,
  }),
  postgres: (cfg) => cfg.table
    ? { connector: "postgres", summary: "orders has 4 columns; id grows with every row",
        columns: [{ name: "id", type: "bigint" }, { name: "status", type: "text" }, { name: "amount", type: "numeric" }, { name: "created_at", type: "timestamptz" }],
        proposed_config: { table: cfg.table, cursor_column: "id", time_column: "created_at", labels: [{ name: "status", field: "status", primary: true }] } }
    : { tables: ["public.orders", "public.users"] },
  prometheus: (cfg) => cfg.for_label ? { metrics_for_label: ["up", "node_load1"] }
    : cfg.selected ? { connector: "prometheus", summary: "2 metrics, keyed by instance",
        proposed_config: { url: cfg.url, default_key: "instance", labels: [{ name: "instance", field: "instance", primary: true }],
          queries: cfg.selected.map((m) => ({ name: m, promql: m })) } }
    : { catalog: { metrics: ["node_cpu_seconds_total", "node_load1", "up"], labels: ["job", "instance"] } },
  prometheus_alerts: () => ({
    summary: "3 alerting rules in 2 groups", severities: ["critical", "warning"],
    rules: [{ name: "DiskFull", group: "node", severity: "critical", state: "firing" },   // words-ok
            { name: "SlowReply", group: "api", severity: "warning", state: "pending" },
            { name: "HighLoad", group: "node", severity: "warning", state: "inactive" }],
    proposed_config: { labels: [{ name: "alertname", field: "alertname", primary: true }] },
  }),
};
