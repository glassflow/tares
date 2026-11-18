// The console's ticket counts against the server's: ticketBucket and countLine
// (src/components/ProgressRing.tsx) must give what factory.count_of and status_words.count_line
// give for every case in tests/count-cases.json. tests/test_ticket_counts_golden.py checks the
// server against the same file, so the two cannot drift apart unnoticed.
// Run: npm run count-parity. Prints one FAIL line per case and exits 1 on any.
const path = require("path");
const fs = require("fs");
const esbuild = require("esbuild");

const UI = path.resolve(__dirname, "..");
const out = esbuild.buildSync({
  entryPoints: [path.join(UI, "src/components/ProgressRing.tsx")], bundle: true, write: false,
  format: "cjs", platform: "node", jsx: "automatic", external: ["react", "react/jsx-runtime"], logLevel: "silent",
});
const mod = { exports: {} };
new Function("module", "exports", "require", out.outputFiles[0].text)(mod, mod.exports, require);
const { ticketBucket, countLine } = mod.exports;

const cases = JSON.parse(fs.readFileSync(path.join(__dirname, "count-cases.json"), "utf8"));
const fails = [];
for (const c of cases.tickets) {
  const got = ticketBucket(c);
  // the console names work under way "doing" too, and to do "todo", as progress() keys them
  if (got !== c.count) fails.push(`FAIL ticketBucket ${JSON.stringify({ stage: c.stage, status: c.status, blocked_by: c.blocked_by, pr: c.pr })}: got ${got}, want ${c.count}`);
}
for (const c of cases.lines) {
  const got = countLine(c.counts);
  if (got !== c.line) fails.push(`FAIL countLine ${JSON.stringify(c.counts)}: got "${got}", want "${c.line}"`);
}
for (const f of fails) console.log(f);
console.log(`${cases.tickets.length + cases.lines.length - fails.length} passed, ${fails.length} failed`);
process.exit(fails.length ? 1 : 0);
