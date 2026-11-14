import { Link } from "react-router-dom";

import { Button, EmptyState, ListSection } from "../../components/edition";
import type { HomeRaised } from "./Home.api";

/** "Last things raised": what the watcher raised, how it came out, and the person's answer to
 *  whether it was worth raising. An item nobody can answer for links to its note instead. */
export function RaisedList({ items, note, busy, canAct = true, onAnswer }: {
  items: HomeRaised[];
  note: string | null;
  /** The answer in flight ("<id>:worth-yes" or "<id>:worth-no"), or null. */
  busy: string | null;
  /** False for a read-only key: the question is not asked. */
  canAct?: boolean;
  onAnswer: (id: string, worth: boolean) => void;
}) {
  return (
    <ListSection
      paper
      title="Last things raised"
      aside={<Link to="/incidents?show=all" className="text-foreground underline underline-offset-2" data-testid="all-findings">All findings</Link>}
      data-testid="raised"
    >
      {items.length === 0 ? (
        <EmptyState className="px-[var(--row-x,18px)] py-4" data-testid="raised-empty">Nothing has been raised yet.</EmptyState>
      ) : (
        <ul className="m-0! list-none! p-0!">
          {items.map((r) => {
            const yes = busy === `${r.id}:worth-yes`, no = busy === `${r.id}:worth-no`;
            return (
              <li key={r.id} className="grid gap-x-4 gap-y-2 border-t border-border px-[var(--row-x,18px)] py-3 first:border-t-0 sm:grid-cols-[64px_minmax(0,1fr)_auto]" data-testid="raised-row" data-run={r.id}>
                <div className="font-mono text-xs text-muted-foreground">{r.when_words}</div>
                <div className="min-w-0 text-[14px] [overflow-wrap:anywhere]">
                  <div><b data-user-text>{r.service}</b> {r.what && <span data-user-text>· {r.what}</span>}</div>
                  <div className="text-[13px] text-muted-foreground">{r.outcome_words}</div>
                </div>
                <div className="flex flex-wrap items-center gap-2 text-[13px]">
                  {r.worth != null ? (
                    <span className="text-muted-foreground" data-testid="raised-answer">{r.worth ? "You said it was worth raising." : "You said it was not worth raising."}</span>
                  ) : r.can_answer && canAct ? (
                    <>
                      <span className="text-muted-foreground">Was this worth raising?</span>
                      <Button variant="secondary" className="h-8 text-[13px]!" busy={yes} disabled={busy !== null && !yes} onClick={() => onAnswer(r.id, true)} data-testid="worth-yes">Yes</Button>
                      <Button variant="secondary" className="h-8 text-[13px]!" busy={no} disabled={busy !== null && !no} onClick={() => onAnswer(r.id, false)} data-testid="worth-no">No</Button>
                    </>
                  ) : r.incident_id ? (
                    <Link to={`/incidents/${encodeURIComponent(r.incident_id)}`} className="text-foreground underline underline-offset-2" data-testid="read-note">Read the note</Link>
                  ) : null}
                </div>
              </li>
            );
          })}
        </ul>
      )}
      {note && <p className="m-0! border-t border-border px-[var(--row-x,18px)] py-3 text-[13px] text-muted-foreground" data-testid="raised-note">{note}</p>}
    </ListSection>
  );
}
