# paper-kit

The code half of the paper direction, shared by every app that wears it: the **ESLint contracts** in
`eslint/` and the **components** in `src/` (Button, Dialog, Tabs, StatusLabel, the field controls and the
shared status words).

An app does not import this folder from here. It vendors a tagged copy (`paper-kit-vN`) and never
edits it. A change is made in this repo, tagged, and synced into the app.

## The components

`src/` is the console's own code, moved as is: the components on top, the shadcn-style primitives
they wrap under `ui/`, and `cn` in `lib/`. Import from the index, never from a file inside.

| Export | What it is |
|---|---|
| `Button` | `variant` `primary` / `secondary` / `quiet`, `size` `md` / `lg`, `busy`, `asChild` (a link that is inert gets `aria-disabled` and no pointer events). Types `ButtonProps`, `ButtonVariant`, `ButtonSize`. |
| `Dialog` | A titled, described modal: `trigger`, `title`, `description` (both required), `children`, `footer`, controlled with `open` and `onOpenChange`. Type `DialogProps`. |
| `Tabs` | Tabs from an `items` array (`value`, `label`, `content`, `disabled`), with the sliding underline. Types `TabsProps`, `TabItem`. |
| `StatusLabel`, `StatusDot` | A status as a mono word with a 1px underline, and its dot. A page passes a `tone` (`working`, `waiting`, `ready`, `done`, `bad`, `quiet`); the type is `Tone`. A tone that changes while the reader watches washes once. |
| `Input`, `Textarea` | A labelled text control: `label` (required), `hint`, `error`, then every native prop; a ref and `data-testid` reach the control itself. Types `InputProps`, `TextareaProps`. |
| `Select` | A labelled select from an `options` array (`value`, `label`), with `value` / `defaultValue` / `onValueChange`, `placeholder`, `name`, `required`, `disabled`; aria props and `data-testid` land on the trigger. Types `SelectProps`, `SelectOption`. |
| `Checkbox`, `Switch` | A control with its label, hint and error beside it, over Radix's checkbox and switch. Types `CheckboxProps`, `SwitchProps`. |
| `useFieldIds`, `fieldAria`, `FieldNote`, `StackedField`, `InlineField`, `ERROR_BORDER` | What the five controls share, for an app's own control that should read the same: the ids, the aria attributes (described by the hint and the error, invalid on error), the note and the two layouts (label above, or control then label). Type `FieldProps`. |
| `STATUS`, `StatusKey` | The shared status words: for each key (`running`, `starting`, `stopping`, `stopped`, `failed`, `waiting_for_you`, `building`, `waiting`, `ready`, `done`, `blocked`, `not_started`) the `word` a person reads and the `tone` it takes. See "The status words" below. |
| `cn` | `twMerge(clsx(...))`, for the app's own components beside these. |

Two things differ from the console's files beyond import lines. The marks the primitives draw (the
dialog's close, the select's chevron, the checkmark) are the same 24x24 stroke paths in `ui/icons.tsx`,
where the console imports them from its icon set, so the kit has no icon dependency. And a few comments
that spoke of the console's own stylesheet are reworded, because they named rules the kit does not
have (`lib/cn.ts` also drops the console's comment about generated shadcn files). No class string or
behaviour changed. `SearchSelect` stays in the console: it reads the console's data.

### The status words

`STATUS[key]` is `{ word, tone }`, and `tone` is a `Tone`, so an entry goes straight to the label:

```tsx
<StatusLabel tone={STATUS[key].tone}>{STATUS[key].word}</StatusLabel>
```

The keys are plain strings, so the table imports no app type. Each app maps the states of its API onto
them whose word and tone are the table's, and lets the compiler check the map:

```ts
type TicketTurn = "building" | "waiting" | "done" | "idle"; // the app's API union
const KEY = { building: "building", waiting: "waiting", done: "done" } satisfies Record<
  Exclude<TicketTurn, "idle">,
  StatusKey
>;
```

A state added to the API later then fails the build until it is mapped or left out on purpose. A state
whose word or tone differs from the table (here `idle`, which reads Idle) keeps its own word and tone in
the app, beside this map. The words are the console's, in sentence case (the label draws them in
capitals); a key the console had no word for takes the plain word its meaning gives, and a tone from
the six `StatusLabel` explains.

### Peer dependencies

This repo has no `package.json`, so nothing is installed from here. The app installs these itself, at
the exact versions it already runs (the console's resolved versions today):

| Package | Version |
|---|---|
| `react`, `react-dom` | 18.3.1 |
| `@radix-ui/react-dialog` | 1.3.1 |
| `@radix-ui/react-tabs` | 1.2.1 |
| `@radix-ui/react-select` | 2.4.1 |
| `@radix-ui/react-checkbox` | 1.4.1 |
| `@radix-ui/react-switch` | 1.4.1 |
| `@radix-ui/react-slot` | 1.5.0 |
| `class-variance-authority` | 0.7.1 |
| `clsx` | 2.1.1 |
| `tailwind-merge` | 3.7.0 |

Tailwind CSS 4 generates the classes (see below). `@types/react` 18 is needed for the types.

### Consumer setup

1. **Vendor `src/`** from a tagged copy into the app (for example `src/paper-kit/`).
2. **A path alias** `@paper-kit` to the vendored `src` (`tsconfig` `paths` and the bundler's alias), and
   import from `@paper-kit`.
3. **Tailwind `@source`** pointing at the vendored `src`, in the app's main stylesheet, so the classes
   the components carry are generated: `@source "./paper-kit/src";` (path relative to that stylesheet).
4. **The paper CSS**, imported after Tailwind: `paper-theme.css` (the role layer: `bg-brand`, `text-ok`,
   `bg-primary-hover` and the other utilities the classes name) and `paper-motion.css` (the `motion-*`
   classes: `motion-dialog`, `motion-scrim`, `motion-tabs`, `motion-wash`). Without the second the
   components work but do not move.
5. **Strict types**: the code type-checks under `strict`, `noUncheckedIndexedAccess`, `target` ES2023,
   `jsx` `react-jsx` and `moduleResolution` `bundler`. An app that lints may ignore the vendored folder
   (it is checked here) or run the contracts below over it, with `src/ui/**` out of the `kit-only`
   rule: the primitives there are the raw `<input>` and `<textarea>` the components wrap, as the
   console's own `components/ui` is.
6. **Bare element rules go in a layer.** An app whose own stylesheet has unlayered `button { ... }`
   or `input { ... }` rules would beat the primitives' utilities; put those rules in a cascade layer.
7. **The app supplies the focus ring.** Button, the tab triggers, Checkbox, Switch, the select trigger
   and the dialog close carry no `focus-visible` utility, and the text controls only turn their border
   to `ring`. Give the app one global `:focus-visible` rule (an outline in the brand colour) so every
   control shows keyboard focus the same way. In dark mode the text controls also fill with
   `dark:focus:bg-field-focus` when focused, so the theme needs the `field-focus` colour role
   (`--color-field-focus` in the vendored `paper-theme.css`).

## The ESLint contracts

Four rules and two factories. The factories return flat-config arrays, so an app's config is a call:
what differs between apps (paths, registers, baselines) goes in as options, and the rules stay the
same. Inline config is off in both, so an `eslint-disable` comment exempts nothing.

| File | What it is |
|---|---|
| `eslint/design-contract.mjs` | `designContract(options)`: radius, shadow, stripe, em dash, motion, plus the `kit-only` and `one-time` rules. Also exports `kitOnlyConfig(options, extra)` for the app's lint. |
| `eslint/token-contract.mjs` | `tokenContract(options)`: no colour literal, no stock palette colour, no arbitrary value for a token that has a utility. Also exports `themedTokens(css, extra)`. |
| `eslint/rules/kit-only.mjs` | no inline `style`, no raw `<button>`, `<input>`, `<select>`, `<textarea>`, with a per-file register and baseline. |
| `eslint/rules/one-time.mjs` | no `toLocale*String` call and no `Intl.DateTimeFormat`, `RelativeTimeFormat` or `NumberFormat` outside the app's time and number modules, with a per-file baseline for legacy files. |

The app provides `eslint` and `typescript-eslint` (the factories import only `typescript-eslint`
besides `node:` built-ins). Nothing here reads a JSON file or an app path for you: the app reads its
own baselines and passes them in.

### What each check says

- **radius** `rounded-xs` to `rounded-4xl` (any side) and `rounded-[..]` fail; `rounded-full` stays.
- **shadow** stock `shadow-xs` to `shadow-2xl`, any `shadow-[..]` that is not an inset bar or
  `var(--sheet-shadow)`, and `boxShadow` in a style object fail.
- **stripe** a left or inline-start border wider than 1px fails, as a class or in a style object.
- **voice** an em dash in a string or JSX text fails, except a lone `—` as the no-value mark.
- **motion** `duration-150`, `delay-..`, `ease-[..]`, `animate-[..]` and a raw time in a style
  object's `transition` or `animation` fail. Name a token (`duration-(--dur-2)`) or a `motion-*` class.
- **kit-only** see above. Edition files may carry a violation only while the file is in the register
  with a reason; legacy files carry at most their count in the baseline. A register entry for a clean
  file is reported, and so is a baseline that is higher than the count, so both only go down.
- **one-time** time is said through the time module. Strict files have no allowance; legacy files
  carry at most their baseline count.
- **token-contract** hex (`#fff`, `#e3c777`), `rgb()` / `rgba()` with a number, a stock Tailwind
  palette utility (`bg-amber-200`), and `text-[color:var(--err)]` or the Tailwind 4 shorthand
  `text-(--err)` where `text-err` exists. The names
  that have a utility are read from `@theme inline` in your theme CSS, with `/* */` comments
  stripped first (a `}` in a comment inside the block used to end the match and lose the names after it).

### `designContract(options)`

All options are optional. Paths are relative to `root`, with forward slashes.

| Option | Default | Meaning |
|---|---|---|
| `files` | `['src/**/*.{ts,tsx}']` | globs every rule runs on: the selectors, `kit-only` and `one-time` |
| `ignores` | `[]` | globs left out of those rules |
| `root` | `process.cwd()` | the app root, absolute |
| `registers.shadow`, `registers.stripe` | `[]` | files exempt from that selector |
| `registers.kit` | `{}` | `{ "<file>": "<reason>" }`, the kit-only register |
| `registers.kitEdition` | `[]` | path prefixes that make a file edition code for kit-only |
| `baselines.time`, `baselines.kit` | `{}` | `{ "<file>": count }`, parsed from the app's JSON |
| `paperProfile` | `true` | `false` drops the radius rule |
| `doc` | `'See .impeccable.md.'` | closes each selector message and `kit-only`'s |
| `appName` | `'app'` | word in the one-time messages |
| `kitOnly` | `files` and `ignores` as above | narrows `kit-only` with its own `files` and `ignores` (they replace, not add), and sets its message words (`doc`, `registerFile`, `baselineFile`, `baselineCommand`, `kitDir`; the defaults name no app) |
| `time` | the paper convention | `modules` (`['src/lib/time.ts', 'src/lib/number.ts']`), `legacy`, `strict` path prefixes and message words |

### `tokenContract(options)`

| Option | Default | Meaning |
|---|---|---|
| `files`, `ignores`, `root` | as above | |
| `themeCss` | `['src/paper/paper-theme.css']` | CSS files to read `@theme inline` from; each must exist |
| `register` | `[]` | `[{ files, drop: ['hex' \| 'rgb' \| 'palette' \| 'utility'] }]`, each entry with its reason in a comment |
| `extraTokens` | `[]` | tokens that have a utility but are not in `@theme inline` |
| `doc`, `appName` | | message words |

It throws if it reads fewer than 20 names, so a moved or emptied theme file cannot turn the gate off.

### An app's config

```js
// eslint-design-contract.config.mjs
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { designContract } from './paper-kit/eslint/design-contract.mjs'
import { EDITION, KIT_REGISTER } from './eslint-rules/kit-register.mjs' // the app's own register

const json = (f) => JSON.parse(readFileSync(new URL(f, import.meta.url), 'utf8'))

export default designContract({
  root: fileURLToPath(new URL('.', import.meta.url)),
  appName: 'console',
  registers: { stripe: ['src/components/ProjectActivity.tsx'], kit: KIT_REGISTER, kitEdition: EDITION },
  baselines: { time: json('./time-contract.baseline.json'), kit: json('./kit-contract.baseline.json') },
  kitOnly: { ignores: ['src/components/ui/**', 'src/pages/dev/**'] },
})
```

```js
// eslint-token-contract.config.mjs
import { fileURLToPath } from 'node:url'
import { tokenContract } from './paper-kit/eslint/token-contract.mjs'

export default tokenContract({
  root: fileURLToPath(new URL('.', import.meta.url)),
  themeCss: ['src/paper/paper-theme.css'],
  register: [{ files: ['src/samples.ts'], drop: ['hex'] }], // "#595" is a pull request number here
})
```

Run them as `eslint --quiet --config <file> "src/**/*.{ts,tsx}"`. The app's own `eslint.config.mjs` can
add the same kit-only rule for feedback while writing: put `kitOnlyConfig({ register, edition, baseline, root, ignores })` (one config object) in its array.

### Registers and baselines live in the app

The factories take registers and baselines as arguments; the app owns them. A register entry carries
a reason and the ticket that empties it. A baseline file only goes down: the app's baseline script
lowers it when a file improves, and raising one needs a reason in the PR. **Exceptions go in a
register with a reason, never in an `eslint-disable` comment** (inline config is off, so one would
not work in the gate anyway).

### Checking a change

There is no CI here. For `src/`, copy it into a console worktree's `ui/paper-kit/src`, add a scratch
`tsconfig` with the flags above and `include: ["paper-kit/src"]`, and run `tsc --noEmit`; then run the
design and token contracts over `paper-kit/src` with empty registers (and `ui/**` in `kitOnly.ignores`):
both must print nothing.

For `eslint/`: Copy `packages/paper-kit` into a consumer's vendored place, point its two
contract configs at the factories, run its design and token contracts on the tree with and without
the change, and compare the `ruleId`, file and line of every message.
