import { SERVICE_TONE } from "./tones";
import { formatCount } from "../../lib/number";

/** The tone of a service's state, from the one status table. The words say the state; the tone only repeats it. */
export const STATE_TONE = SERVICE_TONE;

export const up = (s: string) => (s ? s[0].toUpperCase() + s.slice(1) : s);

/** "1 incident", "3 incidents". */
export const count = (n: number, one: string, many = `${one}s`) => `${formatCount(n)} ${n === 1 ? one : many}`;

export const serviceHref = (name: string) => `/services/${encodeURIComponent(name)}`;
