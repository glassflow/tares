import { Button } from "../../components/edition";
import type { HomeWatch, HomeWayOut } from "./Home.api";
import { agoWords, atWords, money, plural } from "./homeWords";

/** The panel at the top of the page while the watcher is off: why, for how long, what it last said,
 *  and the button that starts it again. */
export function WatchPanel({ watch, busy, canAct = true, onWayOut }: {
  watch: HomeWatch;
  /** The action in flight, or null. */
  busy: string | null;
  /** False for a read-only key: the buttons are left out. */
  canAct?: boolean;
  onWayOut: (w: HomeWayOut) => void;
}) {
  const title = watch.reason_words ? `The watcher is off: ${watch.reason_words}` : "The watcher is off";
  const k = "font-mono text-[11px] uppercase tracking-[0.08em] text-muted-foreground";
  return (
    <section className="min-w-0 border-t border-foreground [overflow-wrap:anywhere]" data-testid="watch-off">
      <div className="pb-1 pt-3">
        <h2 className="m-0! text-[15px]!" data-testid="watch-off-title">{title}</h2>
      </div>
      <div className="grid gap-5 pt-4">
        <div className="grid gap-x-6 gap-y-4 sm:grid-cols-3">
          {watch.off_for_words && (
            <div className="grid content-start gap-1" data-testid="watch-off-for">
              <div className={k}>Off for</div>
              <div className="text-[22px] font-semibold leading-tight">{watch.off_for_words}</div>
              {watch.missed_checks != null && watch.missed_checks > 0 && (
                <div className="text-xs text-muted-foreground">about {plural(watch.missed_checks, "check")} missed</div>
              )}
            </div>
          )}
          {watch.last_check && (
            <div className="grid content-start gap-1" data-testid="watch-last-check">
              <div className={k}>Last check said</div>
              <div className="text-[15px] leading-snug" data-user-text>{watch.last_check.said}</div>
              <div className="text-xs text-muted-foreground">{agoWords(watch.last_check.at)}</div>
            </div>
          )}
          {watch.spent_words && (
            <div className="grid content-start gap-1" data-testid="watch-spent">
              <div className={k}>Spent</div>
              <div className="text-[22px] font-semibold leading-tight">{watch.spent_words}</div>
              {watch.cost_per_check_usd != null && <div className="text-xs text-muted-foreground">About {money(watch.cost_per_check_usd)} a check</div>}
              {watch.resets_on && <div className="text-xs text-muted-foreground" data-testid="watch-resets">Starts again on {watch.resets_on}</div>}
            </div>
          )}
        </div>
        <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-3 border-t border-border pt-4">
          <p className="m-0! text-[13px] text-muted-foreground" data-testid="watch-told">
            {watch.told ? <>Told <span data-user-text>{watch.told.where}</span> {atWords(watch.told.at)}.</> : "Nobody was told."}
          </p>
          <div className="flex flex-wrap gap-2">
            {canAct && watch.ways_out.map((w) => (
              <Button key={w.action} variant={w.primary ? "primary" : "secondary"} busy={busy === w.action} disabled={busy !== null && busy !== w.action}
                onClick={() => onWayOut(w)} data-testid={`way-out-${w.action}`}>
                {w.label}
              </Button>
            ))}
          </div>
        </div>
      </div>
    </section>
  );
}
