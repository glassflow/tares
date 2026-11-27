/*
 * Browser e2e for Connect (/connect, TR-1395): four tabs (MCP, Webhook, REST API, Tools); the first comes
 * from ?tab= and later picks stay off the address; the MCP endpoint is found behind a proxy, on its own
 * port, or not at all, and each says so; with sign-in on, the token is masked on screen and real on the
 * clipboard, in its panel and in every snippet; each client gets its own snippet; the Webhook tab names
 * the first rule and says when Tares could not list the rules or has none; the Tools tab lists, searches,
 * loads, fails and retries. The factory and the SRE editions name rules their own way. Light and dark,
 * 1440 and 390. The daemons are real; the MCP probe, the tools and the rules are mocked (connect.fixture.cjs).
 * Run:  npm run build && node tests/connect.e2e.cjs     (from ui/, tares installed in ../.venv)
 */
const path = require("path");
const { chromium } = require("./browser.cjs");
const { startDaemon } = require("./licensed.cjs");
const { offendersIn } = require("./words.cjs");
const { mockConnect } = require("./connect.fixture.cjs");

const PORT = Number(process.env.TARES_E2E_PORT || 9991);
const TOKEN = "tok-connect-e2e";
let pass = 0, fail = 0;
const ck = (label, cond, detail = "") => { cond ? pass++ : fail++; console.log((cond ? "  ok   " : "  FAIL ") + label + (cond ? "" : `   ${detail}`)); };

const MAIN_TEXT = () => (document.querySelector("main") || document.body).innerText;
// The page's own words: wire names in <code data-words-exempt> (scopes, tool names, snippets) are the
// API's spelling, exempt in the source check too.
const PAGE_WORDS = () => {
  const main = (document.querySelector("main") || document.body).cloneNode(true);
  main.querySelectorAll("[data-words-exempt], [data-user-text]").forEach((n) => n.remove());
  document.body.appendChild(main);
  const t = main.innerText;
  main.remove();
  return t;
};
const sideways = (page) => page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
const text = async (loc) => (await loc.count()) === 1 ? (await loc.innerText()).replace(/\s+/g, " ").trim() : `<${await loc.count()} found>`;

(async () => {
  const signed = await startDaemon({ port: PORT, edition: "factory", licence: "valid", auth: TOKEN });
  const open_ = await startDaemon({ port: PORT + 1, edition: "factory", licence: "valid" });
  const sre = await startDaemon({ port: PORT + 2, edition: "sre", licence: "valid" });
  process.on("exit", () => { signed.stop(); open_.stop(); sre.stop(); });
  const browser = await chromium.launch(process.env.TARES_E2E_CHROME ? { executablePath: process.env.TARES_E2E_CHROME } : {});

  const open = async (to, { d = signed, width = 1440, dark = false, wait = "connect-endpoint", healthDelay = 0, ...mock } = {}) => {
    const ctx = await browser.newContext({ viewport: { width, height: width < 600 ? 844 : 900 }, ...(dark ? { colorScheme: "dark" } : {}),
      permissions: ["clipboard-read", "clipboard-write"] });
    if (d === signed) await ctx.addInitScript((t) => { try { localStorage.setItem("tares_token", t); } catch { /* none */ } }, TOKEN);
    const page = await ctx.newPage();
    const errors = [];
    page.on("pageerror", (e) => errors.push(String(e)));
    const calls = await mockConnect(page, mock);
    if (healthDelay) await page.route("**/health", async (route) => {
      await new Promise((resolve) => setTimeout(resolve, healthDelay));
      await route.continue();
    });
    await page.goto(`${d.base}${to}`);
    await page.getByTestId("connect-page").waitFor({ timeout: 10000 }).catch(() => {});
    if (wait === "connect-endpoint") {
      await page.waitForFunction(() => !document.querySelector("[data-testid=connect-endpoint]")?.textContent?.startsWith("Looking"), null, { timeout: 8000 }).catch(() => {});
    } else if (wait) await page.getByTestId(wait).first().waitFor({ timeout: 10000 }).catch(() => {});
    const close = ctx.close.bind(ctx);
    ctx.close = async () => { await page.unrouteAll({ behavior: "ignoreErrors" }); await close(); };
    return { ctx, page, errors, calls };
  };
  const words = async (page, label) => {
    const found = offendersIn(await page.evaluate(PAGE_WORDS));
    ck(`${label}: no internal names`, !found.filter((w) => w !== "em dash").length, found.join(", "));
    ck(`${label}: no em dashes`, !found.includes("em dash"), found.join(", "));
  };
  const tab = (page, name) => page.getByRole("tab", { name, exact: true });
  const selected = async (page) => (await page.getByRole("tab", { selected: true }).innerText()).trim();
  const codes = (page) => page.getByTestId("connect-code").allInnerTexts();
  // The snippets carry no Authorization line until /health answers. The MCP checks wait for the token
  // panel; the Webhook and REST tabs have no such panel, so wait there for the signed snippet itself.
  const signedSnippet = (page) => page.getByTestId("connect-code").filter({ hasText: "Bearer" }).first().waitFor({ timeout: 10000 }).catch(() => {});
  const clip = (page) => page.evaluate(() => navigator.clipboard.readText());
  const settle = (page) => page.waitForTimeout(300);
  const client = async (page, name) => {
    await page.getByTestId("connect-client").click();
    await page.getByRole("option", { name, exact: true }).click();
    await settle(page);
  };

  try {
    // ── landing: MCP, behind a proxy, signed in ─────────────────────────────────────────────────────
    {
      const { ctx, page, errors } = await open("/connect");
      ck("page: titled Connect", (await page.getByRole("heading", { level: 1 }).innerText()) === "Connect");
      ck("page: says what it is for", (await page.evaluate(MAIN_TEXT)).includes("Give an agent of your own access to this Tares."));
      ck("tabs: four, in order", JSON.stringify(await page.getByRole("tab").allInnerTexts()) === JSON.stringify(["MCP", "Webhook", "REST API", "Tools"]),
         JSON.stringify(await page.getByRole("tab").allInnerTexts()));
      ck("tabs: opens on MCP", (await selected(page)) === "MCP");
      await words(page, "mcp");
      const ep = await text(page.getByTestId("connect-endpoint"));
      ck("endpoint: live behind the proxy, at this origin", ep === `The MCP endpoint is live at ${signed.base}/mcp`, ep);

      // the token: masked, revealed, hidden, copied
      const tok = page.getByTestId("connect-token-value");
      ck("token: masked on screen", (await tok.innerText()) === "••••••••");
      ck("token: no snippet shows it", !(await codes(page)).some((c) => c.includes(TOKEN)));
      ck("token: every snippet with a token shows the mask", (await codes(page)).filter((c) => c.includes("Bearer")).every((c) => c.includes("Bearer ••••••••")), (await codes(page)).join(" | "));
      await page.getByRole("button", { name: "Show" }).click();
      ck("token: Show reveals it", (await tok.innerText()) === TOKEN);
      ck("token: revealed in the snippet too", (await codes(page))[0].includes(`Bearer ${TOKEN}`));
      await page.getByRole("button", { name: "Hide" }).click();
      ck("token: Hide masks it again", (await tok.innerText()) === "••••••••");
      await page.getByRole("button", { name: "Copy token" }).click();
      await settle(page);
      ck("token: Copy token puts the real token on the clipboard", (await clip(page)) === TOKEN);
      ck("token: points to a scoped key in Settings", (await page.getByTestId("connect-token").getByRole("link", { name: "Settings" }).getAttribute("href")) === "/settings");

      // Claude Code (the default client)
      const cc = (await codes(page))[0];
      ck("claude code: the add command with this endpoint", cc === `claude mcp add --transport http tares ${signed.base}/mcp --header "Authorization: Bearer ••••••••"`, cc);
      const copy = page.getByTestId("connect-copy").first();
      await copy.click();
      ck("claude code: copy carries the real token, not the mask", (await clip(page)) === `claude mcp add --transport http tares ${signed.base}/mcp --header "Authorization: Bearer ${TOKEN}"`, await clip(page));
      ck("claude code: copy says it copied", (await copy.innerText()) === "Copied");
      ck("claude code: offers the plugin", (await codes(page))[1] === "/plugin marketplace add glassflow/tares\n/plugin install tares@tares");

      // the other clients
      await client(page, "Codex CLI");
      ck("codex: env var and --url", (await codes(page))[0] === `export TARES_API_KEY=••••••••\ncodex mcp add tares --url ${signed.base}/mcp --bearer-token-env-var TARES_API_KEY`, (await codes(page))[0]);
      await client(page, "Cursor");
      const cursor = JSON.parse((await codes(page))[0]);
      ck("cursor: the bare-url shape, no type", cursor.mcpServers.tares.url === `${signed.base}/mcp` && !("type" in cursor.mcpServers.tares)
         && cursor.mcpServers.tares.headers.Authorization === "Bearer ••••••••", (await codes(page))[0]);
      await page.getByTestId("connect-copy").first().click();
      ck("cursor: copy carries the real token", JSON.parse(await clip(page)).mcpServers.tares.headers.Authorization === `Bearer ${TOKEN}`);
      await client(page, "Claude Desktop");
      ck("claude desktop: names its config file", (await text(page.getByTestId("connect-snippet").first().locator("h3"))).startsWith("Add to claude_desktop_config.json"));
      await client(page, "Another client (JSON)");
      const other = JSON.parse((await codes(page))[0]);
      ck("another client: the http shape", other.mcpServers.tares.type === "http" && other.mcpServers.tares.url === `${signed.base}/mcp`);
      await client(page, "On this machine (stdio)");
      const stdio = JSON.parse((await codes(page))[0]);
      ck("stdio: tares-mcp pointed at this daemon with the token",
         stdio.mcpServers.tares.command === "tares-mcp" && stdio.mcpServers.tares.env.TARESD_URL === signed.base
         && stdio.mcpServers.tares.env.TARES_AUTH_TOKEN === "••••••••", (await codes(page))[0]);
      ck("mcp: no errors", !errors.length, errors.join(" | "));
      await ctx.close();
    }

    // ── the endpoint on its own port, and nowhere ───────────────────────────────────────────────────
    {
      const { ctx, page } = await open("/connect", { mcp: "port" });
      const host = new URL(signed.base).hostname;
      const ep = await text(page.getByTestId("connect-endpoint"));
      ck("endpoint: on port 8788 when only it answers", ep === `The MCP endpoint is live at http://${host}:8788/mcp`, ep);
      ck("endpoint: the snippet uses that address", (await codes(page))[0].includes(`tares http://${host}:8788/mcp`));
      await ctx.close();
    }
    {
      const { ctx, page } = await open("/connect", { mcp: "none" });
      const ep = page.getByTestId("connect-endpoint");
      ck("endpoint: says when nothing answers", (await text(ep)).startsWith("No MCP endpoint answers"), await text(ep));
      // The kit's warn tone is the inked border on muted paper; a note sits on the card.
      ck("endpoint: as a warning, not a note", (await ep.evaluate((n) => n.className)).includes("bg-muted"),
         await ep.evaluate((n) => n.className));
      ck("endpoint: says how to start one, and the stdio way", (await text(ep)).includes("Start tares mcp") && (await text(ep)).includes("On this machine (stdio)"));
      await words(page, "absent");
      await ctx.close();
    }

    // ── sign-in off: no token anywhere ────────────────────────────────────────────────────────────
    {
      const { ctx, page } = await open("/connect", { d: open_ });
      ck("no sign-in: no token panel", (await page.getByTestId("connect-token").count()) === 0);
      ck("no sign-in: the command has no header", (await codes(page))[0] === `claude mcp add --transport http tares ${open_.base}/mcp`, (await codes(page))[0]);
      await tab(page, "REST API").click();
      ck("no sign-in: the curl has no Authorization", !(await codes(page))[0].includes("Authorization"));
      await ctx.close();
    }

    // ── the address picks the first tab only ─────────────────────────────────────────────────────
    {
      const { ctx, page } = await open("/connect?tab=push");
      ck("address: ?tab=push opens Webhook", (await selected(page)) === "Webhook");
      await tab(page, "Tools").click();
      await settle(page);
      ck("address: picking a tab does not change the address", new URL(page.url()).search === "?tab=push", page.url());
      await ctx.close();
    }
    {
      const { ctx, page } = await open("/connect?tab=nonsense");
      ck("address: an unknown tab opens MCP", (await selected(page)) === "MCP");
      await ctx.close();
    }

    // ── Webhook ─────────────────────────────────────────────────────────────────────────────────
    {
      const { ctx, page, errors } = await open("/connect?tab=push");
      await signedSnippet(page);
      await words(page, "webhook");
      const all = await codes(page);
      ck("webhook: a receiver sample, the shape and the add call", all.length === 3, String(all.length));
      ck("webhook: the shape names the first rule", JSON.parse(all[1]).trigger === "error_spike", all[1]);
      ck("webhook: the add call names the first rule, at this origin",
         all[2].startsWith(`curl -X POST ${signed.base}/subscribe`) && all[2].includes('"trigger": "error_spike"') && all[2].includes("Bearer ••••••••"), all[2]);
      await page.getByTestId("connect-copy").nth(2).click();
      ck("webhook: copy carries the real token", (await clip(page)).includes(`Bearer ${TOKEN}`));
      ck("webhook: says it is at least once, and how retries go",
         (await text(page.getByTestId("connect-retries"))).startsWith("Tares sends each notification at least once, so dedupe on dispatch_id."));
      ck("webhook: factory says rule", (await page.evaluate(MAIN_TEXT)).includes("Add the endpoint to a rule"));
      ck("webhook: no errors", !errors.length, errors.join(" | "));
      await ctx.close();
    }
    {
      const { ctx, page } = await open("/connect?tab=push", { rules: [], wait: "connect-no-rules" });
      ck("no rules: says there is nothing to add it to", (await text(page.getByTestId("connect-no-rules"))).startsWith("This Tares has no rules yet"));
      ck("no rules: no add call", (await codes(page)).length === 2);
      await ctx.close();
    }
    {
      const { ctx, page } = await open("/connect?tab=push", { rules: "fail", wait: "connect-rules-failed" });
      const b = page.getByTestId("connect-rules-failed");
      ck("rules failed: says Tares could not list them", (await text(b)).includes("Tares could not list its rules: database is locked"), await text(b));
      ck("rules failed: offers to try again", (await b.getByRole("button", { name: "Try again" }).count()) === 1);
      ck("rules failed: does not claim there are none", (await page.getByTestId("connect-no-rules").count()) === 0);
      await ctx.close();
    }
    {
      const { ctx, page } = await open("/connect?tab=push", { d: sre });
      ck("sre: says check rule", (await page.evaluate(MAIN_TEXT)).includes("Add the endpoint to a check rule"));
      await words(page, "sre webhook");
      await ctx.close();
    }

    // ── REST API ────────────────────────────────────────────────────────────────────────────────
    {
      const { ctx, page } = await open("/connect?tab=rest", { healthDelay: 500 });
      // The REST snippet renders before /health resolves. Wait for its auth state,
      // including when the health response deliberately arrives after the snippet.
      await page.getByTestId("connect-code").first().filter({ hasText: "Bearer ••••••••" }).waitFor();
      await words(page, "rest");
      const c = (await codes(page))[0];
      ck("rest: a /read call at this origin", c.startsWith(`curl -X POST ${signed.base}/read`) && c.includes('"window": "15m"') && c.includes("Bearer ••••••••"), c);
      await ctx.close();
    }

    // ── Tools ───────────────────────────────────────────────────────────────────────────────────
    {
      const { ctx, page } = await open("/connect?tab=tools", { wait: "connect-tool" });
      await words(page, "tools");
      const rows = page.getByTestId("connect-tool");
      ck("tools: one row per tool", (await rows.count()) === 4);
      ck("tools: name and what it does", (await text(rows.first())).replace(/\s+/g, " ") === "read One correlated timeline for a key over a window.", await text(rows.first()));
      ck("tools: counts them", (await text(page.getByTestId("connect-tools-count"))) === "4 of 4");
      const search = page.getByRole("textbox", { name: "Search the tools" });
      await search.fill("timeline");
      ck("search: by what it does", (await rows.count()) === 2 && (await text(page.getByTestId("connect-tools-count"))) === "2 of 4", String(await rows.count()));
      await search.fill("CATALOG");
      ck("search: by name, any case", (await rows.count()) === 1 && (await rows.first().innerText()).startsWith("catalog_list"));
      await search.fill("zzz");
      ck("search: nothing matches says so", (await rows.count()) === 0 && (await text(page.getByTestId("connect-tools-none"))) === "No tool matches.");
      await ctx.close();
    }
    {
      const { ctx, page } = await open("/connect?tab=tools", { tools: [], wait: "connect-tools-empty" });
      ck("tools empty: says so", (await page.getByTestId("connect-tools-empty").count()) === 1);
      ck("tools empty: no search box", (await page.getByRole("textbox", { name: "Search the tools" }).count()) === 0);
      await ctx.close();
    }
    {
      const { ctx, page, calls } = await open("/connect?tab=tools", { tools: "fail", toolsFailUntil: 1, wait: "connect-tools-failed" });
      const b = page.getByTestId("connect-tools-failed");
      ck("tools failed: says Tares could not list them", (await text(b)).includes("Tares could not list its tools: the MCP server did not answer"), await text(b));
      ck("tools failed: does not claim there are none", (await page.getByTestId("connect-tools-empty").count()) === 0);
      const retry = b.getByRole("button", { name: "Try again" });
      ck("tools failed: offers to try again", (await retry.count()) === 1);
      if (await retry.count()) await retry.click();
      await page.getByTestId("connect-tool").first().waitFor({ timeout: 5000 }).catch(() => {});
      ck("retry: asks again and lists them", calls.toolCalls >= 2 && (await page.getByTestId("connect-tool").count()) === 4, `${calls.toolCalls} calls`);
      await ctx.close();
    }
    {
      const { ctx, page } = await open("/connect?tab=tools", { tools: "slow", wait: null });
      await page.waitForTimeout(600);
      ck("tools slow: shows it is loading", (await page.locator("main [aria-busy=true]").count()) > 0);
      ck("tools slow: does not claim there are none", (await page.getByTestId("connect-tools-empty").count()) === 0);
      await ctx.close();
    }

    // ── layout ────────────────────────────────────────────────────────────────────────────────
    // The nav link to /connect sits under Advanced; factory-settings.e2e.cjs clicks it through.
    for (const [width, dark] of [[1440, true], [390, false], [390, true]]) {
      const label = `${width}${dark ? " dark" : ""}`;
      const { ctx, page } = await open("/connect", { width, dark });
      for (const t of ["MCP", "Webhook", "REST API", "Tools"]) {
        await tab(page, t).click();
        await settle(page);
        ck(`layout ${label}: ${t} has no sideways scroll`, (await sideways(page)) <= 0, String(await sideways(page)));
      }
      await ctx.close();
    }
  } catch (e) {
    ck("suite ran without an exception", false, String(e && e.stack || e));
  } finally {
    await browser.close();
  }
  console.log(`\n${pass} passed, ${fail} failed`);
  process.exit(fail ? 1 : 0);
})();
