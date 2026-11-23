// Rewrites time-contract.baseline.json: per legacy file, how many one-time violations (locale and
// Intl calls outside src/lib/time.ts and src/lib/number.ts, paper-kit/eslint/rules/one-time.mjs) it may still
// carry. Same ratchet as scripts/kit-baseline.mjs: counts only go down; a higher count, or a new
// file, is refused unless --raise is passed, and a raise is a reviewed change with its reason in
// the PR. Edition code is never written: there the fix is the module, not a number.
//
//   npm run time-baseline            lower counts, drop files now at 0
//   npm run time-baseline -- --raise also accept higher counts
import { readFileSync, writeFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { ESLint } from 'eslint'

const FILE = new URL('../time-contract.baseline.json', import.meta.url)
const ROOT = fileURLToPath(new URL('..', import.meta.url))
const raise = process.argv.includes('--raise')
const old = JSON.parse(readFileSync(FILE, 'utf8'))

// Lint against an empty baseline, so every violation is reported.
writeFileSync(FILE, '{}\n')
let results
try {
  const eslint = new ESLint({ cwd: ROOT, overrideConfigFile: 'eslint-design-contract.config.mjs' })
  results = await eslint.lintFiles(['src/**/*.{ts,tsx}'])
} catch (err) {
  writeFileSync(FILE, JSON.stringify(old, null, 2) + '\n')
  throw err
}

const counts = {}
const edition = []
for (const r of results) {
  const hits = r.messages.filter((m) => m.ruleId === 'time/one-time' && m.messageId !== 'stale')
  if (!hits.length) continue
  const f = r.filePath.slice(ROOT.length).replace(/^\//, '')
  if (hits[0].message.includes('(one-time: edition code)')) edition.push(f)
  else counts[f] = hits.length
}

const raised = Object.entries(counts).filter(([f, n]) => n > (old[f] ?? 0))
const next = raise ? counts : Object.fromEntries(Object.entries(counts).map(([f, n]) => [f, Math.min(n, old[f] ?? 0)]))
const sorted = Object.fromEntries(
  Object.entries(next)
    .filter(([, n]) => n > 0)
    .sort(([a], [b]) => a.localeCompare(b)),
)
writeFileSync(FILE, JSON.stringify(sorted, null, 2) + '\n')

const total = (o) => Object.values(o).reduce((a, b) => a + b, 0)
console.log(`one-time baseline: ${total(old)} → ${total(sorted)} violations in ${Object.keys(sorted).length} files`)
let failed = false
for (const f of edition) {
  console.error(`  refused: ${f} is edition code. Use src/lib/time.ts or src/lib/number.ts.`)
  failed = true
}
if (raised.length && !raise) {
  for (const [f, n] of raised) console.error(`  refused: ${f} ${old[f] ?? 0} → ${n}`)
  console.error('Use src/lib/time.ts, or pass --raise and say why in the PR.')
  failed = true
}
if (failed) process.exit(1)
