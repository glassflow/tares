/*
 * Browser e2e for Settings, Team, People on a factory cell: on Tares Cloud one list. The workspace says
 * who is on the team (invite and remove); the cell adds each person's GitHub login and admin flag.
 * General no longer lists the team, and there is no Add person. A person on the cell's records who is
 * not on the team shows apart and can be removed. A self-hosted cell keeps its own list.
 * Run:  npm run build && node tests/factory-team.e2e.cjs     (from ui/, tares installed in ../.venv)
 * Set SHOTS_DIR to keep screenshots.
 */
const path = require("path");
const { chromium } = require(path.join(path.resolve(__dirname, ".."), "node_modules", "playwright"));
const { startDaemon } = require("./licensed.cjs");
const { checkWords, offendersIn } = require("./words.cjs");

const PORT = Number(process.env.TARES_E2E_PORT || 9960);
const SHOTS = process.env.SHOTS_DIR;
let pass = 0, fail = 0;
const ck = (label, cond, detail = "") => { cond ? pass++ : fail++; console.log((cond ? "  ok   " : "  FAIL ") + label + (cond ? "" : `   ${detail}`)); };

(async () => {
  const cell = await startDaemon({ port: PORT, edition: "factory", licence: "valid", env: { TARES_WORKSPACE_API_URL: `http://127.0.0.1:${PORT}/api/ws` } });
  const own = await startDaemon({ port: PORT + 2, edition: "factory", licence: "valid" });
  process.on("exit", () => { cell.stop(); own.stop(); });
  const browser = await chromium.launch(process.env.TARES_E2E_CHROME ? { executablePath: process.env.TARES_E2E_CHROME } : {});
  const post = (base, body) => fetch(`${base}/api/people`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) });
  const cellPeople = async () => (await (await fetch(`${cell.base}/api/people`)).json()).people;
  try {
    await post(cell.base, { name: "Owner", email: "owner@example.com", owner: true });
    await post(cell.base, { name: "Kim", email: "kim@example.com" });
    await post(cell.base, { name: "Sam", email: "sam@example.com" });   // on the records, not on the team

    let members = [
      { user_id: "u1", email: "owner@example.com", name: "Owner", role: "owner", status: "active" },
      { user_id: "u2", email: "kim@example.com", name: "Kim", role: "member", status: "active" },
      { user_id: "u3", email: "lee@example.com", name: "", role: "member", status: "invited" },
    ];
    const page = await (await browser.newContext({ viewport: { width: 1440, height: 900 } })).newPage();
    const json = (route, status, body) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
    await page.route("**/api/ws", (route) => route.request().method() !== "GET" ? route.fallback() : json(route, 200, {
      slug: "acme", state: "running", plan: "team", image_tag: "1.56.0", storage_gb: 5, storage_max_gb: 50, storage_used_bytes: null, storage_pct_used: null,
      host: "acme.example.test", role: "owner", you: { email: "owner@example.com", name: "Owner" }, is_default: false, default_url: "http://127.0.0.1:9/api/ws/default",
    }));
    await page.route("**/api/ws/members", (route) => json(route, 200, { members }));
    await page.route("**/api/ws/members/*", (route) => {
      if (route.request().method() !== "DELETE") return route.fallback();
      members = members.filter((m) => !route.request().url().endsWith(m.user_id));
      return json(route, 200, { removed: true });
    });

    await page.goto(`${cell.base}/settings?tab=people`, { waitUntil: "load" });
    const rows = page.getByTestId("team-row");
    await rows.first().waitFor({ timeout: 15000 });
    ck("one list: everyone on the team", (await rows.count()) === 3, `${await rows.count()}`);
    ck("the page is People, with no Team heading", (await page.getByRole("heading", { name: "People", exact: true }).count()) === 1 && (await page.getByRole("heading", { name: "Team", exact: true }).count()) === 0);
    ck("there is an Invite and no Add person", (await page.getByRole("button", { name: "Invite" }).count()) === 1 && (await page.getByRole("button", { name: "Add person" }).count()) === 0);
    const kim = rows.filter({ hasText: "kim@example.com" });
    const lee = rows.filter({ hasText: "lee@example.com" });
    ck("an invited person says so", /invited/i.test(await lee.innerText()));
    ck("someone not on the cell yet has nothing to edit", (await lee.getByRole("button", { name: /^Edit/ }).count()) === 0);
    await checkWords(page, ck, "people");
    if (SHOTS) await page.screenshot({ path: path.join(SHOTS, "team-people-1440.png"), fullPage: true });

    await kim.getByRole("button", { name: "Edit kim@example.com" }).click();
    const edit = page.getByTestId("team-edit");
    await edit.getByRole("textbox", { name: "GitHub login" }).fill("kim-gh");
    await edit.getByRole("checkbox", { name: "Admin" }).check();
    await edit.getByRole("button", { name: "Save" }).click();
    await edit.waitFor({ state: "detached" });
    await page.waitForFunction(() => document.body.innerText.includes("kim-gh"));
    const saved = (await cellPeople()).find((p) => p.email === "kim@example.com");
    ck("Edit saves the GitHub login and admin on the cell", saved.github === "kim-gh" && saved.is_admin === true, JSON.stringify(saved));

    const stray = page.getByTestId("team-stray");
    ck("a person who is not on the team shows apart", (await stray.count()) === 1 && /sam@example.com/.test(await stray.innerText()));
    await stray.getByRole("button", { name: "Remove Sam" }).click();
    await stray.waitFor({ state: "detached" });
    ck("Remove takes them off the cell", !(await cellPeople()).some((p) => p.email === "sam@example.com"));

    await kim.getByRole("button", { name: "Remove kim@example.com" }).click();
    await page.getByText("kim@example.com no longer has access.").waitFor();
    ck("Remove takes a member off the team", (await rows.count()) === 2, `${await rows.count()}`);

    await page.goto(`${cell.base}/settings?tab=general`, { waitUntil: "load" });
    await page.getByTestId("pane-general").waitFor({ timeout: 15000 });
    await page.getByRole("heading", { name: "acme", exact: true }).waitFor({ timeout: 15000 });
    ck("General no longer lists the team", (await page.getByRole("heading", { name: "Team", exact: true }).count()) === 0 && (await page.getByRole("button", { name: "Invite" }).count()) === 0);
    if (SHOTS) await page.screenshot({ path: path.join(SHOTS, "team-general-1440.png"), fullPage: true });

    // General, the workspace's own panels (TR-1393): the overview, storage, the included credit, the
    // sign-in pin and delete, each against a mocked control plane, at 1440 light and dark and at 390.
    const GB = 1024 ** 3;
    const general = async (width, { dark = false, ov = {}, status = 200 } = {}) => {
      const ctx = await browser.newContext({ viewport: { width, height: width < 600 ? 844 : 900 }, ...(dark ? { colorScheme: "dark" } : {}) });
      const p = await ctx.newPage();
      const seen = { patch: [], put: [], del: [] };
      const reply = { patch: [200, { storage_gb: 10 }], put: [200, {}], del: [409, { detail: "the workspace is still starting" }] };
      const w = { slug: "acme", state: "active", plan: "team", image_tag: "1.56.0", storage_gb: 5, storage_max_gb: 50,
        storage_used_bytes: 4.3 * GB, storage_pct_used: 86, host: "acme.example.test", role: "owner",
        trial: { state: "active", spend_usd: 1.2, credit_usd: 5 }, you: { email: "owner@example.com", name: "Owner" },
        is_default: false, default_url: `${cell.base}/api/ws/default`, ...ov };
      await p.route("**/api/ws?*", (route) => route.request().method() === "DELETE"
        ? (seen.del.push(route.request().url()), json(route, ...reply.del)) : route.fallback());
      await p.route("**/api/ws", (route) => {
        const m = route.request().method();
        if (m === "GET") return status === 200 ? json(route, 200, w) : json(route, status, status === 404 ? {} : { detail: "the control plane is restarting" });
        if (m === "PATCH") { seen.patch.push(route.request().postDataJSON()); return json(route, ...reply.patch); }
        return route.fallback();
      });
      await p.route("**/api/ws/default", (route) => { seen.put.push(route.request().postDataJSON()); return json(route, ...reply.put); });
      await p.route("**/api/ws/members", (route) => json(route, 200, { members }));
      await p.goto(`${cell.base}/settings?tab=general`, { waitUntil: "load" });
      return { p, ctx, seen, reply };
    };

    for (const [w, dark] of [[1440, false], [1440, true], [390, false]]) {
      const tag = `${w}${dark ? " dark" : ""}`;
      const { p, ctx } = await general(w, { dark });
      const ovp = p.getByTestId("ws-overview");
      await ovp.waitFor({ timeout: 15000 });
      const facts = await ovp.innerText();
      ck(`general ${tag}: the overview names the plan, the version and the state`,
        /Plan\s*team/.test(facts) && /Tares version\s*1\.56\.0/.test(facts) && /State\s*Running/.test(facts), JSON.stringify(facts));
      ck(`general ${tag}: storage shows a bar and says it is nearly full`,
        (await p.getByRole("progressbar", { name: "Storage used" }).count()) === 1 && /86% full/.test(await p.getByTestId("ws-near-full").innerText()));
      ck(`general ${tag}: the new size has a label and its range under it`,
        (await p.getByRole("spinbutton", { name: "New size in GiB" }).count()) === 1 && (await p.getByText("From 5 to 50 GiB.").count()) === 1);
      ck(`general ${tag}: the credit says what is used, with no dead link on a factory cell`,
        /\$1\.20 of \$5\.00/.test(await p.getByTestId("ws-credit").innerText()) && (await p.getByTestId("ws-credit").getByRole("button").count()) === 0);
      ck(`general ${tag}: the sign-in pin is a switch with its label`,
        (await p.getByRole("switch", { name: "Open this workspace when I sign in" }).count()) === 1);
      ck(`general ${tag}: delete waits for the typed name`, await p.getByRole("button", { name: "Delete workspace" }).isDisabled());
      ck(`general ${tag}: the workspace's panels are kit panels`, (await p.locator("[data-testid=workspace] > .panel:not([data-testid])").count()) === 2
        && (await p.locator("[data-testid=workspace] > [data-testid^=ws-]").count()) === 5, `${await p.locator("[data-testid=workspace] > [data-testid^=ws-]").count()}`);
      const over = await p.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
      ck(`general ${tag}: no sideways scroll`, over <= 0, `${over}px`);
      // only the workspace's own panels: the usage panels between them are shared legacy ones (UsagePanels,
      // ClaudeUsage) that still say run and station, out of this move's scope
      const own = (await p.locator("[data-testid=workspace] > [data-testid^=ws-]").allInnerTexts()).join("\n");
      ck(`general ${tag}: no internal names or em dashes in the workspace's panels`, offendersIn(own).length === 0, offendersIn(own).join(", "));
      if (SHOTS) await p.screenshot({ path: path.join(SHOTS, `general-${w}${dark ? "-dark" : ""}-after.png`), fullPage: true });
      await ctx.close();
    }

    {
      const { p, ctx, seen, reply } = await general(1440);
      const size = p.getByRole("spinbutton", { name: "New size in GiB" });
      await size.waitFor({ timeout: 15000 });
      await size.fill("60");
      await p.getByRole("button", { name: "Grow" }).click();
      ck("grow: a size over the plan's maximum is said under the field, and nothing is sent",
        (await p.getByText("Give a whole number from 5 to 50.").count()) === 1 && (await size.getAttribute("aria-invalid")) === "true" && seen.patch.length === 0);
      await size.fill("10");
      await size.press("Enter");
      await p.getByTestId("ws-grown").waitFor({ timeout: 5000 });
      ck("grow: Enter sends the new size and says it grows in the background", JSON.stringify(seen.patch) === JSON.stringify([{ storage_gb: 10 }]), JSON.stringify(seen.patch));
      ck("grow: the panel shows the new size", /Now 10 GiB/.test(await p.getByTestId("ws-storage-panel").innerText()));
      if (SHOTS) await p.screenshot({ path: path.join(SHOTS, "general-grown-1440-after.png"), fullPage: true });
      reply.patch = [409, { detail: "the volume is busy" }];
      await size.fill("12");
      await p.getByRole("button", { name: "Grow" }).click();
      await p.getByTestId("ws-storage-panel").getByTestId("ws-error").waitFor({ timeout: 5000 });
      ck("grow: a refusal is said as a sentence and the field goes back",
        (await p.getByTestId("ws-storage-panel").getByTestId("ws-error").innerText()).trim() === "The volume is busy." && (await size.inputValue()) === "10");

      const pin = p.getByRole("switch", { name: "Open this workspace when I sign in" });
      await pin.click();
      await p.getByText("Signing in to Tares Cloud brings you straight here.").waitFor({ timeout: 5000 });
      ck("pin: the switch pins this workspace", JSON.stringify(seen.put) === JSON.stringify([{ slug: "acme" }]) && (await pin.getAttribute("aria-checked")) === "true");

      await p.getByRole("textbox", { name: "Type acme to confirm" }).fill("acme");
      await p.getByRole("button", { name: "Delete workspace" }).click();
      await p.getByTestId("ws-delete-panel").getByTestId("ws-error").waitFor({ timeout: 5000 });
      ck("delete: the typed name sends the delete, and a refusal is said in the panel",
        seen.del.length === 1 && /confirm=acme/.test(seen.del[0]) && /The workspace is still starting\./.test(await p.getByTestId("ws-delete-panel").innerText()));
      await ctx.close();
    }
    {
      const { p, ctx, seen } = await general(1440, { ov: { default_url: "https://elsewhere.example/api/ws/default" } });
      const pin = p.getByRole("switch", { name: "Open this workspace when I sign in" });
      await pin.waitFor({ timeout: 15000 });
      await pin.click();
      await p.getByTestId("ws-pin").getByTestId("ws-error").waitFor({ timeout: 5000 });
      ck("pin: an address on another origin is never written to", seen.put.length === 0 && (await pin.getAttribute("aria-checked")) === "false");
      await ctx.close();
    }
    {
      const { p, ctx } = await general(1440, { ov: { role: "member", storage_pct_used: 40, storage_used_bytes: 2 * GB, trial: { state: "exhausted", spend_usd: 5, credit_usd: 5 } } });
      await p.getByTestId("ws-overview").waitFor({ timeout: 15000 });
      ck("member: no grow, no delete", (await p.getByRole("spinbutton").count()) === 0 && (await p.getByTestId("ws-delete-panel").count()) === 0);
      ck("member: under the warning threshold, no warning", (await p.getByTestId("ws-near-full").count()) === 0);
      ck("member: used-up credit says so", /is used up/.test(await p.getByTestId("ws-credit").innerText()));
      if (SHOTS) await p.screenshot({ path: path.join(SHOTS, "general-member-1440-after.png"), fullPage: true });
      await ctx.close();
    }
    {
      const { p, ctx } = await general(1440, { status: 500 });
      const err = p.getByTestId("ws-load-error");
      await err.waitFor({ timeout: 15000 });
      ck("load error: said as a sentence, with Try again", (await err.innerText()).includes("The control plane is restarting.") && (await err.getByRole("button", { name: "Try again" }).count()) === 1);
      if (SHOTS) await p.screenshot({ path: path.join(SHOTS, "general-error-1440-after.png"), fullPage: true });
      await ctx.close();
    }
    {
      const { p, ctx } = await general(1440, { status: 404 });
      const err = p.getByTestId("ws-load-error");
      await err.waitFor({ timeout: 15000 });
      ck("gone: no access, and nothing to try again", /no longer have access/.test(await err.innerText()) && (await err.getByRole("button").count()) === 0);
      await ctx.close();
    }
    {
      const { p, ctx } = await general(1440, { status: 401 });
      await p.getByText("You are signed out of Tares Cloud.").waitFor({ timeout: 15000 });
      ck("signed out: the page says so in place of the panels", (await p.getByTestId("ws-overview").count()) === 0);
      await ctx.close();
    }

    const phone = await (await browser.newContext({ viewport: { width: 390, height: 844 } })).newPage();
    await phone.route("**/api/ws", (route) => route.request().method() !== "GET" ? route.fallback() : json(route, 200, {
      slug: "acme", state: "running", plan: "team", role: "owner", you: { email: "owner@example.com" }, is_default: false }));
    await phone.route("**/api/ws/members", (route) => json(route, 200, { members }));
    await phone.goto(`${cell.base}/settings?tab=people`, { waitUntil: "load" });
    await phone.getByTestId("team-row").first().waitFor({ timeout: 15000 });
    const over = await phone.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    ck("390: no sideways scroll", over <= 0, `${over}px`);

    const self = await (await browser.newContext({ viewport: { width: 1440, height: 900 } })).newPage();
    await self.goto(`${own.base}/settings?tab=people`, { waitUntil: "load" });
    await self.getByRole("button", { name: "Add person" }).waitFor({ timeout: 15000 });
    ck("a self-hosted cell keeps its own list and Add person", (await self.getByTestId("team-row").count()) === 0);
  } finally {
    await browser.close();
    cell.stop(); own.stop();
  }
  console.log(`\n${pass} passed, ${fail} failed`);
  process.exit(fail ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(1); });
