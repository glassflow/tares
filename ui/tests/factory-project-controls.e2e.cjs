/*
 * Browser e2e for TR-1079: the factory project page offers "Change the lead" and "Add a member" only on a
 * cell whose licence allows enterprise. Both actions answer 402 without one, so the page must not show them.
 *
 * Four scratch cells start through tests/licensed.cjs (the test licence root is trusted in that process
 * only), each on its own data directory seeded with tests/seed-factory-controls.py: one project, led by
 * the cell's owner, with three other people on the cell. Ports are TARES_E2E_PORT (default 8880) and the
 * ones after it (daemon + MCP, two per cell); the suite stops only those.
 *   - licensed: Change the lead and Add a member are on the page and work (the lead moves to Sam Roe, then
 *     Ana Ortiz is added as a member).
 *   - no licence: the page reads the same, with neither control, and no ee chunk is requested.
 *   - expired: an expired licence keeps working, so the controls stay.
 *   - over seats: the licence is still valid, so the controls stay (the console shows its banner).
 * Set SHOTS_DIR to keep a screenshot of every state at 1440 and 390 px (factory-project-controls-<state>-<width>.png).
 * Run:  cd ui && npm run build && TARES_E2E_PORT=9460 node tests/factory-project-controls.e2e.cjs
 */
const fs = require("fs");
const os = require("os");
const path = require("path");
const { execFileSync } = require("child_process");

const UI_DIR = path.resolve(__dirname, "..");
const REPO = path.resolve(UI_DIR, "..");
const { chromium } = require(path.join(UI_DIR, "node_modules", "playwright"));
const { startDaemon } = require("./licensed.cjs");

const PORT = Number(process.env.TARES_E2E_PORT || 8880);
const SHOTS = process.env.SHOTS_DIR;
const EE_CHUNK = /\/assets\/ee-[^/]*\.js(\?|$)/;
const THEME_MS = 600;
const STATES = ["valid", "none", "expired", "over_seats"];

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

function seed(dir) {
  return execFileSync(python(), [path.join(__dirname, "seed-factory-controls.py"), dir],
    { cwd: REPO, encoding: "utf8", env: { ...process.env, PYTHONPATH: REPO } }).trim().split("\n").pop();
}

(async () => {
  const browser = await chromium.launch(process.env.TARES_E2E_CHROME ? { executablePath: process.env.TARES_E2E_CHROME } : {});
  const daemons = [];
  try {
    for (const [i, state] of STATES.entries()) {
      console.log(`== ${state} ==`);
      const dataDir = fs.mkdtempSync(path.join(os.tmpdir(), `tares-project-controls-${state}-`));
      const uid = seed(dataDir);
      const d = await startDaemon({ port: PORT + i * 2, dataDir, edition: "factory", licence: state });
      daemons.push(d);
      const lic = await (await fetch(`${d.base}/api/licence`)).json();
      const allowed = lic.ee_allowed === true;
      ck(`${state}: the cell ${allowed ? "allows" : "does not allow"} ee`,
         allowed === (state !== "none"), JSON.stringify(lic));
      const page_ = await (await fetch(`${d.base}/api/factory/projects/${uid}`)).json();
      ck(`${state}: the page answer carries both flags as the licence says`,
         page_.people.can_set_lead === allowed && page_.people.can_add === allowed,
         JSON.stringify({ lead: page_.people.can_set_lead, add: page_.people.can_add, allowed }));

      for (const width of [1440, 390]) {
        const ctx = await browser.newContext({ viewport: { width, height: width < 600 ? 844 : 900 } });
        const page = await ctx.newPage();
        const chunks = [], errors = [];
        page.on("request", (r) => { if (EE_CHUNK.test(r.url())) chunks.push(r.url()); });
        page.on("pageerror", (e) => errors.push(String(e)));
        await page.goto(`${d.base}/projects/${uid}`);
        await page.getByTestId("project-people").waitFor({ timeout: 15000 });
        await page.waitForLoadState("networkidle");
        await page.waitForTimeout(THEME_MS);
        const text = await page.getByTestId("project-people").innerText();
        const lead = await page.getByTestId("people-set-lead").count();
        const add = await page.getByTestId("people-add-member").count();
        ck(`${state} ${width}: the page names the lead and the member`, /You/.test(text) && /Kim Lee/.test(text), text);
        ck(`${state} ${width}: Change the lead is ${allowed ? "on" : "off"} the page`, (lead > 0) === allowed, `lead=${lead}`);
        ck(`${state} ${width}: Add a member is ${allowed ? "on" : "off"} the page`, (add > 0) === allowed, `add=${add}`);
        if (state === "none") ck(`${state} ${width}: no ee chunk requested`, chunks.length === 0, chunks.join(" "));
        ck(`${state} ${width}: no page error`, errors.length === 0, errors.join(" "));
        ck(`${state} ${width}: no sideways scroll`,
           (await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)) <= 0);
        if (SHOTS) await page.screenshot({ path: path.join(SHOTS, `factory-project-controls-${state}-${width}.png`), fullPage: true });

        await ctx.close();
      }
      if (state === "valid") {   // last, since it moves the lead away from the person who looks
        const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
        const page = await ctx.newPage();
        await page.goto(`${d.base}/projects/${uid}`);
        await page.getByTestId("project-people").waitFor({ timeout: 15000 });
        await page.waitForLoadState("networkidle");
        await page.getByTestId("people-set-lead").first().click();
        await page.getByRole("option", { name: "Sam Roe" }).click();
        await page.waitForFunction(() => /Sam Roe/.test(document.querySelector('[data-testid="project-people"]').innerText), null, { timeout: 10000 });
        const now = await (await fetch(`${d.base}/api/projects/${uid}`)).json();
        ck("valid: Change the lead hands the project to Sam", now.lead && now.lead.name === "Sam Roe", JSON.stringify(now.lead));
        // Ana is on the cell but not on the project, so the picker offers her: Add a member adds her (TR-930 F3)
        const isAna = (m) => m.name === "Ana Ortiz";
        ck("valid: before Add a member, Ana is not a member", !(now.members || []).some(isAna), JSON.stringify(now.members));
        await page.getByTestId("people-add-member").first().click();
        await page.getByRole("option", { name: "Ana Ortiz" }).click();
        let after = now;
        for (let i = 0; i < 50 && !(after.members || []).some(isAna); i++) {
          await page.waitForTimeout(200);
          after = await (await fetch(`${d.base}/api/projects/${uid}`)).json();
        }
        ck("valid: Add a member puts Ana on the project", (after.members || []).some(isAna), JSON.stringify(after.members));
        await ctx.close();
      }
    }
  } finally {
    await browser.close().catch(() => {});
    for (const d of daemons) d.stop();
  }
  console.log(`\n${pass} passed, ${fail} failed`);
  process.exit(fail ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(1); });
