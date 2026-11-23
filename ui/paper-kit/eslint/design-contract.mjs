// vendored from glassflow/arche-design paper-kit-v1 91909425f0335427fe60ca6d879ca56ca2fcce52: do not edit, run npm run paper:sync
// The paper design contract, as a factory: `designContract(options)` returns the flat-config array an
// app's `design-contract` script runs (`eslint --quiet --config <app config> "src/**/*.{ts,tsx}"`).
// Moved from the Tares console's eslint-design-contract.config.mjs; both apps run the same gate, and
// everything that differs between them (paths, registers, baselines) comes in as an option.
//
// It holds the paper conventions that have a code signature:
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
//   motion-contract  Time in motion comes from the tokens (paper-motion.css "Motion"): no
//                    duration-150, duration-[..], delay-.., ease-[..] or animate-[..] utility,
//                    and no raw time in a transition or animation of a style object. Name a
//                    token (duration-(--dur-2), ease-out) or a motion-* class instead. The CSS
//                    side, raw times in .css files, is the app's own motion check.
//   kit-only         No inline style and no raw form control (rules/kit-only.mjs): its own rule
//                    with its own register and per-file baseline, so it is a separate config
//                    object, not a selector. Apps run it in their lint too, through
//                    `kitOnlyConfig` below; it is here because inline config is off here.
//   one-time         Time is said through the app's time module (rules/one-time.mjs): no
//                    toLocale*String call and no Intl.DateTimeFormat outside the time and number
//                    modules, with a per-file baseline for legacy pages.
//
// The allow-lists are exception REGISTERS, not escape hatches: each entry carries a reason and
// the ticket that empties it. A file belongs to at most one register. Inline config is off, so an
// eslint-disable comment exempts nothing.
//
// Shape note: ESLint flat config REPLACES a rule's options when a later object matches the same
// file, so every selector lives in the one SELECTORS map and each register re-declares the rule
// with the subset that still applies to it. Never add a second config object for a new selector.
//
// Options (all optional; paths are relative to `root`, with forward slashes):
//   files          globs the contract runs on. Default ['src/**/*.{ts,tsx}'].
//   ignores        globs left out of the selector rules. Default [].
//   root           the app root, absolute (path.relative base for the per-file rules).
//                  Default process.cwd().
//   registers      { shadow: string[], stripe: string[], kit: { file: reason }, kitEdition: string[] }
//                  files exempt from a selector (shadow, stripe), and the kit-only register with the
//                  path prefixes that make a file edition code.
//   baselines      { time: { file: count }, kit: { file: count } } parsed JSON the app reads itself.
//   paperProfile   true (default): the radius rule runs. false drops it for an app not on paper.
//   doc            the sentence closing each selector message. Default 'See .impeccable.md and the
//                  Console design section of AGENTS.md.'
//   appName        word for the one-time messages ('console', 'admin'). Default 'app'.
//   kitOnly        { files, ignores, doc, registerFile, baselineFile, baselineCommand, kitDir }:
//                  scope and message words for kit-only. Default files ['src/**/*.tsx'].
//   time           { modules, legacy, strict, doc, baselineFile, baselineCommand }: paths and
//                  message words for one-time. See rules/one-time.mjs.

import tseslint from 'typescript-eslint'
import oneTime from './rules/one-time.mjs'
import kitOnly from './rules/kit-only.mjs'

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
// Any string holding an em dash is prose, except a lone '—' (whitespace around it allowed).
// A dash with a space on both sides joins prose and fails even alone in its literal.
const EM_DASH_PROSE = '^(?!\\s*—\\s*$)[\\s\\S]*—|[ \\t]—[ \\t]'

const DEFAULT_DOC = 'See .impeccable.md and the Console design section of AGENTS.md.'
const DEFAULT_FILES = ['src/**/*.{ts,tsx}']

const strings = (re) => `Literal[value=/${re}/], TemplateElement[value.raw=/${re}/]`

const selectors = (DOC) => ({
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
})

const base = {
  languageOptions: { parser: tseslint.parser, parserOptions: { ecmaFeatures: { jsx: true } } },
  linterOptions: { noInlineConfig: true, reportUnusedDisableDirectives: 'off' },
}

// The flat-config block for kit-only. An app's lint and its design contract both use it: lint gives
// the feedback while writing; the design contract runs it again with inline config off, so an
// `eslint-disable` comment cannot switch it off in CI. Pass `extra` (the contract's `base`) to turn
// inline config off; leave it out for lint.
//   kitOnlyConfig({ register, edition, baseline, root, files, ignores, ...words }, extra)
export function kitOnlyConfig(options = {}, extra = {}) {
  const { files = ['src/**/*.tsx'], ignores = [], register = {}, edition = [], baseline = {}, root = process.cwd(), ...words } = options
  return {
    ...extra,
    files,
    ignores,
    plugins: { kit: { rules: { 'kit-only': kitOnly } } },
    rules: { 'kit/kit-only': ['error', { baseline, register, edition, root, ...words }] },
  }
}

export function designContract(options = {}) {
  const {
    files = DEFAULT_FILES,
    ignores = [],
    root = process.cwd(),
    registers = {},
    baselines = {},
    paperProfile = true,
    doc = DEFAULT_DOC,
    appName = 'app',
    kitOnly: kitOnlyOptions = {},
    time = {},
  } = options
  const SELECTORS = selectors(doc)
  const ALL = [...(paperProfile ? ['rounded'] : []), 'shadow', 'stripe', 'motion', 'emDash']
  const syntax = (keys) => ({ 'no-restricted-syntax': ['error', ...keys.map((k) => SELECTORS[k])] })
  const without = (...drop) => ALL.filter((k) => !drop.includes(k))

  // Registers: each re-declares the rule minus the selectors it is exempt from. An empty register
  // adds no object (ESLint refuses an empty `files`), so emptying one is a one-line change.
  const register = (list = [], ...drop) => (list.length ? [{ ...base, files: list, rules: syntax(without(...drop)) }] : [])

  return [
    { ...base, files, ...(ignores.length ? { ignores } : {}), rules: syntax(ALL) },
    ...register(registers.shadow, 'shadow'),
    ...register(registers.stripe, 'stripe'),
    // kit-only, also in the app's lint: here inline config is off, so a disable comment that
    // silences it in lint still fails CI.
    kitOnlyConfig(
      {
        root,
        register: registers.kit ?? {},
        edition: registers.kitEdition ?? [],
        baseline: baselines.kit ?? {},
        ...kitOnlyOptions,
      },
      base,
    ),
    // one-time: its own plugin, so it never shares no-restricted-syntax's options.
    {
      ...base,
      files,
      plugins: { time: { rules: { 'one-time': oneTime } } },
      rules: { 'time/one-time': ['error', { baseline: baselines.time ?? {}, root, app: appName, ...time }] },
    },
  ]
}
