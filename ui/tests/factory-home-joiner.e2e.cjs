// TR-1723: Home for a person joining a factory someone else set up, on a licensed scratch daemon seeded by
// seed-factory-home-joiner.py: the welcome, what the team already set, the three steps with their done
// states, the team's crews, and the usual Home for everyone else. The states a real daemon can send but the
// seed cannot make are the real answer with one part changed on its way to the page.
//   node tests/factory-home-joiner.e2e.cjs     (needs ../ui/dist and the repo .venv; TARES_E2E_PORT for the port)
const fs = require("fs");
const os = require("os");
const path = require("path");
const { spawnSync } = require("child_process");

const { chromium } = require("./browser.cjs");
const { checkWords } = require("./words.cjs");
const { checkPaper } = require("./paper.cjs");
const { startDaemon } = require("./licensed.cjs");

const PORT = Number(process.env.TARES_E2E_PORT || 8973);
const ROOT = "root-token-factory-home-joiner-e2e";
const SHOTS = process.env.SHOTS_DIR;
const PY = process.env.TARES_E2E_PYTHON || "python3";
const TEAM_TEXT = "You approve each merge, 3 builders, no releases, second check on";
const COMMANDS = [["install", "uv tool install tares"], ["login", "tares login"], ["crew", "tares factory crew up"]];
const CLEAR = { clear: ["autonomy", "releaser", "builders_max", "challenger"] };
const TOO_LOOSE = "autonomy L4-ship is looser than the team's L3-review; a personal value can only be as strict or stricter. Clear yours to use the team's.";
const NOW_PRIVATE = "This project is private. Ask its lead, Robin Owner, to add you.";
const TEAM_WELCOME = "Welcome to your team’s factory";

let pass = 0, fail = 0;
const ck = (label, cond, detail = "") => {
  cond ? pass++ : fail++;
  console.log((cond ? "  ok   " : "  FAIL ") + label + (cond ? "" : `   ${detail}`));
};
const same = (label, got, want) => ck(label, JSON.stringify(got) === JSON.stringify(want), `got ${JSON.stringify(got)}`);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const pastCache = () => sleep(5600);   // the daemon keeps /api/home for 5 seconds per credential
const flat = async (loc) => (await loc.innerText()).replace(/\s+/g, " ").trim();
const overflow = (page) => page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
const appears = (loc, timeout = 10000) => loc.waitFor({ timeout }).then(() => true, () => false);
const goes = (loc, timeout = 10000) => loc.waitFor({ state: "detached", timeout }).then(() => true, () => false);

(async () => {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "tares-factory-home-joiner-e2e-"));
  let daemon, browser;
  const crashes = [];
  try {
    daemon = await startDaemon({ port: PORT, dataDir: home, edition: "factory", licence: "valid", seats: 50, auth: ROOT });
    const BASE = daemon.base;
    console.log(`  daemon: pid ${daemon.pid}, port ${PORT}`);
    const seeded = spawnSync(PY, [path.join(__dirname, "seed-factory-home-joiner.py"), String(PORT), ROOT], { encoding: "utf8" });
    ck("seed ran", seeded.status === 0, (seeded.stderr || "").slice(-300));
    if (seeded.status !== 0) throw new Error("no seed");
    const ids = JSON.parse(seeded.stdout.trim().split("\n").pop());

    const api = async (method, p, body, token = ROOT, type = "application/json") => {
      const r = await fetch(BASE + p, { method, headers: { "content-type": type, authorization: `Bearer ${token}` },
                                        body: body === undefined ? undefined : typeof body === "string" ? body : JSON.stringify(body) });
      return { status: r.status, body: await r.json().catch(() => ({})) };
    };
    /** The home the daemon answers a person now, past its own cache. */
    const answer = async (who) => { await pastCache(); return (await api("GET", "/api/home?projects=team", undefined, ids.keys[who])).body; };
    const stepOf = (h, key) => (h.start?.steps || []).find((s) => s.key === key) || {};

    browser = await chromium.launch(process.env.TARES_E2E_CHROME ? { executablePath: process.env.TARES_E2E_CHROME } : {});
    /** A signed-in page. `ctl.alter(home)` changes what the page reads; `ctl.hold` stalls its home reads. */
    const open = async (who, width = 1440) => {
      const ctx = await browser.newContext({ viewport: { width, height: width === 390 ? 844 : 900 },
                                             permissions: ["clipboard-read", "clipboard-write"] });
      await ctx.addInitScript((t) => { try { localStorage.setItem("tares_token", t); } catch { /* blocked */ } }, ids.keys[who]);
      const page = await ctx.newPage();
      const ctl = { alter: null, hold: null, sent: [] };
      page.on("pageerror", (e) => crashes.push(e.message));
      page.on("request", (req) => {
        const u = new URL(req.url());
        if (u.pathname.startsWith("/api/")) ctl.sent.push({ method: req.method(), path: u.pathname, search: u.search, body: req.postData() });
      });
      await page.route(/\/api\/home(\?|$)/, async (route) => {
        if (ctl.hold) await ctl.hold;
        if (!ctl.alter) return route.continue();
        const res = await route.fetch();
        const data = await res.json();
        ctl.alter(data);
        return route.fulfill({ response: res, json: data });
      });
      const reads = () => ctl.sent.filter((x) => x.method === "GET" && x.path === "/api/home").map((x) => x.search);
      const writes = (p) => ctl.sent.filter((x) => x.method !== "GET" && x.path === p).map((x) => ({ method: x.method, body: x.body ? JSON.parse(x.body) : null }));
      const go = async (at = "/", view = "joiner-start") => {
        await page.goto(BASE + at, { waitUntil: "load" });
        await page.getByTestId(view).waitFor();
        await page.waitForLoadState("networkidle");
      };
      const id = (t) => page.getByTestId(t);
      const step = (key) => page.locator(`[data-testid=joiner-step][data-key=${key}]`);
      const command = (key) => page.locator(`[data-testid=joiner-command][data-key=${key}]`);
      const project = (key) => page.locator(`[data-testid=joiner-project][data-id="${ids.projects[key]}"]`);
      const title = () => id("joiner-welcome").getByRole("heading", { level: 1 });
      return { ctx, page, ctl, reads, writes, go, id, step, command, project, title };
    };
    const shot = async (page, state, width) => {
      if (SHOTS) { fs.mkdirSync(SHOTS, { recursive: true }); await page.screenshot({ path: path.join(SHOTS, `factory-home-joiner-${state}-${width}.png`), fullPage: true }); }
    };

    // ── the owner's Home is what it was ──
    {
      const o = await open("robin");
      await o.go("/", "factory-home");
      ck("owner: the usual Home, no joiner view", (await o.id("joiner-start").count()) === 0 && (await o.id("joiner-welcome").count()) === 0);
      same("owner: one plain home read, as before", o.reads(), [""]);
      await checkWords(o.page, ck, "owner");
      const hid = await api("POST", "/api/home/start/hide", undefined, ids.keys.robin);
      ck("owner: their own steps can be hidden on the daemon", hid.status === 200 && (await answer("robin")).start?.hidden === true, JSON.stringify(hid));
      await o.go("/", "factory-home");
      ck("owner with steps hidden: Home offers nothing back, that is for a person joining", (await o.id("start-hidden").count()) === 0);
      await api("DELETE", "/api/home/start/hide", undefined, ids.keys.robin);
      await o.ctx.close();
    }

    // ── a member's first visit ──
    for (const width of [390, 1440]) {
      const t = (s) => `@${width}: ${s}`;
      const m = await open("sam", width);
      let release;
      const held = new Promise((r) => { release = r; });
      await m.page.route("**/api/factory/people", async (route) => { await held; await route.continue(); });
      await m.page.goto(`${BASE}/`, { waitUntil: "load" });
      await m.id("joiner-start").waitFor();
      ck(t("the welcome names nobody while the name is read"),
         (await m.id("joiner-welcome-loading").count()) === 1 && (await m.page.getByRole("heading", { name: /Welcome/ }).count()) === 0);
      release();
      await m.title().waitFor();
      same(t("welcome to the owner's factory"), await flat(m.title()), "Welcome to Robin Owner’s factory");
      await m.page.waitForLoadState("networkidle");
      same(t("a plain home read, then the one with the team's projects"), m.reads(), ["", "?projects=team"]);

      same(t("team strip: its heading"), await flat(m.id("joiner-team").getByRole("heading")), "Already set up by your team");
      same(t("team strip: the team's choices in words"), await flat(m.id("joiner-team-settings")), TEAM_TEXT);
      ck(t("team strip: no GitHub line when none is connected"), (await m.id("joiner-team-github").count()) === 0);

      same(t("three steps in order, none done"),
           await m.id("joiner-step").evaluateAll((els) => els.map((e) => [e.dataset.key, e.dataset.done])),
           [["settings", "false"], ["machine", "false"], ["project", "false"]]);
      for (const [i, key] of ["settings", "machine", "project"].entries())
        ck(t(`${key}: says it is step ${i + 1} of 3`), (await flat(m.step(key))).includes(`Step ${i + 1} of 3`), await flat(m.step(key)));
      ck(t("step 1: its title and what it gets"), (await flat(m.step("settings"))).includes("Choose how your crew works")
         && (await flat(m.step("settings"))).includes("Your team's choices, or stricter ones for your machine."), await flat(m.step("settings")));
      same(t("step 1: the team's choices are the default action"), await flat(m.id("joiner-use-team")), "Use the team’s choices");
      same(t("step 1: and a way to choose their own"), await flat(m.id("joiner-own")), "Choose my own");
      ck(t("step 1: the choices are closed"), (await m.id("choices-form").count()) === 0);

      same(t("step 2: three commands, none ticked"),
           await m.id("joiner-command").evaluateAll((els) => els.map((e) => [e.dataset.key, e.dataset.done])),
           COMMANDS.map(([k]) => [k, "false"]));
      for (const [key, text] of COMMANDS) {
        same(t(`step 2: ${key} shows its command`), await flat(m.command(key).locator("code")), text);
        const copy = m.command(key).getByTestId("start-copy");
        same(t(`step 2: ${key} has its copy button`), [await copy.getAttribute("aria-label"), await copy.getAttribute("title")], [`Copy ${text}`, "Copy"]);
      }
      if (width === 1440) {
        const copy = m.command("login").getByTestId("start-copy");
        await copy.click();
        ck(t("step 2: the button then reads Copied"), await appears(m.command("login").getByRole("button", { name: "Copied" }), 3000));
        same(t("step 2: and the command is on the clipboard"), await m.page.evaluate(() => navigator.clipboard.readText()), "tares login");
      }

      for (const key of ["checkout", "search"]) {
        same(t(`step 3: ${key} offers to join`), await flat(m.project(key).getByTestId("joiner-join")), "Join this project");
        ck(t(`step 3: ${key} names its lead`), (await flat(m.project(key))).includes("Led by Robin Owner"), await flat(m.project(key)));
      }
      same(t("step 3: the private project is a stub that says whom to ask"),
           await flat(m.id("joiner-hidden-project")), "A private project Ask Robin Owner");
      ck(t("step 3: and its name is nowhere on the page"), !(await m.page.locator("body").innerText()).includes("Payroll"));

      ck(t("nothing asks to connect GitHub or tickets"),
         (await m.page.locator('[data-testid=joiner-start] a[href^="/setup"], [data-testid=joiner-start] a[href^="/integrations"]').count()) === 0
         && (await m.id("joiner-start").getByRole("button", { name: /connect/i }).count()) === 0
         && (await m.id("joiner-start").getByRole("link", { name: /connect/i }).count()) === 0);
      ck(t("nothing waits, so no waiting section"), (await m.id("waiting-row").count()) === 0 && (await m.id("waiting-team").count()) === 0);
      ck(t("the team's crews stand beside the steps"), (await m.id("team-crews").count()) === 1
         && (await m.id("team-crew-row").filter({ hasText: "Robin" }).count()) >= 1);
      same(t("and nothing else stands there"), await m.page.locator("[data-testid=joiner-start] aside > *").count(), 1);
      await shot(m.page, "first", width);
      await checkWords(m.page, ck, t("first visit"));
      await checkPaper(m.page, ck, t("first visit"));
      ck(t("no sideways scroll"), (await overflow(m.page)) <= 0, String(await overflow(m.page)));
      await m.ctx.close();
    }

    // ── the welcome with no name to say ──
    const rename = (name) => async (route) => {
      const res = await route.fetch();
      const data = await res.json();
      data.people.find((p) => p.id === "local").name = name;
      await route.fulfill({ response: res, json: data });
    };
    for (const [label, handler] of [
      ["the people read fails", (route) => route.fulfill({ status: 500, contentType: "application/json", body: '{"detail":"down"}' })],
      ["the first person is still named Local", rename("Local")],
      ["the first person is still named You", rename("You")],
      ["the first person has an empty name", rename("")],
      ["the first person has no name", rename(null)],
    ]) {
      const m = await open("sam");
      await m.page.route("**/api/factory/people", handler);
      await m.go();
      await m.title().waitFor();
      same(`${label}: the team's factory`, await flat(m.title()), TEAM_WELCOME);
      await m.ctx.close();
    }

    // ── what a daemon may leave out ──
    const altered = async (label, alter, check, view = "joiner-start") => {
      const m = await open("sam");
      m.ctl.alter = alter;
      await m.go("/", view);
      await check(m, (s) => `${label}: ${s}`);
      await checkWords(m.page, ck, label);
      await m.ctx.close();
    };
    await altered("GitHub only", (h) => { if (h.start) h.start.team = { settings_text: "", github: "GitHub connected as example-org" }; }, async (m, t) => {
      same(t("the GitHub line"), await flat(m.id("joiner-team-github")), "GitHub connected as example-org");
      ck(t("no settings line"), (await m.id("joiner-team-settings").count()) === 0);
    });
    await altered("empty team words", (h) => { if (h.start) h.start.team = { settings_text: "", github: "" }; }, async (m, t) => {
      ck(t("no team strip, the steps stay"), (await m.id("joiner-team").count()) === 0 && (await m.id("joiner-step").count()) === 3);
    });
    await altered("no team words", (h) => { if (h.start) delete h.start.team; }, async (m, t) => {
      ck(t("no team strip, the steps stay"), (await m.id("joiner-team").count()) === 0 && (await m.id("joiner-step").count()) === 3);
    });
    await altered("no choices block", (h) => { delete h.onboarding; }, async (m, t) => {
      ck(t("the team's choices can be taken, with no form to open"),
         (await m.id("joiner-use-team").count()) === 1 && (await m.id("joiner-own").count()) === 0);
    });
    await altered("no choices block, from the link", (h) => { delete h.onboarding; }, async (m, t) => {
      await m.page.evaluate(() => { window.location.hash = "settings"; });
      ck(t("the link opens no form"), (await m.id("choices-form").count()) === 0 && (await m.id("joiner-use-team").count()) === 1);
    });
    await altered("no project", (h) => { h.projects = []; }, async (m, t) => {
      same(t("a plain empty state"), await flat(m.id("joiner-no-projects")), "No team project yet. One shows here once someone on the team starts it.");
      ck(t("nothing to join"), (await m.id("joiner-join").count()) === 0);
    });
    await altered("a stub with no lead", (h) => { for (const p of h.projects) if (p.hidden) p.lead = null; }, async (m, t) => {
      same(t("ask its lead"), await flat(m.id("joiner-hidden-project")), "A private project Ask its lead");
    });
    await altered("a project with nobody named", (h) => {
      for (const p of h.projects) if (!p.hidden) Object.assign(p, { lead: null, members: [], goal: "" });
    }, async (m, t) => {
      ck(t("no lead line"), !(await flat(m.project("checkout"))).includes("Led by"), await flat(m.project("checkout")));
      same(t("and it still offers to join"), await flat(m.project("checkout").getByTestId("joiner-join")), "Join this project");
    });
    await altered("a private project sent whole",(h) => { for (const p of h.projects) if (p.name === "Checkout" && h.start) p.scope = "private"; }, async (m, t) => {
      ck(t("says whom to ask and offers no join"), (await m.project("checkout").getByTestId("joiner-join").count()) === 0
         && (await flat(m.project("checkout").getByTestId("joiner-ask"))) === "Ask Robin Owner", await flat(m.project("checkout")));
    });
    await altered("no team keys", (h) => { delete h.summary; }, async (m, t) => {
      ck(t("no side column"), (await m.page.locator("[data-testid=joiner-start] aside").count()) === 0);
    });
    for (const [label, alter] of [
      ["no joiner block", (h) => { delete h.joiner; }],
      ["not joining", (h) => { if (h.joiner) h.joiner.joining = false; }],
      ["no start block", (h) => { delete h.start; }],
    ]) {
      await altered(label, alter, async (m, t) => {
        ck(t("the usual Home"), (await m.id("joiner-start").count()) === 0 && (await m.id("start-hidden").count()) === 0);
        same(t("read once, plainly"), m.reads(), [""]);
      }, "factory-home");
    }

    // ── /start#settings, the address the first step links to ──
    for (const width of [390, 1440]) {
      const t = (s) => `@${width}: ${s}`;
      const m = await open("sam", width);
      await m.go("/start#settings");
      ck(t("the link opens the choices"), await appears(m.id("choices-form")));
      const at = new URL(m.page.url());
      same(t("on Home, with its hash"), [at.pathname, at.hash], ["/", "#settings"]);
      same(t("the team's values are preselected"),
           [await m.id("choice-L3-review").isChecked(), await m.id("choice-L4-ship").isChecked(),
            await m.id("builders-count").innerText(), await m.id("toggle-releases").getAttribute("aria-checked")],
           [true, false, "3", "false"]);
      same(t("the form saves the person's own"), await flat(m.id("choices-save")), "Save my choices");
      await shot(m.page, "choices", width);
      await checkWords(m.page, ck, t("choices open"));
      ck(t("choices open: no sideways scroll"), (await overflow(m.page)) <= 0, String(await overflow(m.page)));
      await m.id("joiner-close").click();
      ck(t("Close puts them away"), await goes(m.id("choices-form")));
      await m.go("/start?item=none#settings");
      const kept = new URL(m.page.url());
      same(t("a query is kept too"), [kept.pathname, kept.search, kept.hash], ["/", "?item=none", "#settings"]);
      await m.go("/");
      ck(t("with no hash the choices stay closed"), (await m.id("choices-form").count()) === 0);
      await m.page.evaluate(() => { window.location.hash = "settings"; });
      ck(t("a hash that changes on the open page opens them"), await appears(m.id("choices-form")));
      await m.ctx.close();
    }

    // ── step 1 on the daemon ──
    {
      const m = await open("sam");
      await m.go();
      const put = () => m.writes("/api/crew/settings");
      const done = () => m.page.locator("[data-testid=joiner-step][data-key=settings][data-done=true]");
      await m.id("joiner-use-team").click();
      ck("step 1: taking the team's choices ticks it", await appears(done()));
      same("step 1: by clearing the person's own values", put(), [{ method: "PUT", body: CLEAR }]);
      let h = await answer("sam");
      same("step 1: the daemon has it done, on the team's values",
           [stepOf(h, "settings").done, h.onboarding.choices.autonomy, h.onboarding.choices.builders_max], [true, "L3-review", 3]);
      ck("step 1 done: says so, and offers a change instead", (await flat(m.step("settings"))).includes("Done.")
         && (await m.id("joiner-change").count()) === 1 && (await m.id("joiner-use-team").count()) === 0, await flat(m.step("settings")));
      await checkWords(m.page, ck, "step 1 done");

      await m.id("joiner-change").click();
      ck("step 1 done: Change opens the choices", await appears(m.id("choices-form")));
      await m.id("builders-less").click();
      await m.id("choices-save").click();
      ck("a stricter choice is saved and the form closes", await goes(m.id("choices-form")));
      same("a stricter choice: what was sent", put().at(-1), { method: "PUT", body: { autonomy: "L3-review", builders_max: 2, releaser: false } });
      h = await answer("sam");
      const owners = await answer("robin");
      same("a stricter choice: the person's own record, the team's as before",
           [h.onboarding.choices.builders_max, owners.onboarding.choices.builders_max], [2, 3]);

      await m.go();
      await m.id("joiner-change").click();
      same("the form then starts from their own value", await m.id("builders-count").innerText(), "2");
      const sofar = put().length;
      await m.id("choice-L4-ship").check();
      ck("a looser merge rule is asked about first", await appears(m.id("merge-confirm-box")) && put().length === sofar);
      await m.id("merge-confirm").click();
      ck("then refused", await appears(m.id("step-error")));
      same("in the daemon's own words", await flat(m.id("step-error")), TOO_LOOSE);
      ck("the form stays open", (await m.id("choices-form").count()) === 1);
      same("and nothing changed", (await answer("sam")).onboarding.choices.autonomy, "L3-review");
      await shot(m.page, "refused", 1440);
      await checkWords(m.page, ck, "refused choice");
      await m.id("merge-cancel").click();
      ck("keeping the approval clears the refusal", await goes(m.id("step-error")));

      await m.id("joiner-use-team").click();
      ck("the team's choices are one click from an open form", await goes(m.id("choices-form")));
      same("sent as a clear", put().at(-1), { method: "PUT", body: CLEAR });
      same("and the person follows the team again", (await answer("sam")).onboarding.choices.builders_max, 3);

      let status = 403, said = "This key cannot change the crew's choices.";
      await m.page.route("**/api/crew/settings", (route) => route.request().method() !== "PUT" ? route.continue()
        : route.fulfill({ status, contentType: "application/json", body: JSON.stringify({ detail: said }) }));
      await m.id("joiner-change").click();
      await m.id("choices-save").click();
      ck("a save the daemon forbids shows an error", await appears(m.id("step-error")));
      same("in its words", await flat(m.id("step-error")), said);
      await m.id("joiner-use-team").click();
      ck("taking the team's, forbidden: an error under the buttons", await appears(m.id("joiner-settings-error")));
      same("in its words too", await flat(m.id("joiner-settings-error")), said);
      await m.id("joiner-close").click();
      await m.id("joiner-change").click();
      ck("opening again starts with no old error", (await m.id("joiner-settings-error").count()) === 0 && (await m.id("step-error").count()) === 0);
      status = 500; said = "boom";
      await m.id("choices-save").click();
      await m.id("step-error").waitFor();
      same("a save that fails otherwise reads as not saved", await flat(m.id("step-error")), "That was not saved. Try again in a moment.");
      await m.ctx.close();
    }

    // ── what waits for the person stays, above the steps ──
    {
      const desk = await api("POST", "/api/desk", { question: "Which colour for the links?", options: ["blue", "green"], recommendation: "blue",
                                                    why: "it reads best", project: ids.projects.checkout, to: ids.people.sam });
      ck("a question is put to the member", desk.status < 300, JSON.stringify(desk));
      await pastCache();
      const m = await open("sam");
      await m.go();
      const row = m.id("waiting-row").filter({ hasText: "Which colour for the links" });
      ck("waiting: the row shows on the joiner view", await appears(row));
      const [rowBox, stepBox, welcomeBox] = [await row.boundingBox(), await m.step("settings").boundingBox(), await m.id("joiner-welcome").boundingBox()];
      ck("waiting: under the welcome and above the steps", !!rowBox && !!stepBox && !!welcomeBox && welcomeBox.y < rowBox.y && rowBox.y < stepBox.y,
         JSON.stringify([welcomeBox, rowBox, stepBox]));
      await shot(m.page, "waiting", 1440);
      await checkWords(m.page, ck, "waiting");
      await checkPaper(m.page, ck, "waiting");
      await row.getByRole("button", { name: "Answer" }).click();
      await row.getByTestId("answer-panel").getByRole("button", { name: "green", exact: true }).click();
      ck("waiting: it is answered from here and leaves", await goes(row, 20000));
      ck("waiting: the answer was sent", m.ctl.sent.some((x) => x.method === "POST" && /^\/api\/desk\/[^/]+\/reply$/.test(x.path) && /"green"/.test(x.body || "")));
      ck("waiting: the steps are still there", (await m.id("joiner-start").count()) === 1);
      await m.ctx.close();
    }

    // ── step 2 ticks as the daemon hears the machine, one home read a turn ──
    {
      const m = await open("sam");
      await m.go();
      const before = m.reads().length;
      const said = await api("POST", "/api/crew/machine", { machine: "sam-laptop", version: "0.9.0" }, ids.keys.sam);
      ck("the machine reports", said.status === 200, JSON.stringify(said));
      ck("step 2: install ticks on the next read, with no reload",
         await appears(m.page.locator("[data-testid=joiner-command][data-key=install][data-done=true]"), 40000));
      same("a person joining reads Home once a turn", m.reads().slice(before), ["?projects=team"]);
      same("step 2: install and sign-in done, the crew not yet",
           await m.id("joiner-command").evaluateAll((els) => els.map((e) => [e.dataset.key, e.dataset.done])),
           [["install", "true"], ["login", "true"], ["crew", "false"]]);
      same("step 2: so the card is not done", await m.step("machine").getAttribute("data-done"), "false");
      ck("step 2: each says what the daemon saw", (await flat(m.command("install"))).includes("Installed: tares 0.9.0.")
         && (await flat(m.command("login"))).includes("Signed in from sam-laptop."), await flat(m.step("machine")));
      same("step 2: a done step keeps its command", await m.id("joiner-command").getByTestId("start-copy").count(), 3);
      same("step 2: ticked rows read Done", await m.command("login").getByTestId("start-tick").getAttribute("aria-label"), "Done");
      await shot(m.page, "machine", 1440);
      await checkWords(m.page, ck, "machine steps");

      // ── step 3: joining ──
      let release;
      m.ctl.hold = new Promise((r) => { release = r; });   // the page keeps the list it has
      const closed = await api("PATCH", `/api/projects/${ids.projects.search}`, { scope: "private" }, ids.keys.robin);
      ck("a project turns private while the page is open", closed.status === 200, JSON.stringify(closed));
      await m.project("search").getByTestId("joiner-join").click();
      const refused = m.project("search").getByTestId("joiner-join-error");
      ck("step 3: joining it is refused under its row", await appears(refused));
      same("step 3: in the daemon's sentence", await flat(refused), NOW_PRIVATE);
      same("step 3: the click called the join route", m.writes(`/api/projects/${ids.projects.search}/join`).map((x) => x.method), ["POST"]);
      await checkWords(m.page, ck, "join refused");
      const reopened = await api("PATCH", `/api/projects/${ids.projects.search}`, { scope: "team" }, ids.keys.robin);
      ck("and the project is the team's again", reopened.status === 200, JSON.stringify(reopened));
      m.ctl.hold = null; release();

      let go;
      const held = new Promise((r) => { go = r; });
      await m.page.route(`**/api/projects/${ids.projects.checkout}/join`, async (route) => { await held; await route.continue(); });
      await m.project("checkout").getByTestId("joiner-join").click();
      ck("step 3: one join at a time", await m.project("search").getByTestId("joiner-join").isDisabled()
         && (await m.project("checkout").getByTestId("joiner-join").isDisabled()));
      const reading = m.reads().length;
      go();
      ck("step 3: joined, the row says so", await appears(m.project("checkout").getByTestId("joiner-member")));
      same("step 3: in these words", await flat(m.project("checkout").getByTestId("joiner-member")), "You are on it");
      ck("step 3: with no button left on it", (await m.project("checkout").getByTestId("joiner-join").count()) === 0);
      same("step 3: the join route was called once", m.writes(`/api/projects/${ids.projects.checkout}/join`).map((x) => x.method), ["POST"]);
      ck("step 3: the step ticks", await appears(m.page.locator("[data-testid=joiner-step][data-key=project][data-done=true]")));
      ck("step 3: the page read again", m.reads().length > reading);
      same("step 3: the other project still offers to join", await m.id("joiner-join").count(), 1);
      await m.ctx.close();
    }
    {
      const m = await open("sam");
      await m.go();
      ck("a project the person is already on says so and offers no join",
         (await m.project("checkout").getByTestId("joiner-member").count()) === 1 && (await m.project("checkout").getByTestId("joiner-join").count()) === 0);
      await shot(m.page, "joined", 1440);
      await checkWords(m.page, ck, "joined");
      await checkPaper(m.page, ck, "joined");

      // ── hiding the steps, and getting them back ──
      const broken = (route) => route.fulfill({ status: 500, contentType: "application/json", body: '{"detail":"down"}' });
      await m.page.route("**/api/home/start/hide", broken);
      await m.id("joiner-hide").click();
      ck("a hide that fails says so", await appears(m.id("joiner-hide-error")));
      same("in plain words", await flat(m.id("joiner-hide-error")), "Could not hide the steps. Try again in a moment.");
      ck("and the steps stay", (await m.id("joiner-start").count()) === 1);
      await m.page.unroute("**/api/home/start/hide", broken);
      same("the button's words", await flat(m.id("joiner-hide")), "Hide these steps");
      await m.id("joiner-hide").click();
      ck("hidden: the usual Home", await appears(m.id("factory-home")) && (await m.id("joiner-start").count()) === 0);
      ck("hidden: it offers the steps back", await appears(m.id("start-hidden")));
      same("hidden: in these words", await flat(m.id("start-hidden")), "The list “Your first steps” is hidden. Show it again");
      await m.go("/", "factory-home");
      same("hidden: Home is read plainly again", m.reads().at(-1), "");
      ck("hidden: still offered after a reload", (await m.id("start-hidden").count()) === 1);
      await shot(m.page, "hidden", 1440);
      await checkWords(m.page, ck, "hidden");
      await m.id("start-show").click();
      ck("shown again: the joiner view is back", await appears(m.id("joiner-start")) && (await m.id("start-hidden").count()) === 0);
      await m.ctx.close();
    }

    // ── every step done: the usual Home ──
    {
      const stamp = new Date().toISOString();
      const lines = ["orchestrator", "reviewer"].flatMap((role) => {
        const sid = `s-sam-${role}`, name = `sam-${role}`;
        return [{ type: "session_station", sessionId: sid, cwd: "/w/crew", timestamp: stamp, station: name, role, parent: "",
                  factory_station: name, factory_crew: "sam", crew: "sam", factory_machine: "sam-laptop", machine: "sam-laptop" },
                { type: "session_state", sessionId: sid, cwd: "/w/crew", timestamp: stamp, state: "idle", reason: "" }];
      });
      const pushed = await api("POST", "/ingest/claude_code", lines.map((x) => JSON.stringify(x)).join("\n") + "\n", ids.keys.sam, "application/x-ndjson");
      ck("the member's crew reports", pushed.status < 300, JSON.stringify(pushed));
      const h = await answer("sam");
      same("the daemon then has every step done and still calls them a joiner",
           [h.joiner.joining, h.start.show, h.start.steps.every((s) => s.done)], [true, false, true]);
      const m = await open("sam");
      await m.go("/", "factory-home");
      ck("every step done: the usual Home, with nothing offered back",
         (await m.id("joiner-start").count()) === 0 && (await m.id("start-hidden").count()) === 0);
      same("every step done: read plainly", m.reads(), [""]);
      await shot(m.page, "done", 1440);
      await checkWords(m.page, ck, "every step done");
      await m.ctx.close();
    }
    same("no page error", crashes, []);
  } finally {
    if (browser) await browser.close();
    if (daemon) daemon.stop();
    fs.rmSync(home, { recursive: true, force: true });
  }
  console.log(`\n${pass} passed, ${fail} failed`);
  process.exit(fail ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(1); });
