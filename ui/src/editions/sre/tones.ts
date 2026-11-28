import type { Tone } from "@paper-kit";
import type { GroupState } from "./Incidents.api";
import type { SreServiceState } from "./Services.api";

// The AI SRE screens' own status tones, out of the kit's table since the console is the factory only
// (TR-1817). These screens are no longer routed; they go with the rest of the SRE code.

// An incident, or a finding that was raised and did not become one.
export const INCIDENT_TONE = {
  open: "bad",
  on_it: "working",
  resolved: "ready",
  raised: "quiet",
} satisfies Record<GroupState, Tone>;

export const SERVICE_TONE = {
  incident: "bad",
  unusual: "waiting",
  silent: "waiting",
  not_watched: "quiet",
  no_own_signal: "quiet",
  normal: "ready",
} satisfies Record<SreServiceState, Tone>;
