// vendored from glassflow/arche-design paper-kit-v1 91909425f0335427fe60ca6d879ca56ca2fcce52: do not edit, run npm run paper:sync
// one-time: an app says time one way, through one module (paper direction, "one time grammar"). What fails:
//
//   locale   a toLocaleString, toLocaleDateString or toLocaleTimeString call. A date comes out in
//            the viewer's zone and language, so two people read one incident at two times. Use
//            formatStampUTC, sinceWords or ageWords from the time module; a grouped number is
//            formatCount from the number module (a toLocaleString on a number reads the same to a
//            parser as one on a date, so both go through a module).
//   intl     Intl.DateTimeFormat, Intl.RelativeTimeFormat or Intl.NumberFormat, built or called.
//
// The two modules are the only files allowed to do either. Two scopes, as kit-only has them:
//
//   strict   everything the app builds on the kit (options.strict, and any file outside
//            options.legacy): no allowance, whatever the baseline holds.
//   legacy   files under options.legacy and not under options.strict, frozen: a file may carry at
//            most its count in options.baseline. Under that count the file is reported once, so
//            the baseline goes down in the same PR (the app's baseline script). It never goes up
//            without a reason.
//
// Options (all paths relative to options.root, with forward slashes):
//   modules          [time module, number module]; the only files allowed to call these. Default
//                    src/lib/time.ts, src/lib/number.ts.
//   legacy, strict   path prefixes. Default src/pages/, src/components/ and
//                    src/pages/dev/, src/components/edition/, src/components/ui/.
//   baseline         { "<file>": count } for the legacy files. The app reads its own JSON.
//   root             the app root, absolute. Default process.cwd().
//   app, doc, baselineFile, baselineCommand   words for the messages.
//
// It runs in the app's design contract, where inline config is off.

import path from 'node:path'

const LOCALE = new Set(['toLocaleString', 'toLocaleDateString', 'toLocaleTimeString'])
const INTL = new Set(['DateTimeFormat', 'RelativeTimeFormat', 'NumberFormat'])
const MODULES = ['src/lib/time.ts', 'src/lib/number.ts']
const LEGACY = ['src/pages/', 'src/components/']
const STRICT = ['src/pages/dev/', 'src/components/edition/', 'src/components/ui/']

const name = (n) => (n.type === 'Identifier' ? n.name : n.type === 'Literal' ? String(n.value) : null)

export default {
  meta: {
    type: 'problem',
    docs: { description: 'Say time and grouped numbers through the time and number modules.' },
    schema: [
      {
        type: 'object',
        properties: {
          baseline: { type: 'object' },
          root: { type: 'string' },
          modules: { type: 'array', items: { type: 'string' } },
          legacy: { type: 'array', items: { type: 'string' } },
          strict: { type: 'array', items: { type: 'string' } },
          app: { type: 'string' },
          doc: { type: 'string' },
          baselineFile: { type: 'string' },
          baselineCommand: { type: 'string' },
        },
        additionalProperties: false,
      },
    ],
    messages: {
      locale:
        'A {{call}} call: the viewer\'s locale decides what it says. A time goes through {{time}} (formatStampUTC, sinceWords, ageWords), a grouped number through formatCount in {{number}}. {{doc}} (one-time{{why}})',
      intl:
        'Intl.{{call}}: the {{app}} says time one way. Use {{time}}, or formatCount in {{number}}. {{doc}} (one-time{{why}})',
      stale:
        'This file now has {{count}} one-time violations and {{baselineFile}} allows {{allowed}}. Lower it in the same PR: {{baselineCommand}}. (one-time)',
    },
  },
  create(context) {
    const opts = context.options[0] ?? {}
    const file = path.relative(opts.root ?? process.cwd(), context.filename).split(path.sep).join('/')
    const modules = opts.modules ?? MODULES
    if (modules.includes(file)) return {}
    const legacyScope = opts.legacy ?? LEGACY
    const strictScope = opts.strict ?? STRICT
    const legacy = legacyScope.some((p) => file.startsWith(p)) && !strictScope.some((p) => file.startsWith(p))
    const words = {
      time: modules[0] ?? MODULES[0],
      number: modules[1] ?? MODULES[1],
      app: opts.app ?? 'app',
      doc: opts.doc ?? 'See the Time line in .impeccable.md.',
      baselineFile: opts.baselineFile ?? 'time-contract.baseline.json',
      baselineCommand: opts.baselineCommand ?? 'npm run time-baseline',
    }
    const allowed = legacy ? (opts.baseline?.[file] ?? 0) : 0
    const hits = []
    const intl = (node) => {
      const c = node.callee
      if (c.type === 'MemberExpression' && c.object.type === 'Identifier' && c.object.name === 'Intl' && INTL.has(name(c.property)))
        hits.push({ node, messageId: 'intl', call: name(c.property) })
    }

    return {
      CallExpression(node) {
        const c = node.callee
        if (c.type === 'MemberExpression' && LOCALE.has(name(c.property))) hits.push({ node: c.property, messageId: 'locale', call: name(c.property) })
        else intl(node)
      },
      NewExpression: intl,
      'Program:exit'(program) {
        const n = hits.length
        if (n > allowed) {
          const why = legacy ? `: ${n} in this file, the baseline allows ${allowed}` : ': edition code'
          for (const h of hits) context.report({ node: h.node, messageId: h.messageId, data: { ...words, call: h.call, why } })
        } else if (n < allowed) {
          context.report({ node: program, loc: { line: 1, column: 0 }, messageId: 'stale', data: { ...words, count: String(n), allowed: String(allowed) } })
        }
      },
    }
  },
}
