// vendored from glassflow/arche-design paper-kit-v2 b0f2a89c9b746ebbc4a4bd6968c128ccd414afb2: do not edit, run npm run paper:sync
// kit-only: app UI is built from the kit. Two things fail:
//
//   style    a `style` attribute, on an element or a component. Space, size and colour come from
//            utilities; a value only known at run time (a chart's computed width, a column count
//            from data) is the one honest use, and it lives in the app's register with its reason.
//   control  a raw <button>, <input>, <select> or <textarea>. The kit has Button, Input, Select,
//            Textarea, Checkbox and Switch.
//
// Two scopes, one count per file:
//
//   edition  the path prefixes in `options.edition`. A file here may carry violations only while it
//            is in `options.register` with a reason. A new edition file, or one not registered,
//            fails on its first violation, whatever the baseline says.
//   legacy   everything else the rule runs on. Frozen: a file may carry at most its count in the
//            baseline.
//
// Baseline: `options.baseline` maps a file (relative to `options.root`) to the violations it may
// still carry. Over that number every violation in the file is reported; under it, the file is
// reported once so the baseline is lowered in the same PR. It only goes down (the app's baseline
// script). A register entry for a file that is now clean is reported too, so neither list
// outlives its reason.
//
// Words for the messages: `doc`, `registerFile`, `baselineFile`, `baselineCommand`, `kitDir`. The
// defaults name no app: an app that wants its own file names passes them.
//
// no-restricted-syntax cannot do this: it has no notion of a per-file allowance.

import path from 'node:path'

const CONTROLS = new Set(['button', 'input', 'select', 'textarea'])
const KIT = {
  button: 'Button',
  input: 'Input (or Checkbox, Switch)',
  select: 'Select',
  textarea: 'Textarea',
}

export default {
  meta: {
    type: 'suggestion',
    docs: { description: 'Build app UI from the kit: no inline style, no raw form control.' },
    schema: [
      {
        type: 'object',
        properties: {
          baseline: { type: 'object' },
          register: { type: 'object' },
          edition: { type: 'array', items: { type: 'string' } },
          root: { type: 'string' },
          doc: { type: 'string' },
          registerFile: { type: 'string' },
          baselineFile: { type: 'string' },
          baselineCommand: { type: 'string' },
          kitDir: { type: 'string' },
        },
        additionalProperties: false,
      },
    ],
    messages: {
      style:
        'An inline style. Use a utility class; a value only known at run time goes in the kit-only register ({{registerFile}}) with its reason. {{doc}} (kit-only{{why}})',
      control:
        'A raw <{{tag}}>. Use the kit {{kit}} from {{kitDir}}. {{doc}} (kit-only{{why}})',
      stale:
        'This file now has {{count}} kit-only violations and {{baselineFile}} allows {{allowed}}. Lower it in the same PR: {{baselineCommand}}. (kit-only)',
      cleanRegister:
        'This file has no kit-only violations left. Take it out of the kit-only register ({{registerFile}}). (kit-only)',
    },
  },
  create(context) {
    const opts = context.options[0] ?? {}
    const root = opts.root ?? process.cwd()
    const file = path.relative(root, context.filename).split(path.sep).join('/')
    const edition = (opts.edition ?? []).some((p) => file.startsWith(p))
    const registered = Object.hasOwn(opts.register ?? {}, file)
    const allowed = opts.baseline?.[file] ?? 0
    const words = {
      doc: opts.doc ?? 'See .impeccable.md.',
      registerFile: opts.registerFile ?? "the app's register file",
      baselineFile: opts.baselineFile ?? "the app's kit baseline file",
      baselineCommand: opts.baselineCommand ?? "run the app's kit baseline script",
      kitDir: opts.kitDir ?? 'paper-kit',
    }
    const hits = []

    return {
      JSXAttribute(node) {
        if (node.name.type === 'JSXIdentifier' && node.name.name === 'style') hits.push({ node, messageId: 'style', data: { ...words } })
      },
      JSXOpeningElement(node) {
        if (node.name.type === 'JSXIdentifier' && CONTROLS.has(node.name.name))
          hits.push({ node: node.name, messageId: 'control', data: { ...words, tag: node.name.name, kit: KIT[node.name.name] } })
      },
      'Program:exit'(program) {
        const n = hits.length
        if (edition && registered && n === 0) {
          context.report({ node: program, loc: { line: 1, column: 0 }, messageId: 'cleanRegister', data: words })
          return
        }
        // An unregistered edition file has no allowance, whatever the baseline holds.
        const cap = edition && !registered ? 0 : allowed
        if (n > cap) {
          const why = edition && !registered ? ': edition code' : `: ${n} in this file, the baseline allows ${cap}`
          for (const h of hits) context.report({ node: h.node, messageId: h.messageId, data: { ...words, ...h.data, why } })
        } else if (n < allowed) {
          context.report({ node: program, loc: { line: 1, column: 0 }, messageId: 'stale', data: { ...words, count: String(n), allowed: String(allowed) } })
        }
      },
    }
  },
}
