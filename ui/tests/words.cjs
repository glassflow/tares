/*
 * TR-726: the words rule for the factory and sre editions, for the node scripts.
 *   offendersIn(text)               the forbidden words and "em dash" in a text (same rule as src/editions/words.ts
 *                                   and tares/words.py; the list is src/editions/words.json)
 *   checkWords(page, ck, label)     the text a person sees on the page, minus [data-words-exempt] and [data-user-text]
 *   expectOneCall(page, route)      the screen's one read call; call it before page.goto
 * Every edition e2e script calls checkWords once per state and expectOneCall for each state that loads live data.
 */
const fs = require("fs");
const path = require("path");

const WORDS = JSON.parse(fs.readFileSync(path.join(__dirname, "..", "src", "editions", "words.json"), "utf8"));
const EM_DASH = WORDS.punctuation[0];
const WORD_CHAR = "A-Za-z0-9_";
const escape = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

const RULES = WORDS.forbidden.map((e) => ({
  allow: e.allow.map((a) => new RegExp(`(?<![${WORD_CHAR}])${escape(a.replace(/\s+/g, " ").trim())}(?![${WORD_CHAR}])`, "gi")),
  find: new RegExp(`(?<![${WORD_CHAR}])(?:${escape(e.plural)}|${escape(e.word)})(?![${WORD_CHAR}])`, "gi"),
}));

function offendersIn(text) {
  const t = String(text).replace(/\s+/g, " ");
  const found = [];
  if (t.includes(EM_DASH)) found.push({ at: t.indexOf(EM_DASH), name: "em dash" });
  for (const r of RULES) {
    let clean = t;
    for (const a of r.allow) clean = clean.replace(a, (m) => " ".repeat(m.length));
    for (const m of clean.matchAll(r.find)) found.push({ at: m.index, name: m[0].toLowerCase() });
  }
  found.sort((a, b) => a.at - b.at);
  return [...new Set(found.map((f) => f.name))];
}

// What a person reads: innerText with the exempt regions taken out (raw tool output, documents,
// names people chose). They are hidden for the read and shown again right after.
const READ_TEXT = () => {
  const style = document.createElement("style");
  style.textContent = "[data-words-exempt],[data-user-text]{display:none !important}";
  document.head.appendChild(style);
  try {
    return document.body.innerText;
  } finally {
    style.remove();
  }
};

async function checkWords(page, ck, label) {
  const text = await page.evaluate(READ_TEXT);
  const found = offendersIn(text);
  const dashes = found.filter((w) => w === "em dash");
  const names = found.filter((w) => w !== "em dash");
  ck(`${label}: no internal names`, names.length === 0, names.join(", "));
  ck(`${label}: no em dashes`, dashes.length === 0, dashes.join(", "));
}

// A screen loads from exactly one read route. Records every /api/ request until the page is idle;
// the returned check says exactly one GET went to `route` (query string ignored) and no other GET
// went to /api/. /api/whoami (the shell's edition read) is not counted, and nothing after the check
// is (a person's clicks are actions).
function expectOneCall(page, route) {
  const gets = [];
  let recording = true;
  const onRequest = (req) => {
    if (!recording || req.method() !== "GET") return;
    const p = new URL(req.url()).pathname;
    if (p.startsWith("/api/") && p !== "/api/whoami") gets.push(p);
  };
  page.on("request", onRequest);
  return async (ck, label) => {
    await page.waitForLoadState("networkidle");
    recording = false;
    page.off("request", onRequest);
    const hits = gets.filter((p) => p === route);
    const others = gets.filter((p) => p !== route);
    ck(`${label}: one call to ${route}`, hits.length === 1, `${hits.length} calls`);
    ck(`${label}: no other read call`, others.length === 0, others.join(", "));
  };
}

module.exports = { offendersIn, checkWords, expectOneCall, WORDS };
