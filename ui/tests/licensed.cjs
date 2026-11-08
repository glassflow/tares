/*
 * TR-926: starts a scratch daemon that trusts the test licence root, for the browser suites that need a
 * licence (the enterprise screens). Never `tares up` there: the root is installed in-process only, so the
 * daemon starts through `python ee/tests/licensed.py serve` (TR-1047).
 *
 *   const { startDaemon } = require("./licensed.cjs");
 *   const d = await startDaemon({ port, dataDir, edition: "factory", licence: "valid", auth: "tok" });
 *   ... d.base, d.stop()
 *
 * licence: "none" | "valid" | "expired" | "over_seats" | "invalid" (default "valid"; invalid puts text that is not a
 * licence in TARES_LICENSE). The daemon gets a clean
 * environment (no TARES_* or FACTORY_* of the machine), the console in ui/dist (TARES_UI_DIST overrides)
 * and the code under test on PYTHONPATH. Python: TARES_E2E_PY, else the repo's .venv, else python3.
 */
const { spawn } = require("child_process");
const fs = require("fs");
const http = require("http");
const os = require("os");
const path = require("path");

const UI_DIR = path.resolve(__dirname, "..");
const REPO = path.resolve(UI_DIR, "..");

function cleanEnv(extra) {
  const keep = ["PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "SHELL", "USER"];
  const env = {};
  for (const k of keep) if (process.env[k] !== undefined) env[k] = process.env[k];
  return { ...env, ...extra };
}

function python() {
  if (process.env.TARES_E2E_PY) return process.env.TARES_E2E_PY;
  const venv = path.join(REPO, ".venv/bin/python");
  return fs.existsSync(venv) ? venv : "python3";
}

function healthy(url) {
  return new Promise((res) => http.get(url, (r) => { r.resume(); res(r.statusCode > 0); }).on("error", () => res(false)));
}

async function startDaemon({ port, dataDir, edition, licence = "valid", auth } = {}) {
  if (!port) throw new Error("startDaemon: port is required");
  if (!["none", "valid", "expired", "over_seats", "invalid"].includes(licence)) throw new Error(`startDaemon: unknown licence ${licence}`);
  const home = dataDir || fs.mkdtempSync(path.join(os.tmpdir(), "tares-licensed-e2e-"));
  const dist = process.env.TARES_UI_DIST || path.join(UI_DIR, "dist");
  const extra = {
    TARES_HOME: home, TARES_UI_DIST: dist, PYTHONPATH: REPO,
    TARES_MCP_PORT: String(port + 1), TARES_OTLP_GRPC_PORT: "off", TARES_PR_SYNC_SECONDS: "off",
  };
  if (edition) extra.TARES_EDITION = edition;
  const args = [path.join(REPO, "ee/tests/licensed.py"), "serve", "--port", String(port), "--data-dir", home, "--licence", licence];
  if (auth) args.push(`--auth=${auth}`);
  const child = spawn(python(), args, { cwd: REPO, stdio: "ignore", env: cleanEnv(extra), detached: true });
  let exited = false;
  child.on("exit", () => { exited = true; });
  const base = `http://127.0.0.1:${port}`;
  const stop = () => { try { process.kill(child.pid, "SIGTERM"); } catch { /* already gone */ } };
  for (let i = 0; i < 150; i++) {   // 30 s
    if (exited) break;
    if (await healthy(`${base}/health`)) return { base, port, dataDir: home, pid: child.pid, stop };
    await new Promise((r) => setTimeout(r, 200));
  }
  stop();
  throw new Error(`startDaemon: no answer from ${base}/health within 30 s${exited ? " (the daemon exited)" : ""}`);
}

module.exports = { startDaemon };
