// one-time: the console says time one way, through src/lib/time.ts (TR-1261). What fails:
//
//   locale   a toLocaleString, toLocaleDateString or toLocaleTimeString call. A date comes out in
//            the viewer's zone and language, so two people read one incident at two times. Use
//            formatStampUTC, sinceWords or ageWords from lib/time.ts; a grouped number is
//            formatCount from lib/number.ts (a toLocaleString on a number reads the same to a
//            parser as one on a date, so both go through a module).
//   intl     Intl.DateTimeFormat, Intl.RelativeTimeFormat or Intl.NumberFormat, built or called.
//
// The two modules are the only files allowed to do either. Two scopes, as kit-only has them:
//
//   edition  src/editions, src/components/edition, src/components/ui, src/ee, src/pages/dev:
//            no allowance, whatever the baseline holds.
//   legacy   the platform pages and the shared components outside the kit, frozen by TR-1250: a
//            file may carry at most its count in time-contract.baseline.json. Under that count the
//            file is reported once, so the baseline goes down in the same PR
//            (npm run time-baseline). It never goes up without --raise and a reason.
//
// It runs in `npm run design-contract`, where inline config is off.

import path from 'node:path'

const LOCALE = new Set(['toLocaleString', 'toLocaleDateString', 'toLocaleTimeString'])
const INTL = new Set(['DateTimeFormat', 'RelativeTimeFormat', 'NumberFormat'])
const MODULES = new Set(['src/lib/time.ts', 'src/lib/number.ts'])
const LEGACY = ['src/pages/', 'src/components/']
const STRICT = ['src/pages/dev/', 'src/components/edition/', 'src/components/ui/']

const name = (n) => (n.type === 'Identifier' ? n.name : n.type === 'Literal' ? String(n.value) : null)

export default {
  meta: {
    type: 'problem',
    docs: { description: 'Say time and grouped numbers through src/lib/time.ts and src/lib/number.ts.' },
    schema: [
      { type: 'object', properties: { baseline: { type: 'object' }, root: { type: 'string' } }, additionalProperties: false },
    ],
    messages: {
      locale:
        'A {{call}} call: the viewer\'s locale decides what it says. A time goes through src/lib/time.ts (formatStampUTC, sinceWords, ageWords), a grouped number through formatCount in src/lib/number.ts. See the Time line in .impeccable.md. (one-time{{why}})',
      intl:
        'Intl.{{call}}: the console says time one way. Use src/lib/time.ts, or formatCount in src/lib/number.ts. See the Time line in .impeccable.md. (one-time{{why}})',
      stale:
        'This file now has {{count}} one-time violations and time-contract.baseline.json allows {{allowed}}. Lower it in the same PR: npm run time-baseline. (one-time)',
    },
  },
  create(context) {
    const opts = context.options[0] ?? {}
    const file = path.relative(opts.root ?? process.cwd(), context.filename).split(path.sep).join('/')
    if (MODULES.has(file)) return {}
    const legacy = LEGACY.some((p) => file.startsWith(p)) && !STRICT.some((p) => file.startsWith(p))
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
          for (const h of hits) context.report({ node: h.node, messageId: h.messageId, data: { call: h.call, why } })
        } else if (n < allowed) {
          context.report({ node: program, loc: { line: 1, column: 0 }, messageId: 'stale', data: { count: String(n), allowed: String(allowed) } })
        }
      },
    }
  },
}
