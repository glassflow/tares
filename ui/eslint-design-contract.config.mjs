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
//   motion-contract  Time in motion comes from the tokens (TR-1431, paper-motion.css "Motion"): no
//                    duration-150, duration-[..], delay-.., ease-[..] or animate-[..] utility,
//                    and no raw time in a transition or animation of a style object. Name a
//                    token (duration-(--dur-2), ease-out) or a motion-* class instead. The CSS
//                    side, raw times in src/**/*.css, is tests/motion.check.cjs.
//   kit-only         No inline style and no raw form control in edition code (TR-1258). Its own
//                    rule (eslint-rules/kit-only.mjs) with its own register and per-file baseline,
//                    so it is a separate config object, not a selector. It runs in `npm run lint`
//                    too; it is here because inline config is off here.
//   one-time         Time is said through src/lib/time.ts (TR-1261): no toLocale*String call and
//                    no Intl.DateTimeFormat outside it and src/lib/number.ts. Its own rule
//                    (eslint-rules/one-time.mjs), with a per-file baseline for the legacy pages
//                    (time-contract.baseline.json, lowered by npm run time-baseline).
//
// Not here: a table-head selector. The console has no table primitive in src/components/ui,
// so a raw <th> rule would have nowhere to point; it joins when the primitive does. Stripes and
// shadows in src/styles.css are out of reach of a TSX gate.
//
// The allow-lists are exception REGISTERS, not escape hatches: each entry carries a reason and
// the ticket that empties it. A file belongs to at most one register.
//
// Shape note: ESLint flat config REPLACES a rule's options when a later object matches the same
// file, so every selector lives in the one SELECTORS map and each register re-declares the rule
// with the subset that still applies to it. Never add a second config object for a new selector.

import tseslint from 'typescript-eslint'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import oneTime from './eslint-rules/one-time.mjs'
import { kitOnlyConfig } from './eslint-rules/kit-register.mjs'

// --- repoint on copy ---------------------------------------------------------
const SRC = 'src/**/*.{ts,tsx}'
const PAPER_PROFILE = true
// Empty since TR-1266 put the factory's Calls, Decisions and GitHub cards on the paper. Keep it
// empty: a new entry needs a reason and a ticket, like every register.
const SHADOW_REGISTER = []
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
// A utility that names its own time or curve. duration-(--dur-N) and ease-out/in/in-out read the tokens.
const MOTION = `${B}(duration|delay)-(\\d|\\[)|${B}(ease|animate)-\\[`
const MOTION_KEY = '^(transition|animation)(Duration|Delay|TimingFunction)?$'
const RAW_TIME = '\\d(m?s)\\b|cubic-bezier'
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
  motion: {
    selector:
      `${strings(MOTION)}, ` +
      `Property[key.name=/${MOTION_KEY}/] Literal[value=/${RAW_TIME}/], ` +
      // in a template the unit can follow the expression: `${ms}ms`
      `Property[key.name=/${MOTION_KEY}/] TemplateElement[value.raw=/${RAW_TIME}|^m?s\\b/]`,
    message: `Motion takes its time from the tokens: duration-(--dur-1..4), ease-out / ease-in, or a motion-* class from paper-motion.css. ${DOC} (motion-contract)`,
  },
  emDash: {
    selector: `Literal[value=/${EM_DASH_PROSE}/], TemplateElement[value.cooked=/${EM_DASH_PROSE}/], JSXText[value=/${EM_DASH_PROSE}/]`,
    message: `An em dash in copy: rewrite the sentence with a period, a colon or a comma. A lone '—' as the no-value mark is fine. ${DOC} (voice-contract)`,
  },
}

const ALL = [...(PAPER_PROFILE ? ['rounded'] : []), 'shadow', 'stripe', 'motion', 'emDash']
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
  // kit-only (TR-1258), also in `npm run lint`: here inline config is off, so a disable comment
  // that silences it in lint still fails CI.
  kitOnlyConfig(base),
  // one-time (TR-1261): its own plugin, so it never shares no-restricted-syntax's options.
  {
    ...base,
    files: [SRC],
    plugins: { time: { rules: { 'one-time': oneTime } } },
    rules: {
      'time/one-time': [
        'error',
        {
          baseline: JSON.parse(readFileSync(new URL('./time-contract.baseline.json', import.meta.url), 'utf8')),
          root: fileURLToPath(new URL('.', import.meta.url)),
        },
      ],
    },
  },
]
