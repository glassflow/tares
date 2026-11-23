// Config for `npm run token-contract`, the console's colour gate (CI job `console`).
//
// The rule lives in the shared paper kit, vendored at ui/paper-kit/ (`npm run paper:sync`, never
// edited here): no hex, no rgb(), no stock Tailwind palette colour, and no arbitrary value for a
// token that has a utility, in a string or a template. What the console decides is below: where it
// runs, which CSS files hold `@theme inline`, and the one register. The rule and its reasons are in
// ui/paper-kit/eslint/token-contract.mjs.
//
// CSS is exempt: src/styles.css and the vendored paper files hold the definitions.
// The register is an exception REGISTER, not an escape hatch: each entry carries a reason and the
// ticket that removes it. Inline config is off, so a disable comment exempts nothing.

import { fileURLToPath } from 'node:url'
import { tokenContract } from './paper-kit/eslint/token-contract.mjs'

export default tokenContract({
  files: ['src/**/*.{ts,tsx}'],
  root: fileURLToPath(new URL('.', import.meta.url)),
  appName: 'console',
  // The map lives in the vendored src/paper/paper-theme.css (TR-1565); a block left in
  // src/styles.css counts too.
  themeCss: ['src/paper/paper-theme.css', 'src/styles.css'],
  // `--ink-soft` has a utility but no `--color-` line: it is the same role as muted-foreground and
  // has no other name.
  extraTokens: ['ink-soft'],
  register: [
    {
      // Sample crew transcripts: "#595" is a pull request number in prose, and three hex digits
      // look the same as a colour. The file holds no styling. Permanent while the samples quote PRs.
      // The merge queue samples' "In line behind #219." is the same.
      files: ['src/editions/factory/crewSamples.ts', 'src/editions/factory/mergeQueueSamples.ts'],
      drop: ['hex'],
    },
  ],
})
