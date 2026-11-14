// Config for `npm run design-contract`, the console's design gate (CI job `console`).
// Adapted from arche-ui seed/ci/eslint-design-contract.config.mjs and argus-ui's copy of it.
// It holds the paper conventions that have a code signature, as `.impeccable.md` and the
// Console design section of AGENTS.md state them:
//
//   radius-contract  Everything is square. No rounded-xs..4xl utility (any side) and no
//                    rounded-[..] arbitrary radius; rounded-full stays for dots, switches
//                    and avatars. A bare `rounded` is not caught: in a string it is as often
//                    an English word as a class, so it stays a review rule.
//   shadow-contract  No drop shadow in the page. Two shadows are allowed: the selected item's
//                    inset accent bar (shadow-[inset_3px_0_0_var(--accent)]) and a floating
//                    layer casting var(--sheet-shadow). Stock shadow-xs..2xl, any other
//                    shadow-[..] and a boxShadow in a style object fail.
//   stripe-contract  No side stripes. A left (or inline-start) border wider than 1px fails, as
//                    a class (border-l-2, border-s-[3px]) or in a style object
//                    (borderLeft: "2px solid ..."). A selected row marks its edge with the
//                    inset accent bar above; a section opens with a 1px top rule.
//   voice-contract   Copy carries no em dash. A string or JSX text holding one fails, except a
//                    lone '—' (the no-value mark in a cell). Comments are not copy.
//
// Not here: a table-head selector. The console has no table primitive in src/components/ui,
// so a raw <th> rule would have nowhere to point; it joins when the primitive does. The time
// selector joins with the time module (TR-1261). Stripes and shadows in src/styles.css are out
// of reach of a TSX gate.
//
// The allow-lists are exception REGISTERS, not escape hatches: each entry carries a reason and
// the ticket that empties it. A file belongs to at most one register.
//
// Shape note: ESLint flat config REPLACES a rule's options when a later object matches the same
// file, so every selector lives in the one SELECTORS map and each register re-declares the rule
// with the subset that still applies to it. Never add a second config object for a new selector.

import tseslint from 'typescript-eslint'

// --- repoint on copy ---------------------------------------------------------
const SRC = 'src/**/*.{ts,tsx}'
const PAPER_PROFILE = true
// Card shadows with a raw colour. TR-1266 replaces them with 1px rules and empties this.
const SHADOW_REGISTER = [
  'src/editions/factory/FactoryCalls.tsx',
  'src/editions/factory/FactoryDecisions.tsx',
  'src/editions/factory/FactoryProjectGitHub.tsx',
]
// A 2px neutral rail that nests child runs and rule firings under their parent on the legacy
// project activity page: a tree guide in --line, not an accent callout. The legacy pages move
// onto the edition kit or freeze (TR-1250, TR-1267); the rail goes with that.
const STRIPE_REGISTER = ['src/components/ProjectActivity.tsx']
// -----------------------------------------------------------------------------

const DOC = 'See .impeccable.md and the Console design section of AGENTS.md.'
// Any string holding an em dash is prose, except a lone '—' (whitespace around it allowed).
// A dash with a space on both sides joins prose and fails even alone in its literal.
const EM_DASH_PROSE = '^(?!\\s*—\\s*$)[\\s\\S]*—|[ \\t]—[ \\t]'
// The boundary is "not a word character or a dash", so md:rounded-lg and !rounded-lg are caught
// while a longer name ending in the same letters is not.
const B = '(?<![\\w-])'
const ROUNDED = `${B}rounded(-(t|r|b|l|s|e|tl|tr|br|bl|ss|se|es|ee))?-(xs|sm|md|lg|xl|2xl|3xl|4xl|\\[)`
// Stock sizes, and any arbitrary shadow that is neither inset nor the floating layer's.
const SHADOW = `${B}shadow-(xs|sm|md|lg|xl|2xl)(?![\\w-])|${B}shadow-\\[(?!inset)(?![^\\]]*--sheet-shadow)`
// A left or inline-start border wider than 1px: border-l-2..N, border-l-[3px]. border-l, -0, -1
// and a colour (border-l-[color:..], border-l-transparent) stay allowed.
const STRIPE = `${B}border-(l|s)-([2-9]|[1-9]\\d+|\\[\\d)`
const WIDE = '^\\s*(?:[2-9]|[1-9]\\d+|1\\.\\d*[1-9])'

const strings = (re) => `Literal[value=/${re}/], TemplateElement[value.raw=/${re}/]`

const SELECTORS = {
  rounded: {
    selector: strings(ROUNDED),
    message: `Radius is 0 in the paper direction: drop the rounded-* utility (rounded-full stays for dots, switches and avatars). ${DOC} (radius-contract)`,
  },
  shadow: {
    selector: `${strings(SHADOW)}, Property[key.name='boxShadow'] Literal[value=/[\\s\\S]/]:not([value=/^(none|inset\\b.*|.*--sheet-shadow.*)$/])`,
    message: `No drop shadow in the page: separate with a 1px rule. Allowed: the selected item's shadow-[inset_3px_0_0_var(--accent)] and a floating layer's var(--sheet-shadow). ${DOC} (shadow-contract)`,
  },
  stripe: {
    selector:
      `${strings(STRIPE)}, ` +
      `Property[key.name=/^border(Left|InlineStart)(Width)?$/] Literal[value=/${WIDE}/], ` +
      `Property[key.name=/^border(Left|InlineStart)(Width)?$/] Literal[value>1]`,
    message: `No side stripes: a left border wider than 1px. Mark a selected row with shadow-[inset_3px_0_0_var(--accent)]; open a section with a 1px top rule. ${DOC} (stripe-contract)`,
  },
  emDash: {
    selector: `Literal[value=/${EM_DASH_PROSE}/], TemplateElement[value.cooked=/${EM_DASH_PROSE}/], JSXText[value=/${EM_DASH_PROSE}/]`,
    message: `An em dash in copy: rewrite the sentence with a period, a colon or a comma. A lone '—' as the no-value mark is fine. ${DOC} (voice-contract)`,
  },
}

const ALL = [...(PAPER_PROFILE ? ['rounded'] : []), 'shadow', 'stripe', 'emDash']
const syntax = (keys) => ({ 'no-restricted-syntax': ['error', ...keys.map((k) => SELECTORS[k])] })
const without = (...drop) => ALL.filter((k) => !drop.includes(k))

const base = {
  languageOptions: { parser: tseslint.parser, parserOptions: { ecmaFeatures: { jsx: true } } },
  linterOptions: { noInlineConfig: true, reportUnusedDisableDirectives: 'off' },
}

// Registers: each re-declares the rule minus the selectors it is exempt from. An empty register
// adds no object (ESLint refuses an empty `files`), so emptying one is a one-line change.
const register = (files, ...drop) => (files.length ? [{ ...base, files, rules: syntax(without(...drop)) }] : [])

export default [
  { ...base, files: [SRC], rules: syntax(ALL) },
  ...register(SHADOW_REGISTER, 'shadow'),
  ...register(STRIPE_REGISTER, 'stripe'),
]
