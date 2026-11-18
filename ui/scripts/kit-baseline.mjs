// Rewrites kit-contract.baseline.json: per file, how many kit-only violations (inline styles and raw
// form controls, eslint-rules/kit-only.mjs) it may still carry. The baseline only goes down. A file
// whose count rose, or a new file with a violation, is refused unless --raise is passed, and a raise
// is a reviewed change with its reason in the PR. An edition file is never written unless it is in
// the register (eslint-rules/kit-register.mjs): there the fix is the kit, not a number.
//
//   npm run kit-baseline            lower counts, drop files now at 0
//   npm run kit-baseline -- --raise also accept higher counts
//
// Adapted from argus-ui scripts/copy-budget-baseline.mjs.
import { readFileSync, writeFileSync } from 'node:fs'
import { ESLint } from 'eslint'
import { EDITION, KIT_REGISTER } from '../eslint-rules/kit-register.mjs'

const FILE = new URL('../kit-contract.baseline.json', import.meta.url)
const raise = process.argv.includes('--raise')
const old = JSON.parse(readFileSync(FILE, 'utf8'))

// Lint against an empty baseline, so every violation is reported.
writeFileSync(FILE, '{}\n')
let results
try {
  const eslint = new ESLint({ cwd: new URL('..', import.meta.url).pathname })
  results = await eslint.lintFiles(['src/**/*.tsx'])
} catch (err) {
  writeFileSync(FILE, JSON.stringify(old, null, 2) + '\n')
  throw err
}

const counts = {}
for (const r of results) {
  const n = r.messages.filter((m) => m.ruleId === 'kit/kit-only' && (m.messageId === 'style' || m.messageId === 'control')).length
  if (n) counts[r.filePath.slice(process.cwd().length + 1)] = n
}

const edition = (f) => EDITION.some((p) => f.startsWith(p))
const unregistered = Object.keys(counts).filter((f) => edition(f) && !Object.hasOwn(KIT_REGISTER, f))
for (const f of unregistered) delete counts[f]

const raised = Object.entries(counts).filter(([f, n]) => n > (old[f] ?? 0))
const next = raise
  ? counts
  : Object.fromEntries(Object.entries(counts).map(([f, n]) => [f, Math.min(n, old[f] ?? 0)]))
const sorted = Object.fromEntries(
  Object.entries(next)
    .filter(([, n]) => n > 0)
    .sort(([a], [b]) => a.localeCompare(b)),
)
writeFileSync(FILE, JSON.stringify(sorted, null, 2) + '\n')

const total = (o) => Object.values(o).reduce((a, b) => a + b, 0)
console.log(`kit-only baseline: ${total(old)} → ${total(sorted)} violations in ${Object.keys(sorted).length} files`)
let failed = false
for (const f of unregistered) {
  console.error(`  refused: ${f} is edition code. Use the kit, or register the file with its reason.`)
  failed = true
}
if (raised.length && !raise) {
  for (const [f, n] of raised) console.error(`  refused: ${f} ${old[f] ?? 0} → ${n}`)
  console.error('Use the kit, or pass --raise and say why in the PR.')
  failed = true
}
if (failed) process.exit(1)
