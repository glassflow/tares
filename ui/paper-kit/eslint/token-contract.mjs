// vendored from glassflow/arche-design paper-kit-v1 91909425f0335427fe60ca6d879ca56ca2fcce52: do not edit, run npm run paper:sync
// The paper token contract, as a factory: `tokenContract(options)` returns the flat-config array an
// app's `token-contract` script runs. Moved from the Tares console's eslint-token-contract.config.mjs.
//
// Colour comes from a token, never from a literal. In TSX that means a CSS variable
// (`var(--accent)`, `bg-[var(--code-dark-bg)]`) or a utility mapped to one in the vendored
// paper-theme.css (`bg-panel`, `text-muted-foreground`). Four things fail, in a string or a template:
//
//   hex       #e3c777, #fff, #1e0f00cc
//   rgb       rgb(…), rgba(…) with a number inside
//   palette   a stock Tailwind palette utility: bg-amber-200, text-slate-500, ring-red-600/40
//   utility   an arbitrary value naming a token that has a utility: text-[color:var(--err)] is
//             text-err, bg-[var(--row-hover)] is bg-row-hover. The list of such tokens is read
//             from `@theme inline` in the theme CSS, so a role added there is guarded the moment
//             it exists. Tokens with no utility (the code palette) still pass.
//
// CSS is exempt: the stylesheets hold the definitions, and they are outside a TSX gate anyway.
// Raw palette variables (`var(--green-300)`) are tokens and pass; choosing a semantic one
// over a raw one is a review call, not a lint. A `var()` in an inline style passes too: only the
// Tailwind arbitrary-value form has a utility to use instead.
//
// The allow-list is an exception REGISTER, not an escape hatch: each entry carries a reason
// and the ticket that empties it. Inline config is off, so a disable comment exempts nothing.
//
// Shape note: ESLint flat config REPLACES a rule's options when a later object matches the same
// file, so every selector lives in the one SELECTORS map and a register re-declares the rule
// with the subset that still applies to it (same as design-contract.mjs).
//
// Options:
//   files          globs the contract runs on. Default ['src/**/*.{ts,tsx}'].
//   ignores        globs left out. Default [].
//   root           the app root, absolute; `themeCss` paths resolve against it. Default process.cwd().
//   themeCss       CSS files to read `@theme inline` from, relative to root (or absolute). Every
//                  file must exist. Default ['src/paper/paper-theme.css'].
//   register       [{ files: [...], drop: ['hex' | 'rgb' | 'palette' | 'utility'] }]: files exempt
//                  from the named selectors, each with its reason in a comment. Default [].
//   extraTokens    tokens that have a utility but are not in `@theme inline`. Default [].
//   doc            sentence closing the selector messages. Default points at the first themeCss
//                  file and .impeccable.md.
//   appName        word for the messages ('console', 'admin'). Default 'app'.

import { readFileSync } from 'node:fs'
import path from 'node:path'
import tseslint from 'typescript-eslint'

// A hex colour is # and 3, 4, 6 or 8 hex digits, not glued to a word on either side (so an HTML
// entity like &#9662; or an anchor like #section-2 passes). A pull request number such as #595 is
// also three hex digits and fails; an app registers the one file of sample prose with them.
const HEX = '(?<![\\w&])#(?:[0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{3,4})(?![\\w-])'
// Not `\b`: in a Tailwind arbitrary value the colour follows an underscore
// (shadow-[0_1px_3px_rgba(0,0,0,0.07)]), and `_` is a word character, so `\b` never matches there.
const RGB = '(?<![a-zA-Z])rgba?\\(\\s*\\d'
const PALETTE =
  '(?<![\\w-])(?:[\\w-]+:)*!?(?:bg|text|border(?:-[trblxyse])?|ring|ring-offset|outline|divide|fill|stroke|from|via|to|accent|caret|decoration|shadow)-' +
  '(?:red|orange|amber|yellow|lime|green|emerald|teal|cyan|sky|blue|indigo|violet|purple|fuchsia|pink|rose|gray|grey|zinc|neutral|stone|slate)-\\d{2,3}\\b'

const strings = (re) => `Literal[value=/${re}/], TemplateElement[value.raw=/${re}/]`

// The tokens `@theme inline` maps to a utility (`--color-err: var(--err)` makes `--err` one).
// Comments are stripped first: a `}` inside a /* */ comment in the block would end the match early
// and lose every name after it.
const stripComments = (css) => css.replace(/\/\*[\s\S]*?\*\//g, '')
export function themedTokens(css, extraTokens = []) {
  const blocks = [...stripComments(css).matchAll(/@theme inline\s*\{[^}]*\}/g)].map((m) => m[0])
  return blocks
    .flatMap((b) => [...b.matchAll(/--color-[a-z0-9-]+:\s*var\(--([a-z0-9-]+)\)/g)].map((m) => m[1]))
    .concat(extraTokens)
}

export function tokenContract(options = {}) {
  const {
    files = ['src/**/*.{ts,tsx}'],
    ignores = [],
    root = process.cwd(),
    themeCss = ['src/paper/paper-theme.css'],
    register = [],
    extraTokens = [],
    appName = 'app',
  } = options
  const themeName = themeCss[0]
  const DOC = options.doc ?? `Use a CSS variable (var(--token)) or a utility mapped to one in ${themeName}. See .impeccable.md.`

  const THEMED = themeCss.flatMap((f) => themedTokens(readFileSync(path.resolve(root, f), 'utf8')))
  THEMED.push(...extraTokens)
  if (THEMED.length < 20) throw new Error(`token-contract: could not read @theme inline from ${themeName}`)
  const UTILITY = `-\\[(?:color:)?var\\(--(?:${[...new Set(THEMED)].join('|')})\\)\\]`

  const SELECTORS = {
    hex: { selector: strings(HEX), message: `A hex colour literal. ${DOC} (token-contract: hex)` },
    rgb: { selector: strings(RGB), message: `An rgb()/rgba() colour literal. ${DOC} (token-contract: rgb)` },
    palette: {
      selector: strings(PALETTE),
      message: `A stock Tailwind palette colour. The ${appName}'s colours are its own tokens. ${DOC} (token-contract: palette)`,
    },
    utility: {
      selector: strings(UTILITY),
      message: `An arbitrary value for a token that has a utility: write text-err, not text-[color:var(--err)]. The roles and their names are in @theme inline in ${themeName} and in .impeccable.md. (token-contract: utility)`,
    },
  }

  const ALL = Object.keys(SELECTORS)
  const syntax = (keys) => ({ 'no-restricted-syntax': ['error', ...keys.map((k) => SELECTORS[k])] })

  const base = {
    languageOptions: { parser: tseslint.parser, parserOptions: { ecmaFeatures: { jsx: true } } },
    linterOptions: { noInlineConfig: true, reportUnusedDisableDirectives: 'off' },
  }

  // Each register entry re-declares the rule minus the selectors in its `drop`.
  return [
    { ...base, files, ...(ignores.length ? { ignores } : {}), rules: syntax(ALL) },
    ...register.map((r) => ({ ...base, files: r.files, rules: syntax(ALL.filter((k) => !r.drop.includes(k))) })),
  ]
}
