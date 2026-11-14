// Config for `npm run token-contract`, the console's colour gate (CI job `console`).
// Adapted from arche-ui seed/ci/eslint-token-contract.config.mjs and argus-ui's copy of it.
//
// Colour comes from a token, never from a literal. In TSX that means a CSS variable
// (`var(--accent)`, `bg-[var(--code-dark-bg)]`) or a utility mapped to one in src/styles.css
// (`bg-panel`, `text-muted-foreground`). Three things fail, in a string or a template:
//
//   hex       #e3c777, #fff, #1e0f00cc
//   rgb       rgb(…), rgba(…) with a number inside
//   palette   a stock Tailwind palette utility: bg-amber-200, text-slate-500, ring-red-600/40
//
// CSS is exempt: src/styles.css holds the definitions, and it is outside a TSX gate anyway.
// Raw palette variables (`var(--green-300)`) are tokens and pass; choosing a semantic one
// over a raw one is a review call, not a lint.
//
// The allow-list is an exception REGISTER, not an escape hatch: each entry carries a reason
// and the ticket that empties it. Inline config is off, so a disable comment exempts nothing.
//
// Shape note: ESLint flat config REPLACES a rule's options when a later object matches the same
// file, so every selector lives in the one SELECTORS map and a register re-declares the rule
// with the subset that still applies to it (same as eslint-design-contract.config.mjs).

import tseslint from 'typescript-eslint'

// --- repoint on copy ---------------------------------------------------------
const SRC = 'src/**/*.{ts,tsx}'
// The first run (TR-1256) found no colour literal left in the edition pages: TR-1273 and TR-1266
// had moved them onto tokens. A new entry needs a reason and the ticket that removes it.
const REGISTER = [
  {
    // Sample crew transcripts: "#595" is a pull request number in prose, and three hex digits
    // look the same as a colour. The file holds no styling. Permanent while the samples quote PRs.
    files: ['src/editions/factory/crewSamples.ts'],
    drop: ['hex'],
  },
]
// -----------------------------------------------------------------------------

const DOC = 'Use a CSS variable (var(--token)) or a utility mapped to one in src/styles.css. See .impeccable.md.'
// A hex colour is # and 3, 4, 6 or 8 hex digits, not glued to a word on either side (so an HTML
// entity like &#9662; or an anchor like #section-2 passes). A pull request number such as #595 is
// also three hex digits and fails; the register below holds the one file of sample prose with them.
const HEX = '(?<![\\w&])#(?:[0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{3,4})(?![\\w-])'
// Not `\b`: in a Tailwind arbitrary value the colour follows an underscore
// (shadow-[0_1px_3px_rgba(0,0,0,0.07)]), and `_` is a word character, so `\b` never matches there.
const RGB = '(?<![a-zA-Z])rgba?\\(\\s*\\d'
const PALETTE =
  '(?<![\\w-])(?:[\\w-]+:)*!?(?:bg|text|border(?:-[trblxyse])?|ring|ring-offset|outline|divide|fill|stroke|from|via|to|accent|caret|decoration|shadow)-' +
  '(?:red|orange|amber|yellow|lime|green|emerald|teal|cyan|sky|blue|indigo|violet|purple|fuchsia|pink|rose|gray|grey|zinc|neutral|stone|slate)-\\d{2,3}\\b'

const strings = (re) => `Literal[value=/${re}/], TemplateElement[value.raw=/${re}/]`

const SELECTORS = {
  hex: { selector: strings(HEX), message: `A hex colour literal. ${DOC} (token-contract: hex)` },
  rgb: { selector: strings(RGB), message: `An rgb()/rgba() colour literal. ${DOC} (token-contract: rgb)` },
  palette: {
    selector: strings(PALETTE),
    message: `A stock Tailwind palette colour. The console's colours are its own tokens. ${DOC} (token-contract: palette)`,
  },
}

const ALL = Object.keys(SELECTORS)
const syntax = (keys) => ({ 'no-restricted-syntax': ['error', ...keys.map((k) => SELECTORS[k])] })

const base = {
  languageOptions: { parser: tseslint.parser, parserOptions: { ecmaFeatures: { jsx: true } } },
  linterOptions: { noInlineConfig: true, reportUnusedDisableDirectives: 'off' },
}

// Each register entry re-declares the rule minus the selectors in its `drop`.
export default [
  { ...base, files: [SRC], rules: syntax(ALL) },
  ...REGISTER.map((r) => ({ ...base, files: r.files, rules: syntax(ALL.filter((k) => !r.drop.includes(k))) })),
]
