import { request } from "../../api";
import type { FeedAnswered } from "./feed.types";

// The feed's one read of its own: the desk's answered items, for "Answered today". Every other call is
// Home's (FactoryHome.api.ts), through Home's handler.

/** An answered desk row, as GET /api/desk sends it (factory_api.desk_out); only what the feed reads. */
interface DeskRow {
  label: string;
  question: string;
  command?: string | null;
  closed_at: string | null;
  by_label?: string | null;
}

/** The desk items answered since the start of the person's day, newest first, as feed rows. The id is
 *  the home item's (`question:D7`, `permission:D8`), so an answer given on this page is not listed twice. */
export async function getAnsweredToday(now = new Date()): Promise<FeedAnswered[]> {
  const { desk } = await request<{ desk: DeskRow[] }>("/api/desk?state=answered");
  const start = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  return desk
    .filter((d) => d.closed_at && Date.parse(d.closed_at) >= start)
    .sort((a, b) => Date.parse(b.closed_at!) - Date.parse(a.closed_at!))
    .map((d) => ({
      id: `${d.command ? "permission" : "question"}:${d.label}`,
      ask: d.question,
      choice: null,
      said: null,
      by: d.by_label === "You" || !d.by_label ? "you" : d.by_label,
      at: d.closed_at!,
      via: null,
    }));
}
