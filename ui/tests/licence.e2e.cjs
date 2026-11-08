/*
 * Browser e2e for TR-1050: Settings, Enterprise licence (the panel an admin pastes a key into) and the banner
 * above every page when the licence ended or the cell has more people than it covers.
 *
 * Scratch cells start through tests/licensed.cjs (the test licence root is trusted in that process only).
 * Ports are TARES_E2E_PORT (default 8940) and the ones after it (daemon + MCP, two per cell); the suite stops
 * only those.
 *   - none (factory, auth on): the panel says None. An admin pastes a garbled key and sees the reason, pastes
 *     a valid key and sees the org and seats ("Saved here"), then Remove brings back None. The key text is never
 *     on the page. A person with a read key sees the facts and no textarea.
 *   - expired (factory): the banner is on / and on /settings/advanced; its link opens the licence tab.
 *   - expired (platform): the banner is on / and on /settings?tab=licence.
 *   - over seats (factory): the over-seats banner says how many people and how many seats.
 *   - key in the environment (valid, then not valid): the panel says where the key comes from, shows the reason
 *     for a key that does not check out, and says the environment wins.
 * Not covered: the sre edition's banner (same Shell as factory, same link) and a cell with no auth reading
 * "Only an admin" (an open cell has no non-admin).
 * Set SHOTS_DIR to keep screenshots at 1440 and 390 px: licence-<state>-<width>.png for none, valid, expired,
 * over_seats, env-invalid and source-env, and licence-banner-<expired|over_seats>-<width>.png.
 * Run:  cd ui && npm run build && TARES_E2E_PORT=9640 node tests/licence.e2e.cjs
 */
const fs = require("fs");
const os = require("os");
const path = require("path");
const { execFileSync } = require("child_process");

const UI_DIR = path.resolve(__dirname, "..");
const REPO = path.resolve(UI_DIR, "..");
const { chromium } = require(path.join(UI_DIR, "node_modules", "playwright"));
const { startDaemon } = require("./licensed.cjs");
const { checkWords } = require("./words.cjs");

const PORT = Number(process.env.TARES_E2E_PORT || 8940);
const SHOTS = process.env.SHOTS_DIR;
const TOKEN = "licence-e2e-admin-token";
const WIDTHS = [1440, 390];
const GARBLED = "this is not a licence key, only some words";

let pass = 0, fail = 0;
const ck = (label, cond, detail = "") => {
  cond ? pass++ : fail++;
  console.log((cond ? "  ok   " : "  FAIL ") + label + (cond ? "" : `   ${detail}`));
};

function python() {
  if (process.env.TARES_E2E_PY) return process.env.TARES_E2E_PY;
  const venv = path.join(REPO, ".venv/bin/python");
  return fs.existsSync(venv) ? venv : "python3";
}

// A licence the test root signed (5 seats). It checks out only on a daemon from licensed.cjs.
function testKey() {
  return execFileSync(python(), ["-c", "import licensed; print(licensed.test_licence())"],
    { cwd: REPO, encoding: "utf8", env: { ...process.env, PYTHONPATH: path.join(REPO, "ee", "tests") + path.delimiter + REPO } }).trim().split("\n").pop();
}

const tmp = (s) => fs.mkdtempSync(path.join(os.tmpdir(), `tares-licence-${s}-`));
const gone = async (base) => { for (let i = 0; i < 50; i++) { try { await fetch(`${base}/health`); } catch { return true; } await new Promise((r) => setTimeout(r, 200)); } return false; };

(async () => {
  const browser = await chromium.launch(process.env.TARES_E2E_CHROME ? { executablePath: process.env.TARES_E2E_CHROME } : {});
  const daemons = [];
  let nextPort = PORT;
  const start = async (opts) => { const d = await startDaemon({ port: nextPort, ...opts }); nextPort += 2; daemons.push(d); return d; };
  const api = async (d, p, { method = "GET", body, token = TOKEN } = {}) => {
    const r = await fetch(d.base + p, { method, headers: { "content-type": "application/json", ...(token ? { authorization: `Bearer ${token}` } : {}) },
                                        body: body ? JSON.stringify(body) : undefined });
    return { status: r.status, body: await r.json().catch(() => ({})) };
  };
  const open = async (d, p, width, token) => {
    const ctx = await browser.newContext({ viewport: { width, height: width < 600 ? 844 : 900 } });
    if (token) await ctx.addInitScript((t) => { try { localStorage.setItem("tares_token", t); } catch { /* blocked */ } }, token);
    const page = await ctx.newPage();
    const errors = [];
    page.on("pageerror", (e) => errors.push(String(e)));
    await page.goto(d.base + p, { waitUntil: "load" });
    return { ctx, page, errors };
  };
  const overflow = (page) => page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  const shot = async (page, name, width) => {
    if (SHOTS) { fs.mkdirSync(SHOTS, { recursive: true }); await page.screenshot({ path: path.join(SHOTS, `${name}-${width}.png`), fullPage: true }); }
  };
  const settled = async (page) => { await page.waitForLoadState("networkidle"); await page.waitForTimeout(400); };
  // The banner's words, or "" when it never shows (the check then fails, and the rest of that page is skipped).
  const banner = async (page) => {
    await page.getByTestId("licence-banner").waitFor({ timeout: 8000 }).catch(() => {});
    await settled(page);
    return (await page.getByTestId("licence-banner").count()) ? (await page.getByTestId("licence-banner").innerText()).trim() : "";
  };
  const text = (page, id) => page.getByTestId(id).innerText().then((s) => s.trim());
  const KEY = testKey();
  const bits = [KEY.slice(0, 40), KEY.slice(-40), KEY.slice(Math.floor(KEY.length / 2), Math.floor(KEY.length / 2) + 40)];
  const keyOnPage = async (page) => {   // what a person can see or select: text, markup, field values
    const seen = await page.evaluate(() => document.documentElement.outerHTML + "\n" + document.body.innerText + "\n"
      + [...document.querySelectorAll("textarea,input")].map((e) => e.value).join("\n"));
    return bits.some((b) => seen.includes(b));
  };

  try {
    // ── a cell with no licence, auth on: the admin's flow, and a person who only reads ──
    console.log("== none ==");
    const none = await start({ dataDir: tmp("none"), edition: "factory", licence: "none", auth: TOKEN });
    {
      const t = (s) => `none: ${s}`;
      const { ctx, page, errors } = await open(none, "/settings/advanced?tab=licence", 1440, TOKEN);
      await page.getByTestId("licence-panel").waitFor({ timeout: 15000 });
      await page.getByTestId("licence-key").waitFor({ timeout: 15000 });
      await settled(page);
      ck(t("the tab opens from ?tab=licence and the panel is there"), (await page.getByTestId("licence-panel").count()) === 1);
      ck(t("the key comes from: None"), (await text(page, "licence-source")) === "None", await text(page, "licence-source"));
      ck(t("it says the cell has no enterprise licence"), /no enterprise licence/.test(await text(page, "licence-none")));
      ck(t("an admin sees the box, Save, and no Remove"), (await page.getByTestId("licence-key").count()) === 1
         && (await page.getByTestId("licence-save").count()) === 1 && (await page.getByTestId("licence-remove").count()) === 0);
      ck(t("no banner on a cell with no licence"), (await page.getByTestId("licence-banner").count()) === 0);
      ck(t("Save waits for a key"), await page.getByTestId("licence-save").isDisabled());
      await checkWords(page, ck, t("panel"));
      for (const w of WIDTHS) { await page.setViewportSize({ width: w, height: w < 600 ? 844 : 900 }); await page.waitForTimeout(150);
        ck(t(`no sideways scroll at ${w}`), (await overflow(page)) <= 0, `${await overflow(page)}px over`); await shot(page, "licence-none", w); }
      await page.setViewportSize({ width: 1440, height: 900 });

      // a garbled key: refused, with its reason, and nothing is stored
      await page.getByTestId("licence-key").fill(GARBLED);
      await page.getByTestId("licence-save").click();
      await page.getByTestId("licence-error").waitFor({ timeout: 10000 });
      const reason = await text(page, "licence-error");
      ck(t("a garbled key shows the reason the cell gave"), reason.length > 0 && !reason.includes(GARBLED), reason);
      const lic1 = await api(none, "/api/licence");
      ck(t("and nothing was stored"), lic1.body.stored === false && lic1.body.source === "none", JSON.stringify(lic1.body));
      ck(t("the source still reads None"), (await text(page, "licence-source")) === "None");

      // a valid key: org and seats show, the key text does not
      await page.getByTestId("licence-key").fill(KEY);
      await page.getByTestId("licence-save").click();
      await page.getByTestId("licence-org").waitFor({ timeout: 10000 });
      await page.waitForFunction(() => document.querySelector('[data-testid="licence-source"]')?.textContent.trim() === "Saved here", null, { timeout: 10000 });
      ck(t("a valid key shows the organisation"), (await text(page, "licence-org")) === "Test Org", await text(page, "licence-org"));
      ck(t("and the seats"), (await text(page, "licence-seats")) === "5", await text(page, "licence-seats"));
      ck(t("and the people in use"), /^\d+$/.test(await text(page, "licence-used")), await text(page, "licence-used"));
      ck(t("and when it expires, with the days left"), /^\d{1,2} [A-Z][a-z]{2} \d{4} \(\d+ days left\)$/.test(await text(page, "licence-expires")), await text(page, "licence-expires"));
      ck(t("the key comes from: Saved here"), (await text(page, "licence-source")) === "Saved here");
      ck(t("the box is empty again and Remove is there"), (await page.getByTestId("licence-key").inputValue()) === "" && (await page.getByTestId("licence-remove").count()) === 1);
      ck(t("the garbled error is gone"), (await page.getByTestId("licence-error").count()) === 0);
      ck(t("the key text is nowhere on the page"), !(await keyOnPage(page)));
      await checkWords(page, ck, t("valid panel"));
      const lic2 = await api(none, "/api/licence");
      ck(t("the cell agrees, and its answer never carries the key"), lic2.body.valid === true && lic2.body.org === "Test Org" && !JSON.stringify(lic2.body).includes(bits[0]), JSON.stringify(lic2.body));
      await ctx.close();

      for (const w of WIDTHS) {
        const v = await open(none, "/settings/advanced?tab=licence", w, TOKEN);
        await v.page.getByTestId("licence-org").waitFor({ timeout: 15000 }); await settled(v.page);
        ck(t(`valid, no sideways scroll at ${w}`), (await overflow(v.page)) <= 0, `${await overflow(v.page)}px over`);
        ck(t(`valid, the key text is nowhere on the page at ${w}`), !(await keyOnPage(v.page)));
        await shot(v.page, "licence-valid", w);
        await v.ctx.close();
      }

      // Remove brings back None
      const again = await open(none, "/settings/advanced?tab=licence", 1440, TOKEN);
      await again.page.getByTestId("licence-remove").waitFor({ timeout: 15000 });
      await again.page.getByTestId("licence-remove").click();
      await again.page.waitForFunction(() => document.querySelector('[data-testid="licence-source"]')?.textContent.trim() === "None", null, { timeout: 10000 });
      ck(t("Remove brings back None"), (await text(again.page, "licence-source")) === "None" && (await again.page.getByTestId("licence-remove").count()) === 0);
      ck(t("and the organisation is gone"), (await again.page.getByTestId("licence-org").count()) === 0);
      const lic3 = await api(none, "/api/licence");
      ck(t("the cell agrees"), lic3.body.valid === false && lic3.body.stored === false, JSON.stringify(lic3.body));
      ck(t("no page error"), errors.length === 0 && again.errors.length === 0, errors.concat(again.errors).join(" "));
      await again.ctx.close();
    }
    {   // a person with a read key: the facts, no box
      const t = (s) => `read key: ${s}`;
      await api(none, "/api/licence", { method: "PUT", body: { key: KEY } });
      const made = await api(none, "/api/keys", { method: "POST", body: { name: "reader", scopes: ["read"] } });
      ck(t("a read key was made"), made.status === 201 && !!made.body.secret, JSON.stringify(made.body).slice(0, 120));
      const { ctx, page } = await open(none, "/settings/advanced?tab=licence", 1440, made.body.secret);
      await page.getByTestId("licence-panel").waitFor({ timeout: 15000 });
      await page.getByTestId("licence-admin-only").waitFor({ timeout: 15000 });
      await settled(page);
      ck(t("the organisation and the source show"), (await text(page, "licence-org")) === "Test Org" && (await text(page, "licence-source")) === "Saved here");
      ck(t("there is no box, no Save and no Remove"), (await page.getByTestId("licence-key").count()) === 0
         && (await page.getByTestId("licence-save").count()) === 0 && (await page.getByTestId("licence-remove").count()) === 0);
      ck(t("it says only an admin can change the licence"), /Only an admin/.test(await text(page, "licence-admin-only")));
      await ctx.close();
      await api(none, "/api/licence", { method: "DELETE" });
    }

    // ── expired, factory edition ──
    console.log("== expired ==");
    const expired = await start({ dataDir: tmp("expired"), edition: "factory", licence: "expired" });
    {
      const t = (s) => `expired: ${s}`;
      const st = (await api(expired, "/api/licence", { token: null })).body;
      ck(t("the cell says valid and expired"), st.valid === true && st.expired === true, JSON.stringify(st));
      const ended = new Date(st.expires + "T00:00:00Z");
      const date = `${ended.getUTCDate()} ${["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"][ended.getUTCMonth()]} ${ended.getUTCFullYear()}`;
      for (const w of WIDTHS) {
        for (const p of ["/", "/settings/advanced"]) {
          const { ctx, page, errors } = await open(expired, p, w);
          const words = await banner(page);
          ck(t(`${p} @${w}: the banner says when it ended and that people are paused`),
             words.includes(`The enterprise licence ended on ${date}. Everything keeps working; adding people is paused until it is renewed.`), words);
          if (!words) { await ctx.close(); continue; }
          ck(t(`${p} @${w}: one banner, no page error, no sideways scroll`), (await page.getByTestId("licence-banner").count()) === 1 && errors.length === 0 && (await overflow(page)) <= 0,
             `${await page.getByTestId("licence-banner").count()} banners, ${errors.join(" ")}, ${await overflow(page)}px over`);
          if (p === "/") { await checkWords(page, ck, t(`banner @${w}`)); await shot(page, "licence-banner-expired", w); }
          else {
            ck(t(`@${w}: its link goes to the licence tab`), (await page.getByTestId("licence-banner-link").getAttribute("href")) === "/settings/advanced?tab=licence");
            await page.getByTestId("licence-banner-link").click();
            await page.getByTestId("licence-panel").waitFor({ timeout: 15000 });
            await page.getByTestId("licence-expires").waitFor({ timeout: 15000 });
            ck(t(`@${w}: the link opens the panel, which says it ended`), /ended/.test(await text(page, "licence-expires")) && /Ended/.test(await page.getByTestId("licence-panel").innerText()),
               await text(page, "licence-expires"));
            await settled(page);
            ck(t(`@${w}: the panel has no sideways scroll`), (await overflow(page)) <= 0, `${await overflow(page)}px over`);
            await shot(page, "licence-expired", w);
          }
          await ctx.close();
        }
      }
    }

    // ── expired, platform edition ──
    console.log("== expired, platform ==");
    const plat = await start({ dataDir: tmp("platform"), edition: "platform", licence: "expired" });
    {
      const t = (s) => `platform: ${s}`;
      for (const w of WIDTHS) {
        for (const p of ["/", "/settings?tab=licence"]) {
          const { ctx, page, errors } = await open(plat, p, w);
          const words = await banner(page);
          ck(t(`${p} @${w}: the banner is there, once`), (await page.getByTestId("licence-banner").count()) === 1 && /licence ended on/.test(words), words);
          if (!words) { await ctx.close(); continue; }
          ck(t(`${p} @${w}: its link goes to /settings?tab=licence`), (await page.getByTestId("licence-banner-link").getAttribute("href")) === "/settings?tab=licence");
          if (p !== "/") {
            await page.getByTestId("licence-panel").waitFor({ timeout: 15000 });
            ck(t(`@${w}: the Enterprise licence tab shows the panel`), (await page.getByTestId("licence-org").count()) === 1);
          } else {
            await page.getByTestId("licence-banner-link").click();
            await page.getByTestId("licence-panel").waitFor({ timeout: 15000 });
            ck(t(`@${w}: the banner link opens the panel`), new URL(page.url()).pathname === "/settings" && new URL(page.url()).searchParams.get("tab") === "licence", page.url());
          }
          ck(t(`${p} @${w}: no page error, no sideways scroll`), errors.length === 0 && (await overflow(page)) <= 0, `${errors.join(" ")} ${await overflow(page)}px over`);
          await ctx.close();
        }
      }
    }

    // ── over seats: seeded under a 5-seat licence, then started with a 1-seat one ──
    console.log("== over seats ==");
    {
      const t = (s) => `over seats: ${s}`;
      const dir = tmp("over");
      const seeding = await start({ dataDir: dir, edition: "factory", licence: "valid", auth: TOKEN });
      for (const [name, email] of [["Kim Lee", "kim@example.com"], ["Sam Roe", "sam@example.com"]]) {
        const r = await api(seeding, "/api/people", { method: "POST", body: { name, email } });
        ck(t(`${name} was added under a licence with room`), r.status === 201, JSON.stringify(r.body));
      }
      seeding.stop();
      ck(t("the seeding cell stopped"), await gone(seeding.base));
      await new Promise((r) => setTimeout(r, 500));
      const over = await start({ dataDir: dir, edition: "factory", licence: "over_seats", auth: TOKEN });
      const st = (await api(over, "/api/licence")).body;
      ck(t("the cell has more people than seats"), st.valid === true && st.over_seats === true && st.used > st.seats, JSON.stringify(st));
      for (const w of WIDTHS) {
        for (const p of ["/", "/settings/advanced"]) {
          const { ctx, page, errors } = await open(over, p, w, TOKEN);
          const words = await banner(page);
          ck(t(`${p} @${w}: the banner counts people and seats`), words.includes(`This cell has ${st.used} people and the licence covers ${st.seats}. Adding people is paused.`), words);
          if (!words) { await ctx.close(); continue; }
          ck(t(`${p} @${w}: it is not the expired wording`), !/ended on/.test(words));
          ck(t(`${p} @${w}: no page error, no sideways scroll`), errors.length === 0 && (await overflow(page)) <= 0, `${errors.join(" ")} ${await overflow(page)}px over`);
          if (p === "/") { await checkWords(page, ck, t(`banner @${w}`)); await shot(page, "licence-banner-over_seats", w); }
          else {
            await page.getByTestId("licence-banner-link").click();
            await page.getByTestId("licence-org").waitFor({ timeout: 15000 });
            ck(t(`@${w}: the panel shows more people in use than seats`), Number(await text(page, "licence-used")) > Number(await text(page, "licence-seats")));
            await settled(page);
            await shot(page, "licence-over_seats", w);
          }
          await ctx.close();
        }
      }
    }

    // ── a key in the environment ──
    console.log("== environment ==");
    const envOk = await start({ dataDir: tmp("envok"), edition: "factory", licence: "valid", auth: TOKEN });
    const envBad = await start({ dataDir: tmp("envbad"), edition: "factory", licence: "invalid", auth: TOKEN });
    for (const [d, state, ok] of [[envOk, "source-env", true], [envBad, "env-invalid", false]]) {
      const t = (s) => `${state}: ${s}`;
      for (const w of WIDTHS) {
        const { ctx, page, errors } = await open(d, "/settings/advanced?tab=licence", w, TOKEN);
        await page.getByTestId("licence-panel").waitFor({ timeout: 15000 }).catch(async (e) => { console.log(page.url(), (await page.locator("body").innerText()).slice(0, 400), d.base, errors.join(" ")); throw e; });
        await page.getByTestId("licence-source").waitFor({ timeout: 15000 });
        await settled(page);
        ck(t(`@${w}: the key comes from the environment`), (await text(page, "licence-source")) === "Set in the environment (TARES_LICENSE)", await text(page, "licence-source"));
        ck(t(`@${w}: it says the environment key wins`), /wins over one saved here/.test(await text(page, "licence-env-note")));
        if (ok) ck(t(`@${w}: the organisation shows`), (await text(page, "licence-org")) === "Test Org");
        else {
          const reason = await text(page, "licence-reason");
          ck(t(`@${w}: the reason the key does not check out shows`), reason.length > 0, reason);
          ck(t(`@${w}: no organisation, and it says the key is not valid`), (await page.getByTestId("licence-org").count()) === 0 && /not valid/.test(await text(page, "licence-none")));
          ck(t(`@${w}: the key text is not on the page`), !(await page.evaluate(() => document.documentElement.outerHTML)).includes("this-is-not-a-licence-key"));
        }
        ck(t(`@${w}: no banner (not expired, not over seats), no page error, no sideways scroll`),
           (await page.getByTestId("licence-banner").count()) === 0 && errors.length === 0 && (await overflow(page)) <= 0, `${errors.join(" ")} ${await overflow(page)}px over`);
        if (w === 1440) await checkWords(page, ck, t("panel"));
        await shot(page, `licence-${state}`, w);
        await ctx.close();
      }
    }
    {   // a key saved while the environment has one: it is kept, and the note says which one is in use
      const t = (s) => `source-env save: ${s}`;
      const { ctx, page } = await open(envOk, "/settings/advanced?tab=licence", 1440, TOKEN);
      await page.getByTestId("licence-key").waitFor({ timeout: 15000 });
      await page.getByTestId("licence-key").fill(KEY);
      await page.getByTestId("licence-save").click();
      await page.getByTestId("licence-note").waitFor({ timeout: 10000 });
      ck(t("the note says the environment key is still the one in use"), /environment is still the one in use/.test(await text(page, "licence-note")), await text(page, "licence-note"));
      ck(t("the source still reads the environment"), (await text(page, "licence-source")) === "Set in the environment (TARES_LICENSE)");
      await page.getByTestId("licence-remove").click();
      await page.waitForFunction(() => !document.querySelector('[data-testid="licence-remove"]'), null, { timeout: 10000 });
      ck(t("Remove takes the saved key away and leaves the environment's"), (await api(envOk, "/api/licence")).body.stored === false);
      await ctx.close();
    }
  } finally {
    await browser.close().catch(() => {});
    for (const d of daemons) d.stop();
  }
  console.log(`\n${pass} passed, ${fail} failed`);
  process.exit(fail ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(1); });
