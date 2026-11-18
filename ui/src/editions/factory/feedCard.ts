import type { HomeItem, HomeItemKind } from "./home.types";
import type { FeedCard, FeedExtra, FeedNo, FeedYes } from "./feed.types";

// A home item as a feed card. What each answer does follows the M1 spec's table, "What each answer does
// to the crew": the crew carries on the same way whether the person answered here, on Home or in the
// crew lead's session, so the feed adds no third way to approve anything.

interface Answers { yes: FeedYes | null; no: FeedNo }

const LATER: FeedNo = { label: "Later", words: "none" };

function answersFor(kind: HomeItemKind, item: HomeItem, recommendation: string | null): Answers {
  switch (kind) {
    case "question":
    case "desk": {
      const options = item.question?.options ?? [];
      return {
        yes: recommendation
          ? { label: `Yes, ${recommendation}`, does: "Takes the crew's answer. The one who asked carries on." }
          : null,
        no: { label: recommendation ? "Something else" : "Answer", words: options.length ? "optional" : "required", options },
      };
    }
    case "permission":
      return {
        yes: { label: "Allow once", does: "The crew member asking goes on with this one command." },
        no: { label: "Decline", words: "optional" },
      };
    case "calls_to_confirm":
      return {
        yes: { label: "Keep the calls", does: "The crew keeps building on what it chose." },
        no: { label: "Overturn one", words: "optional", options: item.lines ?? [] },
      };
    case "prs_to_review":
      return {
        yes: { label: "Merge", does: "Merges the pull request. The next ticket starts." },
        no: { label: "Ask for changes", words: "required" },
      };
    case "ready_to_start":
      return {
        yes: { label: "Start", does: "The crew lead hands out the first tickets." },
        no: LATER,
      };
    case "milestone_to_check":
      return {
        yes: { label: "Accept", does: "The milestone closes and the next one starts." },
        no: { label: "Send it back", words: "required" },
      };
    case "signoff":
      return {
        yes: { label: "Close the project", does: "The project moves to Done." },
        no: LATER,
      };
    case "grant":
      return {
        yes: { label: "Agree", does: "The grant is kept for this project." },
        no: { label: "Decline", words: "optional" },
      };
    case "rule":
      return {
        yes: { label: "Keep the rule", does: "The crew follows it from its next turn." },
        no: { label: "Decline", words: "optional" },
      };
  }
}

/** The link one tap deeper leads to, from the item's own actions. */
function deeperHref(item: HomeItem): string | null {
  const link = item.actions.find((a) => typeof a.href === "string");
  return link?.href ?? (item.project ? `/projects/${encodeURIComponent(item.project.id)}` : null);
}

export function toFeedCard(item: HomeItem, extra: FeedExtra = {}): FeedCard {
  const recommendation = item.question?.recommendation ?? null;
  const { yes, no } = answersFor(item.kind, item, recommendation);
  return {
    id: item.id,
    kind: item.kind,
    label: item.label,
    ask: extra.ask ?? item.title,
    project: item.project,
    from: item.from?.name ?? null,
    waited: item.waited,
    since: item.since,
    holds_up: extra.holds_up !== undefined ? extra.holds_up : item.question?.blocks ?? null,
    blocking: item.blocking === true,
    recommendation: recommendation ? { answer: recommendation, why: extra.why ?? null } : null,
    yes,
    no,
    deeper: { detail: item.detail, lines: item.lines ?? [], checks: extra.checks ?? null, href: deeperHref(item) },
  };
}

/** Blocking first, then the longest wait (the home's own order, TR-1021). */
export function feedOrder(a: FeedCard, b: FeedCard): number {
  if (a.blocking !== b.blocking) return a.blocking ? -1 : 1;
  return (a.since ?? "").localeCompare(b.since ?? "");
}
