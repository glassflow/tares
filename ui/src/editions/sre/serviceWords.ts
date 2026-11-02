import type { Tone } from "../../components/edition";
import type { SreServiceState } from "./Services.api";

/** The colour of a service's state. The words say the state; the colour only repeats it. */
export const STATE_TONE: Record<SreServiceState, Tone> = {
  incident: "bad",
  unusual: "waiting",
  silent: "waiting",
  not_watched: "quiet",
  no_own_signal: "quiet",
  normal: "ready",
};

export const up = (s: string) => (s ? s[0].toUpperCase() + s.slice(1) : s);

/** "1 incident", "3 incidents". */
export const count = (n: number, one: string, many = `${one}s`) => `${n.toLocaleString("en")} ${n === 1 ? one : many}`;

export const serviceHref = (name: string) => `/services/${encodeURIComponent(name)}`;
