// Config for `npm run design-contract`, the console's design gate (CI job `console`).
//
// The rules live in the shared paper kit, vendored at ui/paper-kit/ (`npm run paper:sync`, never
// edited here): radius, shadow, stripe, voice (em dash) and motion as selectors, plus the kit-only
// and one-time rules. What the console decides is below: where the contract runs, its exception
// registers and its two baselines. The rules and what they say are in ui/paper-kit/README.md and
// ui/paper-kit/eslint/design-contract.mjs; the conventions are in `.impeccable.md` and the Console
// design section of AGENTS.md.
//
// The registers are exception REGISTERS, not escape hatches: each entry carries a reason and the
// ticket that empties it. A file belongs to at most one register. Inline config is off, so an
// `eslint-disable` comment exempts nothing.
//
// Not here: a table-head selector. The console has no table primitive in src/components/ui,
// so a raw <th> rule would have nowhere to point; it joins when the primitive does. Stripes and
// shadows in src/styles.css are out of reach of a TSX gate.

import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { designContract } from './paper-kit/eslint/design-contract.mjs'
import { EDITION, KIT_REGISTER } from './eslint-rules/kit-register.mjs'

const json = (f) => JSON.parse(readFileSync(new URL(f, import.meta.url), 'utf8'))

export default designContract({
  files: ['src/**/*.{ts,tsx}'],
  root: fileURLToPath(new URL('.', import.meta.url)),
  appName: 'console',
  registers: {
    // Empty since TR-1266 put the factory's Calls, Decisions and GitHub cards on the paper. Keep it
    // empty: a new entry needs a reason and a ticket, like every register.
    shadow: [],
    // A 2px neutral rail that nests child runs and rule firings under their parent on the legacy
    // project activity page: a tree guide in --line, not an accent callout. The legacy pages move
    // onto the edition kit or freeze (TR-1250, TR-1267); the rail goes with that.
    stripe: ['src/components/ProjectActivity.tsx'],
    // kit-only (TR-1258): scope, register and reasons are in eslint-rules/kit-register.mjs.
    kit: KIT_REGISTER,
    kitEdition: EDITION,
  },
  // Per-file counts the legacy files may still carry; they only go down (npm run time-baseline,
  // npm run kit-baseline).
  baselines: { time: json('./time-contract.baseline.json'), kit: json('./kit-contract.baseline.json') },
  // The kit's own primitives (components/ui) and the dev gallery are out of scope for kit-only.
  kitOnly: { files: ['src/**/*.tsx'], ignores: ['src/components/ui/**', 'src/pages/dev/**'] },
})
