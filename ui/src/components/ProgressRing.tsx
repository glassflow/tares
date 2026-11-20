import type { Ticket, TicketProgress } from "../types";

// A software factory project's tickets at a glance: a small ring whose sectors are merged,
// closed, in progress, stuck, queued and to do, in that order clockwise from 12 o'clock. The
// Projects list gets the counts from GET /api/projects; the project page counts the tickets it
// already has, with the same rules (ticketProgress below, tares/factory.py `progress` and
// `count_of` on the server, the status-words spec's decision 2).

const ORDER = ["merged", "closed", "doing", "stuck", "queued", "todo"] as const;

type Bucket = (typeof ORDER)[number] | "canceled";

/** Where a ticket counts, as factory.count_of reads it off the ticket's turn. Merged only for a
 *  merge (the stage, or the pull request itself); a ticket done in its home system with no merge
 *  is closed. A station's stop is stuck; a ticket to do that waits on one not delivered is
 *  queued. A ticket in review counts as in progress whatever its verdict. */
export function ticketBucket(t: Ticket): Bucket {
  return t.stage === "merged" || t.stage === "shipped" || t.pr?.merged ? "merged"
    : t.status === "done" ? "closed"
    : t.status === "canceled" ? "canceled"
    : t.stage === "blocked" ? "stuck"
    : t.stage === "doing" || t.stage === "review" || t.stage === "changes" || t.status === "in_progress" ? "doing"
    : t.blocked_by.length ? "queued"
    : "todo";
}

/** The tickets in the ring's buckets; canceled ones stay out of the total. */
export function ticketProgress(tickets: Ticket[]): TicketProgress {
  const n = { merged: 0, closed: 0, doing: 0, stuck: 0, queued: 0, todo: 0, canceled: 0 };
  for (const t of tickets) n[ticketBucket(t)] += 1;
  return { ...n, total: tickets.length - n.canceled };
}

/** "5 merged, 2 queued, 1 stuck of 8", as tares/status_words.py `count_line` writes it: merged
 *  always, closed, queued and stuck only when there are some. */
export function countLine(p: Pick<TicketProgress, "merged" | "total"> & Partial<TicketProgress>): string {
  if (!p.total) return "No tickets yet";
  const parts = [`${p.merged} merged`];
  if (p.closed) parts.push(`${p.closed} closed`);
  if (p.queued) parts.push(`${p.queued} queued`);
  if (p.stuck) parts.push(`${p.stuck} stuck`);
  return `${parts.join(", ")} of ${p.total}`;
}

/** The count line, then what it leaves out: "5 merged, 1 stuck of 8. 2 in progress, 0 to do" */
export function progressText(p: TicketProgress): string {
  if (!p.total) return p.canceled ? "No open tickets, every one is canceled" : "No tickets yet";
  const rest = [p.doing ? `${p.doing} in progress` : "", p.todo ? `${p.todo} to do` : ""].filter(Boolean);
  const canceled = p.canceled ? `${p.canceled} canceled, not counted` : "";
  return [countLine(p), rest.join(", "), canceled].filter(Boolean).join(". ");
}

export function ProgressRing({ progress, size = 18, className }: {
  progress: TicketProgress;
  size?: number;          // px, the ring's width and height
  className?: string;
}) {
  const w = size / 5;                       // the ring's thickness
  const r = (size - w) / 2;
  const c = size / 2;
  const around = 2 * Math.PI * r;
  const shown = ORDER.filter((k) => progress[k] > 0);
  const gap = shown.length > 1 ? Math.min(1.2, around / 40) : 0;
  const text = progressText(progress);
  // Each sector gets a floor so one ticket among many still shows, and stuck a wider one: it
  // is what a glance at the ring is for. The length a floor adds comes off the sectors above
  // their floor, so the ring still closes and no sector is drawn over the next.
  const want = shown.map((k) => (progress[k] / progress.total) * around);
  const floor = shown.map((k) => Math.max(gap + 1, k === "stuck" ? around * 0.12 : 0));
  const len = want.map((x, i) => Math.max(x, floor[i]));
  const over = len.reduce((a, b) => a + b, 0) - around;
  const spare = len.reduce((a, x, i) => a + (x - floor[i]), 0);
  if (over > 0 && spare > 0) len.forEach((x, i) => { len[i] = x - over * ((x - floor[i]) / spare); });
  let at = 0;
  return (
    <svg className={`fo-ring${className ? ` ${className}` : ""}`} width={size} height={size}
         viewBox={`0 0 ${size} ${size}`} role="img" aria-label={text}>
      <title>{text}</title>
      {!progress.total
        ? <circle className="fo-ring-todo" cx={c} cy={c} r={r} strokeWidth={w} fill="none" />
        : shown.map((k, i) => {
            const start = at;
            at += len[i];
            return (
              <circle key={k} className={`fo-ring-${k}`} cx={c} cy={c} r={r} strokeWidth={w} fill="none"
                      strokeDasharray={`${shown.length > 1 ? len[i] - gap : around} ${around}`}
                      strokeDashoffset={-start} transform={`rotate(-90 ${c} ${c})`} />
            );
          })}
    </svg>
  );
}
