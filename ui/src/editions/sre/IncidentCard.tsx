import { Link } from "react-router-dom";

import { Button, LineChart } from "../../components/edition";
import type { HomeIncident } from "./Home.api";
import { atWords } from "./homeWords";

const COL = "grid content-start gap-1";
const K = "font-mono text-[11px] uppercase tracking-[0.08em] text-muted-foreground";

/** One open incident: what is failing, the likely cause and the next step, the errors around it
 *  with a mark for each deploy, and the buttons that act on it. */
export function IncidentCard({ incident: i, busy, canAct = true, onPickUp, onResolve }: {
  incident: HomeIncident;
  /** The incident action in flight ("<id>:on_it"), or null. */
  busy: string | null;
  /** False for a read-only key: the buttons that change the incident are left out. */
  canAct?: boolean;
  onPickUp: (id: string) => void;
  onResolve: (id: string) => void;
}) {
  const pickedUp = i.state === "on_it";
  const points = i.chart?.points ?? [];
  return (
    <section className="min-w-0 border border-foreground bg-card [overflow-wrap:anywhere]" data-testid="incident-card" data-incident={i.id}>
      <div className="border-b border-border bg-[var(--err-soft)] px-[18px] py-3">
        <h2 className="m-0! mb-0.5! text-[15px]!" data-user-text data-testid="incident-title">{i.title}</h2>
        <div className="text-[13px] text-muted-foreground" data-testid="incident-age">{i.age_words} · {i.who_words}</div>
      </div>
      <div className="grid gap-5 p-[18px]">
        <div className="grid gap-x-6 gap-y-4 md:grid-cols-3">
          <div className={COL} data-testid="incident-failing">
            <div className={K}>What is failing</div>
            <div className="text-[14px] leading-snug" data-user-text>{i.failing ?? "Not known yet."}</div>
          </div>
          <div className={COL} data-testid="incident-cause">
            <div className={K}>Most likely cause</div>
            <div className="text-[14px] leading-snug" data-user-text>{i.likely_cause ?? "Not known yet."}</div>
          </div>
          <div className={COL} data-testid="incident-next">
            <div className={K}>Suggested next step</div>
            <div className="text-[14px] leading-snug" data-user-text>{i.next_step ?? "Not known yet."}</div>
          </div>
        </div>
        {i.chart && points.length > 0 && (
          <div data-testid="incident-chart">
            <LineChart
              series={[{ label: i.chart.label, points }]}
              marks={i.chart.deploys}
              marksList
              height={120}
              aria-label={`${i.service}, ${i.chart.label}`}
            />
          </div>
        )}
        <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-3 border-t border-border pt-4">
          <p className="m-0! min-w-0 text-[13px] text-muted-foreground" data-testid="incident-foot">
            {i.posted
              ? <>Posted to <span data-user-text>{i.posted.where}</span> {atWords(i.posted.at)}{i.based_on ? ` · based on ${i.based_on}` : ""}</>
              : i.based_on ? `Based on ${i.based_on}` : "Not posted anywhere yet."}
          </p>
          <div className="flex flex-wrap gap-2">
            <Button asChild variant="secondary"><Link to={`/timeline?service=${encodeURIComponent(i.service)}`} data-testid="incident-timeline">Open the timeline</Link></Button>
            {canAct && <Button variant="secondary" busy={busy === `${i.id}:resolve`} disabled={busy !== null && busy !== `${i.id}:resolve`}
              onClick={() => onResolve(i.id)} data-testid="incident-resolve">Mark resolved</Button>}
            {canAct && !pickedUp && (
              <Button busy={busy === `${i.id}:on_it`} disabled={busy !== null && busy !== `${i.id}:on_it`}
                onClick={() => onPickUp(i.id)} data-testid="incident-on-it">I&apos;m on it</Button>
            )}
          </div>
        </div>
      </div>
    </section>
  );
}
