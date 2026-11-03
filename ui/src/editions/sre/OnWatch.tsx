import { Link } from "react-router-dom";

import { ListSection, StatusDot, type Tone } from "../../components/edition";
import type { HomeOnWatch } from "./Home.api";

const STATE: Record<string, { words: Record<string, string>; tone: Tone }> = {
  running: { words: { watcher: "Watching", investigator: "Investigating" }, tone: "working" },
  stopped: { words: { watcher: "Stopped", investigator: "Stopped" }, tone: "bad" },
};

function stateOf(w: HomeOnWatch): { words: string; tone: Tone } {
  const s = STATE[w.state];
  return s ? { words: s.words[w.role] ?? "Working", tone: s.tone } : { words: "Waiting", tone: "quiet" };
}

/** "Haiku, Spent $3.74 of $25" shown as "Haiku · Spent $3.74 of $25". */
const costLine = (w: HomeOnWatch) =>
  w.model_words && w.cost_words.startsWith(`${w.model_words}, `) ? `${w.model_words} · ${w.cost_words.slice(w.model_words.length + 2)}` : w.cost_words;

/** "Who is on watch": the watcher and the investigator, what each is doing and what it has cost,
 *  and where the notes go. */
export function OnWatch({ items, notesGoTo }: { items: HomeOnWatch[]; notesGoTo: { label: string } | null }) {
  return (
    <ListSection title="Who is on watch" data-testid="on-watch">
      <ul className="m-0! list-none! p-0!">
        {items.map((w) => {
          const s = stateOf(w);
          return (
            <li key={`${w.role}-${w.name}`} className="grid gap-0.5 border-t border-border px-[18px] py-3 first:border-t-0" data-testid="on-watch-item" data-role={w.role}>
              <div className="flex flex-wrap items-center gap-x-2.5">
                <b>{w.name}</b>
                <span className="flex items-center gap-1.5 text-[13px]" data-testid="on-watch-state"><StatusDot tone={s.tone} />{s.words}</span>
              </div>
              <div className="text-[13px]" data-user-text>{w.doing}</div>
              <div className="text-xs text-muted-foreground" data-user-text>{costLine(w)}</div>
            </li>
          );
        })}
      </ul>
      {notesGoTo && (
        <p className="m-0! border-t border-border px-[18px] py-3 text-[13px] text-muted-foreground" data-testid="notes-go">
          Notes go to <Link to="/settings#tools" className="text-foreground underline underline-offset-2" data-user-text>{notesGoTo.label}</Link>
        </p>
      )}
    </ListSection>
  );
}
