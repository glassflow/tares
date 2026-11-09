// Whether the console may say "licence" at all, and the one sentence for a refused team action.
// A leaf module (it imports nothing), so api.ts, cloud.ts and the components can all use it
// without an import cycle.
//
// Tares Cloud needs no licence from the person, so on a hosted cell (/health has
// workspace_api_url) the console never says licence, key or enterprise, and links nowhere. The
// words are allowed only on a cell known to be self-hosted: before /health has answered, and when
// it fails, the cell is not known to be self-hosted and says the plain sentence.

/** Where the enterprise licence is explained. The daemon sends the same address in its 402 answer. */
export const ENTERPRISE_URL = "https://docs.glassflow.ai/tares/enterprise";

// pending: /health not answered yet. self: answered, no workspace_api_url. plain: a Tares Cloud
// cell, or /health failed (not known to be self-hosted)
let known: "pending" | "self" | "plain" = "pending";

/** cloud.ts calls this when /health answers (`h`) or fails (no `h`: the cell is not known to be
 *  self-hosted, so it stays plain; a later answer settles it). */
export function noteHealth(h?: { workspace_api_url?: unknown }): void {
  known = !h ? "plain" : h.workspace_api_url ? "plain" : "self";
}

/** True only once /health has answered and the cell is not a Tares Cloud cell: the only time the
 *  console may say licence, key or enterprise, or link to ENTERPRISE_URL. Usable outside React; a
 *  component that shows its words calls `useCloud()` too, so it renders again when /health lands. */
export function licenceWordsOn(): boolean {
  return known === "self";
}

/** False until /health has answered or failed. A line that would say one sentence on a Tares Cloud
 *  cell and another on a self-hosted one shows nothing until then, so the wrong one never flashes. */
export function licenceKnown(): boolean {
  return known !== "pending";
}

/** The one plain sentence for a team action the cell will not take: "Adding people needs an
 *  enterprise licence." On a Tares Cloud cell, before /health has answered, or when it fails, it names no licence:
 *  "Adding people is not available on this workspace right now." */
export function licenceSentence(what = "That"): string {
  return licenceWordsOn()
    ? `${what} needs an enterprise licence.`
    : `${what} is not available on this workspace right now.`;
}
