# paper-kit

The code half of the paper direction, shared by every app that wears it. Today that is the **ESLint
contracts** in `eslint/`; primitives and field controls arrive in `src/` later.

An app does not import this folder from here. It vendors a tagged copy (`paper-kit-vN`) and never
edits it. A change is made in this repo, tagged, and synced into the app.

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
  palette utility (`bg-amber-200`), and `text-[color:var(--err)]` where `text-err` exists. The names
  that have a utility are read from `@theme inline` in your theme CSS, with `/* */` comments
  stripped first (a `}` in a comment inside the block used to end the match and lose the names after it).

### `designContract(options)`

All options are optional. Paths are relative to `root`, with forward slashes.

| Option | Default | Meaning |
|---|---|---|
| `files` | `['src/**/*.{ts,tsx}']` | globs the selector rules and `one-time` run on |
| `ignores` | `[]` | globs left out of the selector rules |
| `root` | `process.cwd()` | the app root, absolute |
| `registers.shadow`, `registers.stripe` | `[]` | files exempt from that selector |
| `registers.kit` | `{}` | `{ "<file>": "<reason>" }`, the kit-only register |
| `registers.kitEdition` | `[]` | path prefixes that make a file edition code for kit-only |
| `baselines.time`, `baselines.kit` | `{}` | `{ "<file>": count }`, parsed from the app's JSON |
| `paperProfile` | `true` | `false` drops the radius rule |
| `doc` | points at `.impeccable.md` and the Console design section of AGENTS.md | closes each selector message |
| `appName` | `'app'` | word in the one-time messages |
| `kitOnly` | `{ files: ['src/**/*.tsx'] }` | `files`, `ignores` and message words (`doc`, `registerFile`, `baselineFile`, `baselineCommand`, `kitDir`) |
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

There is no CI here. Copy `packages/paper-kit` into a consumer's vendored place, point its two
contract configs at the factories, run its design and token contracts on the tree with and without
the change, and compare the `ruleId`, file and line of every message.
