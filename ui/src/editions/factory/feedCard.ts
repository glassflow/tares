import type { HomeAction, HomeItem } from "./home.types";
import type { FeedCard, FeedExtra } from "./feed.types";

// A home item as a feed card. Yes and No are the item's own actions, never invented ones: where the
// cell has no one-tap action yet (merging a PR, accepting a milestone), Yes is the link to the page
// that has it, under the label the cell gives it.

const NO_VERBS = ["later", "decline"];

/** The desk label a question or permission answers to. A question with options sends it; one without
 *  has it on its Answer action (a team cell's `desk` item) or only in its id (`question:<label>`,
 *  factory_waiting.desk_items). */
function deskOf(item: HomeItem): string | null {
  for (const a of item.actions) if ((a.do === "allow" || a.do === "answer") && typeof a.desk === "string") return a.desk;
  if (item.question?.desk) return item.question.desk;
  const m = /^question:(.+)$/.exec(item.id);
  return m ? m[1] : null;
}

/** What a desk item asks, by its shape: a team cell sends every desk item as kind `desk`
 *  (tares_ee/team_home.desk_items), the single-person home as `question` or `permission`. */
function deskKind(item: HomeItem): "question" | "permission" | null {
  if (item.kind === "question" || item.kind === "permission") return item.kind;
  if (item.kind !== "desk" || item.actions.some((a) => a.do === "keep_batch")) return null;
  return item.actions.some((a) => a.do === "allow") ? "permission" : "question";
}

/** The option a recommendation names: the same words, or words that start with the option. */
function recommended(options: string[], rec: string | null): number {
  if (!rec) return -1;
  const r = rec.trim().toLowerCase();
  return options.findIndex((o) => r === o.toLowerCase() || r.startsWith(o.toLowerCase()));
}

function buttons(item: HomeItem): { yes: HomeAction | null; no: HomeAction | null } {
  const desk = deskOf(item);
  const kind = deskKind(item);
  if (kind === "question" && desk) {
    const options = item.question?.options ?? [];
    const i = recommended(options, item.question?.recommendation ?? null);
    // the panel answers with the option's letter, counted over every option, so all of them go in
    const yes: HomeAction | null = i >= 0
      ? { label: `Yes, ${options[i]}`, primary: false, do: "choose", desk, choice: options[i], letter: String.fromCharCode(65 + i) }
      : null;
    return { yes, no: { label: yes ? "Something else" : "Answer", primary: false, do: "answer", desk, options } };
  }
  if (kind === "permission" && desk) {
    const allow = item.actions.find((a) => a.do === "allow") ?? null;
    return { yes: allow, no: { label: "Answer", primary: false, do: "answer", desk } };
  }
  const acts = item.actions.filter((a) => a.do && a.do !== "answer");
  const yes = acts.find((a) => a.primary && !NO_VERBS.includes(a.do!))
    ?? acts.find((a) => !NO_VERBS.includes(a.do!))
    ?? item.actions.find((a) => a.primary && a.href)
    ?? item.actions.find((a) => a.href)
    ?? null;
  return { yes, no: acts.find((a) => NO_VERBS.includes(a.do!)) ?? null };
}

/** The link one tap deeper: an action's page Yes does not already open, else the project. */
function deeperHref(item: HomeItem, yes: HomeAction | null): string | null {
  const link = item.actions.find((a) => typeof a.href === "string" && a !== yes);
  return link?.href ?? (item.project ? `/projects/${encodeURIComponent(item.project.id)}` : null);
}

export function toFeedCard(item: HomeItem, extra: FeedExtra = {}): FeedCard {
  const recommendation = item.question?.recommendation ?? null;
  const { yes, no } = buttons(item);
  // the cell's own verb-first line (TR-1447); a cell from before it: a question's or a permission's
  // title is the project and what is asked is its detail, any other item's title is the ask
  const askedInDetail = item.kind === "question" || item.kind === "permission";
  const ask = item.ask || (askedInDetail ? item.detail : item.title);
  return {
    id: item.id,
    kind: item.kind,
    label: item.label,
    ask,
    project: item.project,
    from: item.from?.name ?? null,
    waited: item.waited,
    since: item.since,
    // the cell's own (TR-1448); a cell from before it sends it only on a question
    holds_up: item.holds_up !== undefined ? item.holds_up : item.question?.blocks ?? null,
    blocking: item.blocking === true,
    recommendation: recommendation ? { answer: recommendation, why: extra.why ?? item.question?.why ?? null } : null,
    yes,
    no,
    deeper: {
      // a permission's detail says again what its ask says, and so does a question's unless its
      // ask was cut short
      detail: askedInDetail && (item.kind === "permission" || ask === item.detail) ? "" : item.detail,
      lines: item.lines ?? [],
      checks: extra.checks ?? null,
      href: deeperHref(item, yes),
    },
    item,
  };
}

/** Blocking first, then the longest wait (the home's own order, TR-1021). */
export function feedOrder(a: FeedCard, b: FeedCard): number {
  if (a.blocking !== b.blocking) return a.blocking ? -1 : 1;
  return (a.since ?? "").localeCompare(b.since ?? "");
}
