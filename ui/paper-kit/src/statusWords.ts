/* vendored from glassflow/arche-design paper-kit-v2 b0f2a89c9b746ebbc4a4bd6968c128ccd414afb2: do not edit, run npm run paper:sync */
import type { Tone } from "./StatusLabel";

// One table from a shared status key to the word a person reads and the tone it reads in. Two apps say
// a state the same way by looking it up here, so a station that works reads "Working" in amber in both.
//
// The keys are plain strings, so this file imports no app type. Each app maps its own API unions onto
// them (a TypeScript `satisfies Record<ApiUnion, StatusKey>` makes a state added later fail the build
// until it is mapped) and renders the entry with StatusLabel or StatusDot.
//
// The words are the console's (its status words list: plain words, no "run", no em dash). The tones
// are StatusLabel's six: working is live work and the only amber; waiting is a person's turn or a
// pause; ready is jade; done is plain; bad is rose; quiet is not started, stale or out of play.
// A key the console has no word for yet (starting, stopping, stopped, failed, waiting, ready) takes
// the plain word and the tone its meaning gives.
export const STATUS = {
  // a crew or a station
  running: { word: "Working", tone: "working" },
  starting: { word: "Starting", tone: "working" },
  stopping: { word: "Stopping", tone: "working" },
  stopped: { word: "Stopped", tone: "quiet" },
  failed: { word: "Failed", tone: "bad" },
  waiting_for_you: { word: "Waiting for you", tone: "waiting" },
  // a project or a ticket
  building: { word: "Building", tone: "working" },
  waiting: { word: "Waiting", tone: "waiting" },
  ready: { word: "Ready", tone: "ready" },
  done: { word: "Done", tone: "done" },
  blocked: { word: "Blocked", tone: "bad" },
  not_started: { word: "Not started", tone: "quiet" },
} as const satisfies Record<string, { word: string; tone: Tone }>;

export type StatusKey = keyof typeof STATUS;
