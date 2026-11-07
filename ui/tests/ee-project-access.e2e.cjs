/*
 * Browser e2e for TR-928: project access and the Areas settings are enterprise screens. They live in
 * src/ee and reach the console through slots, only on a cell whose licence allows enterprise.
 *
 * Scratch daemons start through tests/licensed.cjs (the test licence root is trusted in that process
 * only), each in a fresh data directory, on TARES_E2E_PORT (default 8866) and the ports after it
 * (daemon + MCP, two each), and the suite stops only those.
 *   - licensed platform cell, two people, one project: the Team/Private switch, Change lead and the
 *     Members view (add and remove a member) work, and Settings, Areas lists a repo and sets its owner.
 *   - the same cell after its licence is gone: no switch or lead on the project page, a private project
 *     still shows "Private", the Members and Areas addresses show the licence page, Members and Areas
 *     are not in the navigation, and no ee chunk is ever requested.
 * Run:  cd ui && npm run build && TARES_E2E_PORT=9440 node tests/ee-project-access.e2e.cjs
 */
const fs = require("fs");
const http = require("http");
const os = require("os");
const path = require("path");

const UI_DIR = path.resolve(__dirname, "..");
const { chromium } = require(path.join(UI_DIR, "node_modules", "playwright"));
const { startDaemon } = require("./licensed.cjs");

const PORT = Number(process.env.TARES_E2E_PORT || 8866);
const EE_CHUNK = /\/assets\/ee-[^/]*\.js(\?|$)/;

let pass = 0, fail = 0;
const ck = (label, cond, detail = "") => {
  cond ? pass++ : fail++;
  console.log((cond ? "  ok   " : "  FAIL ") + label + (cond ? "" : `   ${detail}`));
};

const gone = (url) => new Promise((res) => http.get(url, (r) => { r.resume(); res(false); }).on("error", () => res(true)));
async function stopAndWait(d) {
  d.stop();
  for (let i = 0; i < 100 && !(await gone(`${d.base}/health`)); i++) await new Promise((r) => setTimeout(r, 200));
  await new Promise((r) => setTimeout(r, 500));   // the database file is released
}

async function post(base, p, body, method = "POST") {
  const r = await fetch(`${base}${p}`, { method, headers: { "content-type": "application/json" }, body: JSON.stringify(body) });
  if (!r.ok) throw new Error(`${method} ${p}: ${r.status} ${await r.text()}`);
  return r.json();
}

(async () => {
  const daemons = [];
  let browser;
  const dataDir = fs.mkdtempSync(path.join(os.tmpdir(), "tares-ee-access-e2e-"));
  try {
    browser = await chromium.launch(process.env.TARES_E2E_CHROME ? { executablePath: process.env.TARES_E2E_CHROME } : {});
    const newPage = async (width = 1440) => {
      const ctx = await browser.newContext({ viewport: { width, height: 900 } });
      const page = await ctx.newPage();
      page.eeRequests = []; page.errors = [];
      page.on("request", (r) => { if (EE_CHUNK.test(r.url())) page.eeRequests.push(r.url()); });
      page.on("pageerror", (e) => page.errors.push(String(e)));
      return page;
    };

    // ── a licensed platform cell: two people (You and Kim) and a project You lead ──
    const lic = await startDaemon({ port: PORT, dataDir, edition: "platform", licence: "valid" });
    daemons.push(lic);
    await post(lic.base, "/api/people", { name: "Kim Lee", email: "kim@example.com" });
    const open = await post(lic.base, "/api/projects", { template: "custom", name: "Open Shop", objects: [] });
    const priv = await post(lic.base, "/api/projects", { template: "custom", name: "Quiet Shop", objects: [] });
    await post(lic.base, `/api/projects/${priv.id}`, { scope: "private" }, "PATCH");

    {
      const page = await newPage();
      await page.goto(`${lic.base}/projects/${open.id}`);
      const group = page.getByRole("group", { name: "Who can open this project" });
      await group.waitFor({ timeout: 15000 });
      ck("licensed: the project page shows the lead", /Lead:\s*You/.test(await page.locator(".acc").first().innerText()), await page.locator(".acc").first().innerText());
      ck("licensed: Team is on", (await group.getByRole("button", { name: "Team" }).getAttribute("aria-pressed")) === "true");
      await group.getByRole("button", { name: "Private" }).click();
      await page.getByText("Private: only its lead and members can open this project.").waitFor({ timeout: 10000 });
      ck("licensed: the switch makes it private", (await group.getByRole("button", { name: "Private" }).getAttribute("aria-pressed")) === "true");
      ck("licensed: the daemon agrees", (await (await fetch(`${lic.base}/api/projects/${open.id}`)).json()).scope === "private");
      await group.getByRole("button", { name: "Team" }).click();
      await page.getByText("Team: everyone on this cell can open this project.").waitFor({ timeout: 10000 });

      // Change lead
      await page.getByRole("button", { name: "Change lead" }).click();
      await page.getByRole("button", { name: "New lead" }).click();
      await page.getByRole("option", { name: "Kim Lee", exact: true }).click();
      await page.getByRole("button", { name: "Make lead" }).click();
      await page.getByText("Kim Lee", { exact: false }).first().waitFor({ timeout: 10000 });
      await page.waitForFunction(() => /Lead:\s*Kim Lee/.test(document.body.innerText), null, { timeout: 10000 });
      ck("licensed: Change lead hands the project to Kim", (await (await fetch(`${lic.base}/api/projects/${open.id}`)).json()).lead?.name === "Kim Lee");
      ck("licensed: no page error on the project page", page.errors.length === 0, page.errors.join(" | "));
      ck("licensed: the ee chunk was requested", page.eeRequests.length >= 1);
      await page.context().close();
    }

    {
      // Members: the lead (You) of the private project adds and removes Kim
      const page = await newPage();
      await page.goto(`${lic.base}/projects/${priv.id}?view=settings:members`);
      await page.getByRole("heading", { name: "Members" }).waitFor({ timeout: 15000 });
      ck("licensed: the Members address shows the page, not the licence line", (await page.getByTestId("licence-needed").count()) === 0);
      ck("licensed: Members is in the navigation", (await page.getByRole("link", { name: "Members" }).count()) >= 1);
      await page.getByRole("button", { name: "Person to add" }).click();
      await page.getByRole("option", { name: "Kim Lee", exact: true }).click();
      await page.getByRole("button", { name: "Add", exact: true }).click();
      await page.getByRole("row", { name: /Kim Lee/ }).waitFor({ timeout: 10000 });
      let members = (await (await fetch(`${lic.base}/api/projects/${priv.id}`)).json()).members.map((m) => m.name);
      ck("licensed: Add puts Kim on the project", members.includes("Kim Lee"), members.join());
      await page.getByRole("row", { name: /Kim Lee/ }).getByRole("button", { name: "Remove" }).click();
      await page.getByRole("row", { name: /Kim Lee/ }).waitFor({ state: "detached", timeout: 10000 });
      members = (await (await fetch(`${lic.base}/api/projects/${priv.id}`)).json()).members.map((m) => m.name);
      ck("licensed: Remove takes Kim off", !members.includes("Kim Lee"), members.join());
      await page.context().close();
    }

    {
      // Areas: list a repo and set its owner
      const page = await newPage();
      await post(lic.base, "/api/areas/acme/web", { person_id: "local" }, "PUT");
      await page.goto(`${lic.base}/settings?tab=areas`);
      await page.getByRole("heading", { name: "Areas" }).waitFor({ timeout: 15000 });
      await page.getByRole("row", { name: /acme\/web/ }).waitFor({ timeout: 10000 });
      ck("licensed: Areas lists the repo with its owner", /You/.test(await page.getByRole("row", { name: /acme\/web/ }).innerText()));
      ck("licensed: Areas is a tab in Settings", (await page.getByRole("button", { name: "Areas", exact: true }).count()) >= 1);
      await page.getByRole("row", { name: /acme\/web/ }).getByRole("button", { name: "Edit" }).click();
      await page.getByRole("button", { name: "Owner" }).click();
      await page.getByRole("option", { name: "Kim Lee", exact: true }).click();
      await page.getByRole("button", { name: "Save" }).click();
      await page.getByRole("row", { name: /acme\/web/ }).getByText("Kim Lee").waitFor({ timeout: 10000 });
      const areas = await (await fetch(`${lic.base}/api/areas`)).json();
      ck("licensed: the owner is set to Kim", areas.find((a) => a.repo === "acme/web")?.person.name === "Kim Lee", JSON.stringify(areas));
      ck("licensed: no page error on Areas", page.errors.length === 0, page.errors.join(" | "));
      await page.context().close();
    }

    {
      // Projects list: a private project the admin is not on (made up here, the cell has no second login)
      const page = await newPage();
      const stub = { id: "hid-1", name: "Hidden Shop", hidden: true, scope: "private", status: "active", lead: { id: "p-kim", name: "Sam Roe" }, objects: [] };
      await page.route("**/api/projects", async (route) => {
        if (route.request().method() !== "GET") return route.fallback();
        const res = await route.fetch();
        const body = await res.json();
        await route.fulfill({ response: res, json: { ...body, projects: [...body.projects, stub] } });
      });
      let patched;
      await page.route("**/api/projects/hid-1", (route) => {
        patched = route.request().postDataJSON();
        route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(stub) });
      });
      await page.goto(`${lic.base}/projects?show=all`);
      const row = page.locator("tr.proj-hidden");
      await row.waitFor({ timeout: 15000 });
      ck("licensed: the stub says who leads it", /Private, led by Sam Roe/.test(await row.innerText()));
      await row.getByRole("button", { name: "Change lead" }).click();
      await row.getByRole("button", { name: "New lead" }).click();
      await row.getByRole("option", { name: "Kim Lee", exact: true }).click();
      await row.getByRole("button", { name: "Make lead" }).click();
      await page.waitForFunction(() => document.querySelectorAll("tr.proj-hidden button").length > 0);
      ck("licensed: Change lead sends the new lead for the hidden project", patched && typeof patched.lead_person_id === "string" && patched.lead_person_id.length > 0, JSON.stringify(patched));
      ck("licensed: no page error on the Projects list", page.errors.length === 0, page.errors.join(" | "));
      await page.unrouteAll({ behavior: "ignoreErrors" });
      await page.context().close();
    }

    await stopAndWait(lic);

    // ── the same data without a licence ──
    const none = await startDaemon({ port: PORT + 2, dataDir, edition: "platform", licence: "none" });
    daemons.push(none);
    ck("no licence: the cell says ee is not allowed", (await (await fetch(`${none.base}/api/licence`)).json()).ee_allowed === false);

    {
      const page = await newPage();
      await page.goto(`${none.base}/projects/${priv.id}`);
      await page.locator("main").first().waitFor();
      await page.getByText("Private", { exact: true }).first().waitFor({ timeout: 15000 });
      ck("no licence: a private project still shows Private", (await page.locator(".acc-private").count()) === 1);
      ck("no licence: no Team/Private switch", (await page.getByRole("group", { name: "Who can open this project" }).count()) === 0);
      ck("no licence: no lead line and no Change lead", (await page.getByText(/Lead:/).count()) === 0 && (await page.getByRole("button", { name: "Change lead" }).count()) === 0);
      await page.goto(`${none.base}/projects/${open.id}`);
      await page.locator("main").first().waitFor();
      await page.waitForLoadState("networkidle");
      ck("no licence: a team project shows no access line at all", (await page.locator(".acc").count()) === 0);

      await page.goto(`${none.base}/projects/${priv.id}?view=settings:members`);
      await page.getByTestId("licence-needed").first().waitFor({ timeout: 15000 });
      ck("no licence: the Members address shows the licence page", /enterprise licence/.test(await page.getByTestId("licence-needed").first().innerText()));
      // the project's navigation is on this page, and Members is not in it
      ck("no licence: the project navigation is there", (await page.locator("nav.pnav").count()) >= 1 && (await page.locator("nav.pnav").getByText("Settings", { exact: true }).count()) >= 1);
      ck("no licence: Members is not in the navigation", (await page.locator("nav.pnav").getByRole("link", { name: "Members", exact: true }).count()) === 0);

      await page.goto(`${none.base}/settings?tab=areas`);
      await page.getByTestId("licence-needed").first().waitFor({ timeout: 15000 });
      ck("no licence: the Areas address shows the licence page", /enterprise licence/.test(await page.getByTestId("licence-needed").first().innerText()));
      ck("no licence: Areas is not a tab in Settings", (await page.getByRole("button", { name: "Areas", exact: true }).count()) === 0);

      // the Projects list: a private project one is not on keeps its stub, with no Change lead
      await page.goto(`${none.base}/projects`);
      await page.locator("main").first().waitFor();
      await page.waitForLoadState("networkidle");
      ck("no licence: the Projects list has no Change lead", (await page.getByRole("button", { name: "Change lead" }).count()) === 0);
      ck("no licence: no ee chunk requested on any of these pages", page.eeRequests.length === 0, page.eeRequests.join(" "));
      ck("no licence: no page error", page.errors.length === 0, page.errors.join(" | "));
      await page.context().close();
    }
  } catch (e) {
    ck("script ran to the end", false, String(e && e.stack || e));
  } finally {
    if (browser) await browser.close();
    for (const d of daemons) d.stop();
    fs.rmSync(dataDir, { recursive: true, force: true });
  }
  console.log(`\n${pass} passed, ${fail} failed`);
  process.exit(fail ? 1 : 0);
})();
