/*
 * Browser e2e for the "Summarize a session now" dialog on a Challenger workflow project: the
 * session picker must show each session's id and its repo, branch and state in full, inside the
 * viewport. It used to run past the dialog's edge (the dialog clips what overflows it), so a
 * branch label read "factory..." and the list was cut off under the dialog.
 *
 * Runs the real daemon on a scratch data directory and port, serving the built console.
 * Run:  npm run build && node tests/session-picker.e2e.cjs
 * Needs: tares installed in the repo .venv (or on PATH). TARES_E2E_CHROME points Playwright at
 * another Chrome binary when its own browser download is missing.
 */
const http = require("http");
const { spawn } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");

const UI_DIR = path.resolve(__dirname, "..");
const REPO = path.resolve(UI_DIR, "..");
const { chromium } = require(path.join(UI_DIR, "node_modules", "playwright"));

const PORT = 8811;
const BASE = `http://127.0.0.1:${PORT}`;
const LONG_BRANCH = "ashish/long-feature-branch-name-for-pricing";
const SESSIONS = [
  ["2f9c1a7e-0b11-4c1d-9a30-aaaa00000001", "/work/worktrees/RIUS-1425", "factory/RIUS-1425"],
  ["7be04d52-6c8e-4b9f-8d11-aaaa00000002", "/work/worktrees/RIUS-1431", "factory/RIUS-1431"],
  ["c10e83aa-90f4-47a2-b6c7-aaaa00000003", "/work/argus-core", LONG_BRANCH],
];

let pass = 0, fail = 0;
const ck = (label, cond, detail = "") => {
  cond ? pass++ : fail++;
  console.log((cond ? "  ok   " : "  FAIL ") + label + (cond ? "" : `   ${detail}`));
};

function call(method, url, body, type = "application/json") {
  return new Promise((resolve, reject) => {
    const r = http.request(BASE + url, { method, headers: { "content-type": type, "content-length": Buffer.byteLength(body || "") } },
      (res) => { let d = ""; res.on("data", (c) => (d += c)); res.on("end", () => resolve({ status: res.statusCode, body: d })); });
    r.on("error", reject); r.end(body);
  });
}

async function waitFor(url, tries = 100) {
  for (; tries > 0; tries--) {
    const ok = await new Promise((res) => http.get(url, (r) => { r.resume(); res(true); }).on("error", () => res(false)));
    if (ok) return true;
    await new Promise((r) => setTimeout(r, 200));
  }
  return false;
}

(async () => {
  const tares = fs.existsSync(path.join(REPO, ".venv/bin/tares")) ? path.join(REPO, ".venv/bin/tares") : "tares";
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "tares-picker-e2e-"));
  // a scratch cell: its own data under `home`, and none of the caller's auth, database or catalog settings
  const env = { ...process.env, TARES_EDITION: "platform", TARES_HOME: home, TARES_DB: path.join(home, "tares.duckdb"),
                TARES_CATALOG: path.join(home, "catalog.yaml"),
                TARES_PORT: String(PORT), TARES_MCP_PORT: String(PORT + 1),
                TARES_UI_DIST: path.join(UI_DIR, "dist") };
  delete env.TARES_AUTH_TOKEN;
  const daemon = spawn(tares, ["up"], { cwd: REPO, stdio: "ignore", env });
  let browser;
  try {
    ck("daemon up", await waitFor(`${BASE}/health`));
    await call("POST", "/api/sources", JSON.stringify({ name: "claude_code", connector: "claude_code", poll: "10s", config: { push: true } }));
    const stamp = new Date().toISOString().replace(/\.\d+Z$/, "Z");
    const lines = SESSIONS.map(([sessionId, cwd, gitBranch]) =>
      JSON.stringify({ sessionId, type: "session_flow", cwd, gitBranch, timestamp: stamp, flow: "challenger" }));
    await call("POST", "/ingest/claude_code", lines.join("\n") + "\n", "application/x-ndjson");
    const projects = JSON.parse((await call("GET", "/api/projects")).body).projects;
    const project = projects.find((p) => p.template === "challenger_workflow");
    ck("the first challenger line creates the project", !!project);

    browser = await chromium.launch(process.env.TARES_E2E_CHROME ? { executablePath: process.env.TARES_E2E_CHROME } : {});
    const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });
    await page.goto(`${BASE}/projects/${project.id}`, { waitUntil: "networkidle" });
    await page.getByRole("button", { name: "Summarize a session now" }).click();
    await page.locator(".modal input[type=text]").click();
    await page.locator(".combo-item").first().waitFor();

    const items = await page.locator(".combo-item").count();
    ck("all three sessions are offered", items === SESSIONS.length, `items=${items}`);
    // every row's text, drawn: its box and every child of it inside the viewport, none cut off
    // by an ancestor (an element clipped by the dialog has a smaller visible box than its text)
    const rows = await page.evaluate(() => {
      const clipRect = (el) => {            // the visible box of el after its ancestors clip it
        let r = el.getBoundingClientRect();
        let top = r.top, left = r.left, right = r.right, bottom = r.bottom;
        for (let a = el.parentElement; a; a = a.parentElement) {
          const o = getComputedStyle(a).overflow + getComputedStyle(a).overflowX + getComputedStyle(a).overflowY;
          if (/hidden|clip/.test(o)) {
            const b = a.getBoundingClientRect();
            top = Math.max(top, b.top); left = Math.max(left, b.left);
            right = Math.min(right, b.right); bottom = Math.min(bottom, b.bottom);
          }
        }
        return { w: Math.max(0, right - left), h: Math.max(0, bottom - top), full: r };
      };
      return [...document.querySelectorAll(".combo-item")].map((item) => ({
        text: item.innerText,
        parts: [...item.children].map((c) => {
          const v = clipRect(c);
          return { fullW: v.full.width, fullH: v.full.height, w: v.w, h: v.h,
                   inView: v.full.right <= innerWidth && v.full.bottom <= innerHeight };
        }),
      }));
    });
    for (const row of rows) {
      const name = row.text.split("\n")[0].slice(0, 8);
      ck(`${name}: id and label fully visible (not clipped by the dialog)`,
         row.parts.length === 2 && row.parts.every((p) => Math.abs(p.w - p.fullW) < 1 && Math.abs(p.h - p.fullH) < 1 && p.inView),
         JSON.stringify(row.parts));
    }
    ck("the long branch is in the list", rows.some((r) => r.text.includes(LONG_BRANCH)));

    // the menu keeps the field's width: inside the dialog, nothing to scroll sideways, and each
    // label wraps under its id instead of sitting beside it
    const menu = await page.evaluate(() => {
      const list = document.querySelector(".combo-list").getBoundingClientRect();
      const modal = document.querySelector(".modal").getBoundingClientRect();
      const field = document.querySelector(".modal .combo").getBoundingClientRect();
      const l = document.querySelector(".combo-list");
      return {
        inside: list.left >= modal.left - 0.5 && list.right <= modal.right + 0.5,
        sameWidth: Math.abs(list.width - field.width) < 1,
        noSideScroll: l.scrollWidth <= l.clientWidth,
        stacked: [...document.querySelectorAll(".combo-item")].every((i) => {
          const [id, label] = [...i.children].map((c) => c.getBoundingClientRect());
          return label.top >= id.bottom - 0.5;
        }),
      };
    });
    ck("the menu sits inside the dialog at the field's width", menu.inside && menu.sameWidth, JSON.stringify(menu));
    ck("the menu has nothing to scroll sideways", menu.noSideScroll, JSON.stringify(menu));
    ck("each label sits under its id", menu.stacked, JSON.stringify(menu));
  } finally {
    if (browser) await browser.close();
    daemon.kill();
    fs.rmSync(home, { recursive: true, force: true });
  }
  console.log(`${pass} passed, ${fail} failed`);
  process.exit(fail ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(1); });
