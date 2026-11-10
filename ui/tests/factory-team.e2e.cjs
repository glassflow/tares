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
const { checkWords } = require("./words.cjs");

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
