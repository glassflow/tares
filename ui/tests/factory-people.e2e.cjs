/*
 * Browser e2e for the factory People page (TR-1011) on a self-hosted cell.
 *  - The menu has People, and it opens /people.
 *  - Signed in as an admin: every person is listed, you first and marked, admins marked, a person
 *    who never signed in shows as invited; the add form adds a teammate, who shows as invited; the
 *    same email twice says so.
 *  - Signed in with a read key: the list shows, the form does not, and the page says why.
 *  - Pictures (TR-1016): a person with a picture shows it, round, in the account button and on
 *    their row; a picture that does not load falls back to the initials; you change your own.
 *  - At phone width the page fits without a sideways scroll.
 *  - The page's words keep the words list.
 *  - A cell with no licence, open (TR-930): /people lists only you, with the licence line and no add form;
 *    Settings, Access has no "Add person" and the key dialog no person picker; no ee chunk is requested.
 *
 * Runs two real daemons (licensed and not, tests/licensed.cjs) on scratch data directories, serving the built console.
 * Run:  npm run build && node tests/factory-people.e2e.cjs
 */
const http = require("http");
const path = require("path");

const UI_DIR = path.resolve(__dirname, "..");
const { chromium } = require(path.join(UI_DIR, "node_modules", "playwright"));
const { checkWords } = require("./words.cjs");
const { startDaemon } = require("./licensed.cjs");

const ROOT = "root-token-factory-people-e2e";
const PNG = Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFBQIAX8jx0gAAAABJRU5ErkJggg==", "base64");
const PORT = Number(process.env.TARES_E2E_PORT || 8871);
let BASE = `http://127.0.0.1:${PORT}`;
const EE_CHUNK = /\/assets\/ee-[^/]*\.js(\?|$)/;
const SHOTS = process.env.SHOTS_DIR;

let pass = 0, fail = 0;
const ck = (label, cond, detail = "") => {
  cond ? pass++ : fail++;
  console.log((cond ? "  ok   " : "  FAIL ") + label + (cond ? "" : `   ${detail}`));
};

function call(method, url, body, headers = {}) {
  const data = body === undefined ? "" : JSON.stringify(body);
  return new Promise((resolve, reject) => {
    const r = http.request(BASE + url, {
      method,
      headers: { "content-type": "application/json", "content-length": Buffer.byteLength(data), ...headers },
    }, (res) => {
      let d = ""; res.on("data", (c) => (d += c));
      res.on("end", () => { let j = null; try { j = JSON.parse(d); } catch { /* not json */ } resolve({ status: res.statusCode, json: j }); });
    });
    r.on("error", reject); r.write(data); r.end();
  });
}

function waitFor(url, tries = 150) {
  return new Promise((resolve) => {
    const tick = () => http.get(url, (r) => { r.resume(); resolve(true); })
      .on("error", () => (--tries > 0 ? setTimeout(tick, 200) : resolve(false)));
    tick();
  });
}

(async () => {
  // a licensed daemon: the page adds people, which a cell with no licence does not do. Each cell takes its
  // port and the next (MCP); the unlicensed one sits two further on.
  let d, bare;
  try {
    d = await startDaemon({ port: PORT, edition: "factory", licence: "valid", seats: 50, auth: ROOT });
    bare = await startDaemon({ port: PORT + 2, edition: "factory", licence: "none" });   // open: the one person is you
  } catch (e) { console.log("FAIL daemon did not start: " + e.message); process.exit(1); }
  process.on("exit", () => { d.stop(); bare.stop(); });

  const root = { authorization: `Bearer ${ROOT}` };
  const bob = await call("POST", "/api/keys", { name: "bob-laptop", scopes: ["admin"], person: { email: "bob@example.com", name: "Bob B" } }, root);
  const alice = await call("POST", "/api/keys", { name: "alice-laptop", scopes: ["read"], person: { email: "alice@example.com", name: "Alice A" } }, root);
  const kim = await call("POST", "/api/factory/people", { email: "kim@example.com", name: "Kim K" }, root);
  await call("PATCH", `/api/people/${kim.json.id}`, { is_admin: true, picture: "https://pictures.example.test/broken.png" }, root);
  await call("PATCH", `/api/people/${bob.json.person.id}`, { picture: "https://pictures.example.test/bob.png" }, root);
  ck("keys and a person made", bob.status === 201 && alice.status === 201, `${bob.status} ${alice.status}`);
  // Alice has used her key once, so she has signed in; Kim never has
  await call("GET", "/api/factory/people", undefined, { authorization: `Bearer ${alice.json.secret}` });

  const browser = await chromium.launch(process.env.TARES_E2E_CHROME ? { executablePath: process.env.TARES_E2E_CHROME } : {});
  const open = async (token, width = 1440) => {
    const ctx = await browser.newContext({ viewport: { width, height: 900 } });
    await ctx.addInitScript((t) => { try { localStorage.setItem("tares_token", t); } catch { /* none */ } }, token);
    // the pictures' host: bob.png and new.png are a 1x1 image, broken.png fails
    await ctx.route("https://pictures.example.test/**", (route) => route.request().url().endsWith("broken.png")
      ? route.abort()
      : route.fulfill({ status: 200, contentType: "image/png", body: PNG }));
    const page = await ctx.newPage();
    await page.goto(`${BASE}/people`);
    await page.getByTestId("people-list").waitFor({ timeout: 15000 });
    await page.getByTestId("person-row").first().waitFor({ timeout: 15000 });
    return { ctx, page };
  };
  const rowOf = (page, email) => page.getByTestId("person-row").filter({ hasText: email });
  try {
    // ---- an admin
    const { ctx, page } = await open(bob.json.secret);
    ck("the menu has People, current here", (await page.locator('nav[aria-label="Main"] a', { hasText: "People" }).getAttribute("href")) === "/people"
       && (await page.locator('nav[aria-label="Main"] a[aria-current="page"]').allInnerTexts()).map((t) => t.trim()).join() === "People");
    ck("the heading", await page.getByRole("heading", { name: "People", exact: true }).isVisible());
    // pictures
    const avatarImg = (loc) => loc.locator('img[referrerpolicy="no-referrer"]');
    await page.waitForFunction(() => document.querySelector('[data-testid="account-avatar"] img')?.complete);
    ck("pictures: yours shows round in the account button", (await page.getByTestId("account-avatar").getAttribute("data-picture")) === "on"
       && (await avatarImg(page.getByTestId("account-avatar")).getAttribute("src")) === "https://pictures.example.test/bob.png"
       && /rounded-full/.test(await page.getByTestId("account-avatar").getAttribute("class")));
    ck("pictures: and on your row", (await rowOf(page, "bob@example.com").getByTestId("person-avatar").getAttribute("data-picture")) === "on");
    await page.waitForFunction(() => [...document.querySelectorAll('[data-testid="person-row"]')].some((r) => r.textContent.includes("kim@example.com")
      && r.querySelector('[data-testid="person-avatar"]')?.getAttribute("data-picture") === "off"));
    ck("pictures: one that does not load shows the initials", (await rowOf(page, "kim@example.com").getByTestId("person-avatar").innerText()).trim() === "KK");
    ck("pictures: no picture, initials", (await rowOf(page, "alice@example.com").getByTestId("person-avatar").getAttribute("data-picture")) === "off");
    await rowOf(page, "bob@example.com").getByTestId("picture-open").click();
    await page.getByTestId("picture-url").fill("http://pictures.example.test/new.png");
    await page.getByTestId("picture-save").click();
    await page.getByText("Picture must be an https address.").waitFor({ timeout: 10000 });
    ck("pictures: an address that is not https is refused, in words", true);
    await page.getByTestId("picture-url").fill("https://pictures.example.test/new.png");
    await page.getByTestId("picture-save").click();
    await page.waitForFunction(() => document.querySelector('[data-testid="account-avatar"] img')?.getAttribute("src") === "https://pictures.example.test/new.png");
    ck("pictures: you change your own, and the account button follows", (await avatarImg(rowOf(page, "bob@example.com").getByTestId("person-avatar")).getAttribute("src")) === "https://pictures.example.test/new.png");
    ck("pictures: only on your own row", (await rowOf(page, "alice@example.com").getByTestId("picture-open").count()) === 0);
    const names = (await page.getByTestId("person-name").allInnerTexts()).map((t) => t.trim());
    ck("you come first, and are marked", names[0] === "Bob B" && /\(you\)/.test(await page.getByTestId("person-row").first().innerText()), names.join("|"));
    ck("every person is listed", ["Bob B", "Alice A", "Kim K"].every((n) => names.includes(n)), names.join("|"));
    ck("an admin is marked, the others are not", (await rowOf(page, "kim@example.com").getByTestId("person-admin").count()) === 1
       && (await rowOf(page, "alice@example.com").getByTestId("person-admin").count()) === 0);
    ck("signed in: last active", /^Last active/.test((await rowOf(page, "alice@example.com").getByTestId("person-state").innerText()).trim()));
    ck("never signed in: invited", (await rowOf(page, "kim@example.com").getByTestId("person-state").innerText()).trim() === "Invited, not signed in yet");
    ck("the add form is there, with Add", await page.getByTestId("people-add").isVisible()
       && (await page.getByTestId("people-submit").innerText()).trim() === "Add");
    await checkWords(page, ck, "admin");
    if (SHOTS) await page.screenshot({ path: path.join(SHOTS, "factory-people-admin-1440.png"), fullPage: true });

    await page.getByTestId("people-email").fill("sam@example.com");
    await page.getByTestId("people-name").fill("Sam S");
    await page.getByTestId("people-submit").click();
    await page.getByTestId("people-note").waitFor({ timeout: 10000 });
    ck("adding says so", (await page.getByTestId("people-note").innerText()).trim() === "Added Sam S.");
    await rowOf(page, "sam@example.com").waitFor({ timeout: 10000 });
    ck("the new person is listed as invited", (await rowOf(page, "sam@example.com").getByTestId("person-state").innerText()).trim() === "Invited, not signed in yet");
    ck("the form is empty again", (await page.getByTestId("people-email").inputValue()) === "");
    await page.getByTestId("people-email").fill("sam@example.com");
    await page.getByTestId("people-submit").click();
    await page.getByText("sam@example.com is already on this cell.").waitFor({ timeout: 10000 });
    ck("the same email twice says so", true);
    await page.getByTestId("people-email").fill("");
    await page.getByTestId("people-submit").click();
    ck("no email: asks for one", await page.getByText("Enter an email address.").isVisible());
    await ctx.close();

    // ---- a read key
    const r = await open(alice.json.secret);
    ck("a read key: no form, and the page says why", (await r.page.getByTestId("people-add").count()) === 0
       && (await r.page.getByTestId("people-add-text").innerText()).trim() === "Only an admin can add people to this cell.");
    ck("a read key: the list still shows", (await r.page.getByTestId("person-row").count()) >= 4);
    await r.ctx.close();

    // ---- phone width
    const n = await open(bob.json.secret, 390);
    const over = await n.page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    ck("390: no sideways scroll", over <= 0, String(over));
    if (SHOTS) await n.page.screenshot({ path: path.join(SHOTS, "factory-people-admin-390.png"), fullPage: true });
    await n.ctx.close();

    // ---- a cell with no licence: only you, the licence line, nothing to add with
    BASE = bare.base;
    {
      const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
      const eeRequests = [];
      ctx.on("request", (rq) => { if (EE_CHUNK.test(rq.url())) eeRequests.push(rq.url()); });
      const page = await ctx.newPage();
      await page.goto(`${BASE}/people`);
      await page.getByTestId("people-list").waitFor({ timeout: 15000 });
      await page.getByTestId("licence-needed").waitFor({ timeout: 15000 });
      ck("no licence: /people lists only you", (await page.getByTestId("person-row").count()) === 1
         && /\(you\)/.test(await page.getByTestId("person-row").first().innerText()));
      ck("no licence: the licence line, and no add form", await page.getByTestId("licence-needed").isVisible()
         && (await page.getByTestId("people-email").count()) === 0 && (await page.getByTestId("people-add").count()) === 0);
      await checkWords(page, ck, "no licence");
      if (SHOTS) await page.screenshot({ path: path.join(SHOTS, "factory-people-nolicence-1440.png"), fullPage: true });
      await page.goto(`${BASE}/settings?tab=access`);
      await page.getByRole("heading", { name: "People" }).waitFor({ timeout: 15000 });
      await page.getByTestId("you-row").waitFor({ timeout: 15000 });
      ck("no licence: Settings, Access shows you and the licence line", (await page.getByTestId("you-row").count()) === 1
         && await page.getByTestId("licence-needed").first().isVisible());
      ck("no licence: no Add person button", (await page.getByRole("button", { name: "Add person" }).count()) === 0);
      if (SHOTS) await page.screenshot({ path: path.join(SHOTS, "settings-access-nolicence-1440.png"), fullPage: true });
      await page.getByRole("button", { name: "Create key" }).first().click();
      await page.getByRole("dialog", { name: "Create API key" }).waitFor({ timeout: 10000 });
      ck("no licence: the key dialog has no person picker", (await page.getByRole("dialog").getByText("whose agent or script this is").count()) === 0);
      ck("no licence: the ee chunk was never requested", eeRequests.length === 0, eeRequests.join(" "));
      await ctx.close();
      const m = await browser.newContext({ viewport: { width: 390, height: 900 } });
      const mp = await m.newPage();
      await mp.goto(`${BASE}/people`);
      await mp.getByTestId("licence-needed").waitFor({ timeout: 15000 });
      const mo = await mp.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
      ck("no licence, 390: no sideways scroll", mo <= 0, String(mo));
      if (SHOTS) await mp.screenshot({ path: path.join(SHOTS, "factory-people-nolicence-390.png"), fullPage: true });
      await m.close();
    }
  } finally {
    await browser.close();
    d.stop(); bare.stop();
  }
  console.log(`\n${pass} passed, ${fail} failed`);
  process.exit(fail ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(1); });
