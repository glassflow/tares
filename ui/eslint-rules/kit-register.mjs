// The kit-only gate's edition scope and its exception register (TR-1258). Read by eslint.config.mjs
// and scripts/kit-baseline.mjs.
//
// An edition file may carry kit-only violations only while it is listed here, and never more than
// its count in kit-contract.baseline.json. Each entry names its reason. The register is not an
// escape hatch: a reason that a kit primitive or a page move will remove names that work, and the
// entry goes when it lands (the gate reports an entry for a file that is clean).

import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { kitOnlyConfig as paperKitOnlyConfig } from '../paper-kit/eslint/design-contract.mjs'

export const EDITION = ['src/editions/', 'src/components/edition/', 'src/ee/']

// A size, position or colour only known at run time: a chart's geometry, a bar's share, a board's
// column count, a sender's colour. Utilities cannot hold a value computed from data. Permanent.
const RUNTIME = 'run-time value: computed from data, no utility can hold it'

// Controls the kit has no primitive for yet: a disclosure toggle (aria-expanded), a menu or listbox
// row, a segmented choice, a link-styled action, a stepper, a whole card that is a button. They are
// styled by hand around a raw <button>. Each goes when the kit gains the primitive.
const NO_PRIMITIVE = 'kit has no primitive for this control yet (disclosure, menu row, segmented choice, link action)'

// A native control kept on purpose: the phone-width section, document and service pickers are a
// native <select> so the OS picker opens; a radio pair, since the kit has no radio group.
const NATIVE = 'native control on purpose (phone-width <select> opens the OS picker; radio pair)'

// Panels under ee/ still in the legacy dress (`panel`, `pagehead`, `btnrow`, `field`, table rows),
// shown inside Settings. They move with TR-1267's Settings pieces (TR-1250: move what factory and
// SRE users reach).
const LEGACY_PANEL = 'legacy-dress panel shown in Settings; moves with TR-1267'

export const KIT_REGISTER = {
  'src/components/edition/LineChart.tsx': RUNTIME,
  'src/components/edition/ProgressBar.tsx': RUNTIME,
  'src/components/edition/Skeleton.tsx': RUNTIME,
  'src/ee/home/Sender.tsx': RUNTIME,
  'src/ee/home/Side.tsx': RUNTIME,

  'src/editions/ChooseEdition.tsx': NO_PRIMITIVE,
  'src/editions/Shell.tsx': NO_PRIMITIVE,
  'src/editions/StartChecklist.tsx': NO_PRIMITIVE,
  'src/editions/factory/BrainAsk.tsx': NO_PRIMITIVE,
  'src/editions/factory/CrewConversation.tsx': `${NO_PRIMITIVE}; ${RUNTIME} (plan progress, line-number gutter)`,
  'src/editions/factory/FactoryCrew.tsx': NO_PRIMITIVE,
  'src/editions/factory/FactoryDocs.tsx': `${NO_PRIMITIVE}; ${NATIVE}`,
  'src/editions/factory/FactoryHome.tsx': NATIVE,
  'src/editions/factory/FactoryMergeQueue.tsx': NO_PRIMITIVE,
  'src/editions/factory/FactoryOverview.tsx': NO_PRIMITIVE,
  'src/editions/factory/FactoryPeople.tsx': NO_PRIMITIVE,
  'src/editions/factory/FactoryProject.tsx': NO_PRIMITIVE,
  'src/editions/factory/FactoryProjectDocs.tsx': NO_PRIMITIVE,
  'src/editions/factory/FactoryReviews.tsx': NO_PRIMITIVE,
  'src/editions/factory/FactorySettings.tsx': `${NO_PRIMITIVE}; ${NATIVE}`,
  'src/editions/factory/FactorySetup.tsx': `${NO_PRIMITIVE}; ${RUNTIME} (the steps-set bar)`,
  'src/editions/factory/FactoryTicket.tsx': NO_PRIMITIVE,
  'src/editions/factory/FactoryTickets.tsx': `${NO_PRIMITIVE}; ${RUNTIME} (board columns from the lanes shown)`,
  'src/editions/factory/ProjectIcon.tsx': NO_PRIMITIVE,
  'src/editions/factory/ProjectShell.tsx': NO_PRIMITIVE,
  'src/editions/factory/SetupParts.tsx': `${NO_PRIMITIVE}; ${NATIVE}`,
  'src/editions/sre/Incidents.tsx': NO_PRIMITIVE,
  'src/editions/sre/Runbooks.tsx': `${NO_PRIMITIVE}; ${NATIVE}`,
  'src/editions/sre/Settings.tsx': NO_PRIMITIVE,
  'src/editions/sre/Timeline.tsx': NO_PRIMITIVE,

  'src/ee/people/SettingsPeople.tsx': LEGACY_PANEL,
  'src/ee/project/access.tsx': LEGACY_PANEL,
  'src/ee/project/grants.tsx': LEGACY_PANEL,
  'src/ee/project/signoffs.tsx': LEGACY_PANEL,
  'src/ee/settings/AreasPanel.tsx': LEGACY_PANEL,
}

// The flat-config block `npm run lint` uses. The rule is the shared paper kit's (ui/paper-kit/, vendored),
// and `npm run design-contract` runs it again from its own config with inline config off, so an
// `eslint-disable` comment cannot switch it off in CI. The kit's own primitives (components/ui) and
// the dev gallery are out of scope.
export function kitOnlyConfig() {
  return paperKitOnlyConfig({
    files: ['src/**/*.tsx'],
    ignores: ['src/components/ui/**', 'src/pages/dev/**'],
    baseline: JSON.parse(readFileSync(new URL('../kit-contract.baseline.json', import.meta.url), 'utf8')),
    register: KIT_REGISTER,
    edition: EDITION,
    root: fileURLToPath(new URL('..', import.meta.url)),
  })
}
