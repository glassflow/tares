/*
 * Browser e2e for TR-737: the factory Docs and memory page (/docs).
 *
 * The page lists the crew's shared rules and memory and each project's spec, plan, rules and memory,
 * and shows one as a document. Project memory lines say who wrote them and when; every other
 * document says who changed it and when as a whole. A line is added from the page; a line that is
 * already there is refused politely. /docs?doc=<id> opens that document on a full page load. Each
 * load is one read of the cell. Raw HTML in a line is shown as text. At 390 px the list is a select.
 * Every state shows only words from words.json (document text is exempt) and none scrolls sideways.
 *
 * Seeds a scratch factory cell (tests/seed-factory-docs.py, invented names), then runs the real
 * daemon on it, serving the built console. The sample states (?sample=<name>, with the sample flag
 * in localStorage) need no data.
 * Run:  npm run build && node tests/factory-docs.e2e.cjs     (from ui/, tares installed in ../.venv)
 * Set SHOTS_DIR to keep a screenshot of every state at 1440 and 390 px.
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

const PORT = Number(process.env.TARES_E2E_PORT || 8853);
const BASE = `http://127.0.0.1:${PORT}`;
const SHOTS = process.env.SHOTS_DIR;
const SAMPLES = ["busy", "plan", "pmem", "long", "big", "dup", "empty", "load", "err"];

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

(async () => {
  const bin = path.join(REPO, ".venv/bin");
  const tares = fs.existsSync(path.join(bin, "tares")) ? path.join(bin, "tares") : "tares";
  const python = fs.existsSync(path.join(bin, "python")) ? path.join(bin, "python") : "python3";
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "tares-factory-docs-e2e-"));
  const dist = process.env.TARES_UI_DIST || path.join(UI_DIR, "dist");
  const ids = JSON.parse(execFileSync(python, [path.join(__dirname, "seed-factory-docs.py"), home, String(PORT),
    String(PORT + 1), dist, tares], { cwd: REPO, encoding: "utf8" }).trim().split("\n").pop());
  ck("seeded the projects and their documents", !!ids["project-x-memory"] && !!ids["project-y-memory"] && !!ids["shared-rules"]);

  const env = { ...process.env, TARES_EDITION: "factory", TARES_HOME: home, TARES_PORT: String(PORT), TARES_MCP_PORT: String(PORT + 1),
                TARES_UI_DIST: dist, TARES_OTLP_GRPC_PORT: "off", PYTHONPATH: REPO };   // the venv installs the main checkout; this runs the code under test
  for (const k of ["TARES_AUTH_TOKEN", "TARES_DB", "TARES_CATALOG", "TARESD_URL", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"]) delete env[k];
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
    const shot = async (page, n) => { if (SHOTS) await page.screenshot({ path: path.join(SHOTS, n), fullPage: true }); };
    const overflow = (page) => page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    const settle = async (page) => { await page.waitForLoadState("networkidle"); await page.waitForTimeout(150); };

    const { ctx, page } = await newPage(1440);
    const pane = page.getByTestId("doc-pane");
    const title = () => page.getByTestId("doc-title").innerText();
    const rows = page.getByTestId("doc-row");
    const rowText = async () => rows.evaluateAll((r) => r.map((x) => x.firstElementChild.innerText.trim()));
    const rowBy = async () => page.getByTestId("doc-row-by").allInnerTexts().then((a) => a.map((t) => t.trim()));
    const link = (name, group = 0) => page.getByTestId("doc-group").nth(group).getByTestId("doc-link").filter({ hasText: new RegExp(`^${name}$`) });
    const pick = async (name, group, expectTitle) => {
      await link(name, group).click();
      await pane.getByTestId("doc-title").filter({ hasText: expectTitle }).waitFor();
      await settle(page);
    };
    const addBox = page.getByTestId("doc-add");
    // Adding a line is a POST, then (when the line is new) a re-read of the document. Wait for both: a fixed pause
    // is shorter than they take on a loaded machine, and "networkidle" is already reached on this page, so
    // it returns at once. A late answer would also clear the box after the next line was typed into it.
    const addLine = async (text) => {
      await addBox.locator("input").fill(text);
      let posted = false;
      let reread;
      const rereadDone = new Promise((resolve) => { reread = resolve; });
      const onResponse = (r) => {
        if (r.request().method() === "POST" && /\/lines$/.test(r.url())) posted = true;
        else if (posted && r.request().method() === "GET" && /\/api\/factory\/docs/.test(r.url())) reread();
      };
      page.on("response", onResponse);
      try {
        const answer = page.waitForResponse((r) => r.request().method() === "POST" && /\/lines$/.test(r.url()));
        await addBox.getByRole("button", { name: "Add line" }).click();
        const res = await answer;
        const body = res.ok() ? await res.json().catch(() => ({})) : {};
        if (res.ok() && body.added !== false) {
          // The page polls every 60 s, so without a limit a missing re-read would pass after the poll. It must come at once.
          let timer;
          const late = new Promise((_, no) => { timer = setTimeout(() => no(new Error("the page did not re-read the document after the line was added")), 10000); });
          try { await Promise.race([rereadDone, late]); } finally { clearTimeout(timer); }
        }
        else await addBox.getByTestId("doc-add-error").waitFor();
      } finally {
        page.off("response", onResponse);
      }
      await settle(page);
    };

    // 1. the list loads in one call and opens the first shared document
    const one = expectOneCall(page, "/api/factory/docs");
    await page.goto(`${BASE}/docs`, { waitUntil: "load" });
    await pane.waitFor();
    await one(ck, "/docs");
    ck("the page has its heading", await page.getByRole("heading", { name: "Docs and memory", exact: true }).isVisible());
    const groups = await page.getByTestId("doc-group").evaluateAll((g) => g.map((x) => [...x.querySelectorAll("[data-testid=doc-link]")].map((a) => a.textContent.trim())));
    ck("the list has two groups: every project, and the open project", groups.length === 2, JSON.stringify(groups));
    ck("every project: Rules and Memory", JSON.stringify(groups[0]) === '["Rules","Memory"]', JSON.stringify(groups[0]));
    ck("a project: Spec, Plan, Rules, Memory", JSON.stringify(groups[1]) === '["Spec","Plan","Rules","Memory"]', JSON.stringify(groups[1]));
    const picker = page.getByTestId("doc-list").getByTestId("doc-picker-button");
    ck("the picker names the project shown", (await picker.innerText()).trim() === "project-x", await picker.innerText());
    ck("only one project's documents are listed", !(await page.getByTestId("doc-list").innerText()).includes("project-y")
       && (await page.getByTestId("doc-finished").count()) === 0);
    await picker.click();
    const options = page.getByTestId("doc-list").getByTestId("doc-picker-option");
    ck("the picker lists the open project, then the finished one under Finished",
       (await options.allInnerTexts()).map((t) => t.trim()).join("|") === "project-x|project-y"
       && /Finished/.test(await page.getByTestId("doc-picker-panel").innerText()), (await options.allInnerTexts()).join("|"));
    await shot(page, "live-picker-open-1440.png");
    await page.getByTestId("doc-list").getByTestId("doc-picker-search").fill("proj-y");
    ck("a search with no match says so", (await options.count()) === 0 && await page.getByTestId("doc-picker-none").isVisible());
    await page.getByTestId("doc-list").getByTestId("doc-picker-search").fill("ct-y");
    ck("the search narrows the list", (await options.allInnerTexts()).map((t) => t.trim()).join("|") === "project-y");
    await page.keyboard.press("Escape");
    ck("Escape closes the picker and leaves the list as it was", (await page.getByTestId("doc-picker-panel").count()) === 0
       && (await picker.innerText()).trim() === "project-x");
    await shot(page, "live-picker-1440.png");
    ck("a project that is not a factory project is not listed", !(await page.getByTestId("doc-list").innerText()).includes("project-w"));
    ck("the first shared document is open", (await title()) === "Rules for every project" && (await link("Rules", 0).getAttribute("aria-current")) === "page", await title());
    ck("a shared document says who changed it, and when, as a whole", /^Changed by .+, .+/.test(await page.getByTestId("doc-changed").innerText()),
       await page.getByTestId("doc-changed").innerText());
    ck("its lines have no author of their own", (await rowBy()).every((t) => t === ""), (await rowBy()).join("|"));
    ck("the lines the crew added are rows", (await rowText()).includes("Stop only the processes you started yourself."));
    ck("the menu has Docs and memory, and it is the current one",
       (await page.locator('nav[aria-label="Main"] a[aria-current="page"]').allInnerTexts()).map((t) => t.trim()).join() === "Docs and memory");
    await checkWords(page, ck, "list");
    await shot(page, "live-list-1440.png");

    // 2. project memory: who and when per line
    await pick("Memory", 1, "Memory: project-x");
    ck("a project memory is open", (await title()) === "Memory: project-x");
    ck("its lines are in order, oldest first", JSON.stringify(await rowText()) === JSON.stringify([
      "Articles over 600 words fail the length check; split them.", "Expand the questions before the checks, not after.",
      "The first wave needs a yes on each pull request the day it opens.", "Answer articles keep one fact per sentence so an assistant can quote them.",
      "Tell the reviewer which ticket a pull request closes."]), (await rowText()).join("|"));
    ck("each line shows by whom and how many days ago", JSON.stringify(await rowBy()) === JSON.stringify([
      "the Builder, 12 days ago", "the Reviewer, 9 days ago", "you, 6 days ago", "the Builder, 3 days ago", "the Orchestrator, 1 day ago"]), (await rowBy()).join("|"));
    ck("the stamp is not left in the line", !(await rowText()).some((t) => /\(\d{4}-\d{2}-\d{2}/.test(t)));
    ck("the header says who changed it and when", (await page.getByTestId("doc-changed").innerText()) === "Changed by the Builder, 1 day ago",
       await page.getByTestId("doc-changed").innerText());
    ck("the address names the document", new URL(page.url()).searchParams.get("doc") === ids["project-x-memory"], page.url());
    ck("a memory has the add box, with its hint", (await addBox.count()) === 1 && /Add a line to this memory/.test(await addBox.innerText()));
    await checkWords(page, ck, "project memory");
    await shot(page, "live-memory-1440.png");

    // 3. adding a line: it is the last one, by you, just now; the box is empty again
    await addLine("Keep the checks under two minutes.");
    ck("the added line is the last row", (await rowText()).slice(-1)[0] === "Keep the checks under two minutes." && (await rows.count()) === 6, (await rowText()).slice(-1)[0]);
    ck("it says you, just now", (await rowBy()).slice(-1)[0] === "you, just now", (await rowBy()).slice(-1)[0]);
    ck("the box is empty again", (await addBox.locator("input").inputValue()) === "");
    ck("and the header says you", (await page.getByTestId("doc-changed").innerText()) === "Changed by you, just now", await page.getByTestId("doc-changed").innerText());

    // 4. the same line again is refused politely; nothing is added
    await addLine("Keep the checks under two minutes.");
    ck("a duplicate says it is already there", (await page.getByTestId("doc-add-error").innerText()).trim() === "That line is already there.",
       await page.getByTestId("doc-add-error").count() ? await page.getByTestId("doc-add-error").innerText() : "no message");
    ck("and adds nothing", (await rows.count()) === 6);
    ck("the line stays in the box to edit", (await addBox.locator("input").inputValue()) === "Keep the checks under two minutes.");
    await checkWords(page, ck, "duplicate refused");
    await shot(page, "live-dup-1440.png");
    await addBox.locator("input").fill("Keep the checks under two minutes. Really.");
    ck("typing again clears the message", (await page.getByTestId("doc-add-error").count()) === 0);
    await addBox.locator("input").fill("");
    await addBox.getByRole("button", { name: "Add line" }).click();
    ck("an empty line asks for the line", (await page.getByTestId("doc-add-error").innerText()).trim() === "Write the line first.");
    ck("and adds nothing either", (await rows.count()) === 6);

    // 5. a plan and a project's rules are read only
    await pick("Plan", 1, "Plan: project-x");
    ck("a plan has no add box", (await addBox.count()) === 0);
    ck("a plan has its milestones as headings", (await page.locator('[data-testid=doc-row][data-kind=h2]').allInnerTexts()).map((t) => t.trim()).join("|")
       === "M1: Sitemap and index live|M2: Checks gate CI", (await page.locator('[data-testid=doc-row][data-kind=h2]').allInnerTexts()).join("|"));
    ck("a plan's rows have no author", (await rowBy()).every((t) => t === ""));
    ck("a plan says who changed it", (await page.getByTestId("doc-changed").innerText()) === "Changed by you, 14 days ago", await page.getByTestId("doc-changed").innerText());
    await checkWords(page, ck, "plan");
    await shot(page, "live-plan-1440.png");
    await pick("Rules", 1, "Rules: project-x");
    ck("a project's rules have no add box", (await addBox.count()) === 0);
    await pick("Spec", 1, "Spec: project-x");
    ck("a spec is read as a document", (await rowText()).includes("Every article answers one question") && (await addBox.count()) === 0);

    // 6. the shared rules take a line; the header then names you
    await pick("Rules", 0, "Rules for every project");
    ck("the shared rules have the add box", /Add a line to the shared rules/.test(await addBox.innerText()));
    await addLine("Check the diff before you commit.");
    ck("a line added to the shared rules is the last one", (await rowText()).slice(-1)[0] === "Check the diff before you commit.", (await rowText()).slice(-1)[0]);
    ck("and the header says you changed it, just now", (await page.getByTestId("doc-changed").innerText()) === "Changed by you, just now", await page.getByTestId("doc-changed").innerText());
    ck("a shared line has no author of its own", (await rowBy()).every((t) => t === ""));
    await addLine("Check the diff before you commit.");
    ck("a duplicate shared line is refused too", (await page.getByTestId("doc-add-error").innerText()).trim() === "That line is already there.");

    // 7. markdown shows as markdown and never as HTML
    await pick("Memory", 0, "Memory for every project");
    await addLine("**Bold** advice, see [the guide](https://example.com/guide) <img src=x alt=pic> <b>raw</b>");
    const body = page.getByTestId("doc-body");
    ck("bold and a link are rendered", (await body.locator("strong").count()) === 1 && (await body.locator('a[href="https://example.com/guide"]').count()) === 1);
    ck("a link opens in a new tab and keeps the page private", (await body.locator("a").first().getAttribute("target")) === "_blank"
       && /noopener/.test((await body.locator("a").first().getAttribute("rel")) || ""));
    ck("raw HTML is not rendered: no image, no bold tag", (await body.locator("img").count()) === 0 && (await body.locator("b").count()) === 0);
    ck("and it is shown as its own text", (await body.innerText()).includes("<b>raw</b>"));
    await checkWords(page, ck, "markdown line");

    // 8. a document opened by its address on a full page load: one call
    const one2 = expectOneCall(page, `/api/factory/docs/${ids["project-x-memory"]}`);
    await page.goto(`${BASE}/docs?doc=${ids["project-x-memory"]}`, { waitUntil: "load" });
    await pane.waitFor();
    await one2(ck, "/docs?doc=<id>");
    ck("that document is the one open", (await title()) === "Memory: project-x" && (await link("Memory", 1).getAttribute("aria-current")) === "page", await title());
    ck("with the line added before", (await rowText()).includes("Keep the checks under two minutes."));
    await checkWords(page, ck, "document by address");
    await page.goto(`${BASE}/docs?doc=doc_nothing`, { waitUntil: "load" });
    await pane.waitFor();
    await settle(page);
    ck("an address that names no document opens the first shared one", (await title()) === "Rules for every project"
       && !new URL(page.url()).searchParams.has("doc"), `${await title()} ${page.url()}`);

    // 9. finished projects, and a memory longer than the page shows at once
    await page.goto(`${BASE}/docs`, { waitUntil: "load" });
    await pane.waitFor();
    await page.getByTestId("doc-list").getByTestId("doc-picker-button").click();
    await page.getByTestId("doc-list").getByTestId("doc-picker-search").fill("project-y");
    await page.keyboard.press("Enter");
    await pane.getByTestId("doc-title").filter({ hasText: "project-y" }).waitFor();
    await settle(page);
    ck("a finished project is picked with the keyboard, its first document opens, and the list shows only its documents",
       (await page.getByTestId("doc-group").count()) === 2 && /project-y/.test(await title())
       && (await page.getByTestId("doc-list").getByTestId("doc-picker-button").innerText()).trim() === "project-y (finished)", await title());
    await pick("Memory", 1, "Memory: project-y");
    ck("a long memory shows its newest 100 lines", (await rows.count()) === 100 && (await rowText())[99] === "Memory line 130: a fact kept for the crew.", `${await rows.count()}`);
    ck("and offers the 30 before", (await page.getByTestId("doc-earlier").innerText()).trim() === "Show 30 earlier lines");
    await checkWords(page, ck, "long memory");
    await page.getByTestId("doc-earlier").click();
    ck("the earlier lines come on request, in order", (await rows.count()) === 130 && (await rowText())[0] === "Memory line 1: a fact kept for the crew."
       && (await page.getByTestId("doc-earlier").count()) === 0);
    await page.reload({ waitUntil: "load" });
    await pane.waitFor();
    ck("a finished project's document opened by address shows its project in the picker", (await page.getByTestId("doc-group").count()) === 2
       && (await link("Memory", 1).getAttribute("aria-current")) === "page"
       && (await page.getByTestId("doc-list").getByTestId("doc-picker-button").innerText()).trim() === "project-y (finished)");
    ck("the page does not scroll sideways at 1440", (await overflow(page)) <= 0);

    // 10. without the sample flag the sample address changes nothing
    await page.goto(`${BASE}/docs?sample=big`, { waitUntil: "load" });
    await pane.waitFor();
    await settle(page);
    ck("?sample=big without the flag shows the cell's own documents", (await title()) === "Rules for every project" && (await page.getByTestId("doc-group").count()) === 2);
    await ctx.close();

    // 11. narrow: the list is a select, the document fills the width, nothing scrolls sideways
    {
      const n = await newPage(390);
      const p = n.page;
      await p.goto(`${BASE}/docs?doc=${ids["project-x-memory"]}`, { waitUntil: "load" });
      await p.getByTestId("doc-pane").waitFor();
      await settle(p);
      const sel = p.getByTestId("doc-select");
      ck("390: the list is a select", await sel.isVisible() && !(await p.getByTestId("doc-list").isVisible()));
      ck("390: the select has the open document chosen", (await sel.inputValue()) === ids["project-x-memory"], await sel.inputValue());
      ck("390: it lists the shared documents and the chosen project's", (await sel.locator("option").count()) === 6, `${await sel.locator("option").count()}`);
      const np = p.getByTestId("doc-narrow").getByTestId("doc-picker-button");
      ck("390: the picker names the project", (await np.innerText()).trim() === "project-x");
      ck("390: the page does not scroll sideways", (await overflow(p)) <= 0, `${await overflow(p)}px`);
      ck("390: a line's author sits under it", await p.getByTestId("doc-row").first().evaluate((r) => {
        const [text, by] = r.children; return by.getBoundingClientRect().top >= text.getBoundingClientRect().bottom - 1;
      }));
      await checkWords(p, ck, "390 memory");
      await shot(p, "live-memory-390.png");
      await sel.selectOption(ids["project-x-plan"]);
      await p.getByTestId("doc-title").filter({ hasText: "Plan: project-x" }).waitFor();
      ck("390: choosing a document opens it", new URL(p.url()).searchParams.get("doc") === ids["project-x-plan"]);
      ck("390: and the select shows it", (await sel.inputValue()) === ids["project-x-plan"]);
      await checkWords(p, ck, "390 plan");
      await shot(p, "live-plan-390.png");
      await np.click();
      await p.getByTestId("doc-narrow").getByTestId("doc-picker-option").filter({ hasText: "project-y" }).click();
      await p.getByTestId("doc-title").filter({ hasText: "project-y" }).waitFor();
      ck("390: a finished project is pickable, and the select follows it", (await np.innerText()).trim() === "project-y (finished)"
         && (await sel.locator("option").allInnerTexts()).some((t) => t.includes("project-y")) && (await overflow(p)) <= 0);
      await shot(p, "live-finished-390.png");
      await n.ctx.close();
    }

    // 12. every sample state at 1440 and 390: its words, no sideways scroll, a screenshot
    for (const width of [1440, 390]) {
      for (const name of SAMPLES) {
        const { ctx: c, page: p } = await newPage(width, true);
        const tag = `sample ${name} at ${width}`;
        await p.goto(`${BASE}/docs?sample=${name}`, { waitUntil: "load" });
        if (name === "load") await p.getByTestId("docs-loading").waitFor();
        else if (name === "err") await p.getByText("Documents did not load.").waitFor();
        else if (name === "empty") await p.getByTestId("docs-empty").waitFor();
        else await p.getByTestId("doc-pane").waitFor();
        await settle(p);
        const text = await p.getByTestId("factory-docs").innerText();
        if (name === "load") ck(`${tag}: it is the loading state, no document`, (await p.getByTestId("doc-pane").count()) === 0 && (await p.getByTestId("docs-loading").isVisible()));
        if (name === "err") ck(`${tag}: it says the documents did not load and offers Retry`, /Documents did not load\./.test(text)
           && (await p.getByRole("button", { name: "Retry" }).count()) === 1 && (await p.getByTestId("doc-pane").count()) === 0, text);
        if (name === "empty") ck(`${tag}: it says nothing is written yet`, /Nothing written yet\./.test(text) && (await p.getByTestId("doc-pane").count()) === 0, text);
        if (name === "busy") ck(`${tag}: the shared rules, with their lines`, (await p.getByTestId("doc-title").innerText()) === "Rules for every project" && (await p.getByTestId("doc-row").count()) >= 5);
        if (name === "plan") ck(`${tag}: a plan, read only`, (await p.getByTestId("doc-title").innerText()) === "Plan: project-x" && (await p.getByTestId("doc-add").count()) === 0);
        if (name === "pmem" || name === "long") ck(`${tag}: a project memory with who and when`, /^the Builder, 3 days ago$/m.test((await p.getByTestId("doc-row-by").allInnerTexts()).join("\n")), "");
        if (name === "long") ck(`${tag}: a very long line wraps inside the page`, (await overflow(p)) <= 0);
        if (name === "big") ck(`${tag}: 100 of 2,000 lines, the rest on request`, (await p.getByTestId("doc-row").count()) === 100
           && (await p.getByTestId("doc-earlier").innerText()).trim() === "Show 1,900 earlier lines", `${await p.getByTestId("doc-row").count()}`);
        if (name === "dup") ck(`${tag}: the line is typed and the refusal is shown`, (await p.getByTestId("doc-add").locator("input").inputValue()) === "Expand the questions before the checks, not after."
           && (await p.getByTestId("doc-add-error").innerText()).trim() === "That line is already there.");
        ck(`${tag}: no sideways scroll`, (await overflow(p)) <= 0, `${await overflow(p)}px`);
        await checkWords(p, ck, tag);
        await shot(p, `docs-${name}-${width}.png`);
        await c.close();
      }
    }
    // a sample line already in the document is refused, a new one is taken (the sample has no cell to write to)
    {
      const { ctx: c, page: p } = await newPage(1440, true);
      await p.goto(`${BASE}/docs?sample=pmem`, { waitUntil: "load" });
      await p.getByTestId("doc-pane").waitFor();
      const box = p.getByTestId("doc-add");
      await box.locator("input").fill("Expand the questions before the checks, not after.");
      await box.getByRole("button", { name: "Add line" }).click();
      ck("sample: a line already there is refused", (await p.getByTestId("doc-add-error").innerText()).trim() === "That line is already there.");
      await c.close();
    }
  } finally {
    if (browser) await browser.close();
    daemon.kill();
    fs.rmSync(home, { recursive: true, force: true });
  }
  console.log(`\n${pass} passed, ${fail} failed`);
  process.exit(fail ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(1); });
