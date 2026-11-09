/*
 * The words a refused team action carries out of the API layer (TR-1088). No browser: api.ts is bundled with
 * esbuild and run in node with a made-up fetch, so a 402 answered BEFORE /health has landed (the case the
 * browser suites cannot time) is covered.
 *  - request(): the 402 `licence_required` answer becomes a LicenceRequired. Its message and url are the cell's
 *    own only once /health has said the cell is self-hosted; before /health, when /health failed and on a Tares
 *    Cloud cell they are the plain sentence and no link.
 *  - cloudApi: the control plane's 402 is always the plain sentence.
 * Run:  node tests/licence-words.e2e.cjs      (TARES_E2E_PORT is not used)
 */
const fs = require("fs");
const os = require("os");
const path = require("path");

const UI_DIR = path.resolve(__dirname, "..");
const esbuild = require(path.join(UI_DIR, "node_modules", "esbuild"));

let pass = 0, fail = 0;
const ck = (label, cond, detail = "") => {
  cond ? pass++ : fail++;
  console.log((cond ? "  ok   " : "  FAIL ") + label + (cond ? "" : `   ${detail}`));
};

const URL_ENTERPRISE = "https://docs.glassflow.ai/tares/enterprise";
const OWN = { code: "licence_required", message: "Adding people needs an enterprise licence.", url: URL_ENTERPRISE };
const PLAIN = "That is not available on this workspace right now.";
const BAD = /licen[cs]e|enterprise|\bkey\b/i;

(async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "tares-licence-words-"));
  // one bundle holds api.ts and licenceWords.ts, so noteHealth moves the state api.ts reads
  const entry = path.join(dir, "entry.ts");
  fs.writeFileSync(entry, `export * from ${JSON.stringify(path.join(UI_DIR, "src", "api.ts"))};\n` +
                          `export { noteHealth, licenceWordsOn, licenceKnown } from ${JSON.stringify(path.join(UI_DIR, "src", "licenceWords.ts"))};\n`);
  const both = path.join(dir, "both.cjs");
  await esbuild.build({ entryPoints: [entry], bundle: true, platform: "node", format: "cjs", outfile: both, logLevel: "silent" });
  const store = new Map();
  globalThis.localStorage = { getItem: (k) => store.get(k) ?? null, setItem: (k, v) => store.set(k, v), removeItem: (k) => store.delete(k) };
  globalThis.window = { location: { href: "http://cell.test/" }, dispatchEvent: () => true };
  let answer = { status: 402, body: OWN };
  globalThis.fetch = async () => ({
    ok: answer.status < 400, status: answer.status, statusText: "Payment Required",
    headers: { get: () => null }, json: async () => answer.body,
  });
  const m = require(both);

  async function refused() {
    try { await m.request("/api/people", { method: "POST" }); } catch (e) { return e; }
    return null;
  }

  console.log("== request(): a 402 licence_required ==");
  answer = { status: 402, body: OWN };
  let e = await refused();   // before /health has answered
  ck("before /health: a LicenceRequired", m.isLicenceRequired(e), String(e));
  ck("before /health: the plain sentence, not the cell's words", e && e.message === PLAIN && !BAD.test(e.message), e && e.message);
  ck("before /health: no link", e && e.url === "", e && e.url);
  ck("before /health: the words are not on", m.licenceWordsOn() === false && m.licenceKnown() === false);

  m.noteHealth({ ok: true });   // /health answered: no workspace_api_url, a self-hosted cell
  e = await refused();
  ck("self-hosted: the cell's own words", e && e.message === OWN.message, e && e.message);
  ck("self-hosted: the enterprise page", e && e.url === URL_ENTERPRISE, e && e.url);
  answer = { status: 402, body: { code: "licence_required" } };
  e = await refused();
  ck("self-hosted, a bare answer: the console's sentence and the enterprise page",
     e && e.message === "That needs an enterprise licence." && e.url === URL_ENTERPRISE, e && `${e.message} ${e.url}`);

  answer = { status: 402, body: OWN };
  m.noteHealth({ workspace_api_url: "https://workspace-api.example.test" });
  e = await refused();
  ck("Tares Cloud: the plain sentence", e && e.message === PLAIN && !BAD.test(e.message), e && e.message);
  ck("Tares Cloud: no link", e && e.url === "", e && e.url);

  m.noteHealth();   // /health failed: not known to be self-hosted
  e = await refused();
  ck("/health failed: the plain sentence", e && e.message === PLAIN && !BAD.test(e.message), e && e.message);
  ck("/health failed: no link", e && e.url === "", e && e.url);
  ck("/health failed: the words are off but the answer is known", m.licenceWordsOn() === false && m.licenceKnown() === true);

  m.noteHealth({});   // a later answer settles it
  e = await refused();
  ck("a later /health answer settles it: the cell's words again", e && e.message === OWN.message && e.url === URL_ENTERPRISE);

  console.log("== cloudApi: the control plane's 402 ==");
  for (const [label, h] of [["self-hosted", {}], ["Tares Cloud", { workspace_api_url: "https://x.test" }], ["/health failed", undefined]]) {
    m.noteHealth(h);
    answer = { status: 402, body: { code: "licence_required", message: "Inviting people needs an enterprise licence.", url: URL_ENTERPRISE } };
    const r = await m.cloudApi.invite("https://workspace-api.example.test/api/ws", "acme", "sam@example.com");
    ck(`${label}: an invite answered 402 is an error`, r.status === "error" && r.code === 402, JSON.stringify(r));
    if (label === "self-hosted") ck(`${label}: it says what the self-hosted words say`, /licence/.test(r.detail), r.detail);
    else ck(`${label}: it is the plain sentence`, r.detail === PLAIN && !BAD.test(r.detail), r.detail);
  }

  fs.rmSync(dir, { recursive: true, force: true });
  console.log(`\n${pass} passed, ${fail} failed`);
  process.exit(fail ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(1); });
