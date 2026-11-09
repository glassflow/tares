// Whether the console may say "licence" at all, and the one sentence for a refused team action.
// A leaf module (it imports nothing), so api.ts, cloud.ts and the components can all use it
// without an import cycle.
//
// Tares Cloud needs no licence from the person, so on a hosted cell (/health has
// workspace_api_url) the console never says licence, key or enterprise, and links nowhere. A cell
// that is not yet known to be self-hosted counts as hosted until /health has answered.

/** Where the enterprise licence is explained. The daemon sends the same address in its 402 answer. */
export const ENTERPRISE_URL = "https://docs.glassflow.ai/tares/enterprise";

let settled = false;
let hosted = false;

/** cloud.ts calls this when /health answers (`h`) or fails (no `h`: treated as self-hosted). */
export function noteHealth(h?: { workspace_api_url?: unknown }): void {
  settled = true;
  hosted = !!h?.workspace_api_url;
}

/** True only once /health has answered (or failed) and the cell is not a Tares Cloud cell: the only
 *  time the console may say licence, key or enterprise, or link to ENTERPRISE_URL. Usable outside
 *  React; a component that shows its words calls `useCloud()` too, so it renders again when /health lands. */
export function licenceWordsOn(): boolean {
  return settled && !hosted;
}

/** The one plain sentence for a team action the cell will not take: "Adding people needs an
 *  enterprise licence." On a Tares Cloud cell (or before /health has answered) it names no licence:
 *  "Adding people is not available on this workspace right now." */
export function licenceSentence(what = "That"): string {
  return licenceWordsOn()
    ? `${what} needs an enterprise licence.`
    : `${what} is not available on this workspace right now.`;
}
