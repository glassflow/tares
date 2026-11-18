import { request } from "../../api";
import type { FeedAnswered, FeedVia } from "./feed.types";

// The feed's one read of its own: the cell's record of the day's answers (GET /api/answers, TR-1451), for
// "Answered today". Every other call is Home's (FactoryHome.api.ts), through Home's handler.

/** One row of GET /api/answers (factory_api.answers_list); only what the feed reads. */
interface AnswerRow {
  id: string;
  ask: string;
  choice: "yes" | "no" | null;
  said: string;
  by: string | null;
  mine: boolean;
  at: string;
  via: FeedVia;
}

/** The answers given since the start of the person's day, newest first. The id is the home item's
 *  (`question:D7`, `grant:G2`), so an answer given on this page is not listed twice. */
export async function getAnsweredToday(now = new Date()): Promise<FeedAnswered[]> {
  const start = new Date(now.getFullYear(), now.getMonth(), now.getDate()).toISOString();
  const { answers } = await request<{ answers: AnswerRow[] }>(`/api/answers?since=${encodeURIComponent(start)}`);
  return answers.map((a) => ({
    id: a.id, ask: a.ask, choice: a.choice, said: a.said,
    by: a.mine ? "you" : a.by ?? "someone", at: a.at, via: a.via,
  }));
}
