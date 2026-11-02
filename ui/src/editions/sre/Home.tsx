import * as React from "react";
import { Link } from "react-router-dom";

import {
  Banner, Button, EmptyState, ListSection, ServiceRows, Skeleton, type ServiceRowItem,
} from "../../components/edition";
import { useSample } from "../sample";
import { usePagePolling } from "../usePagePolling";
import { BlindSpots } from "./BlindSpots";
import {
  answerWorth, homeAction, pickUpIncident, resolveIncident, sreHome, withIncident, withWorth,
  type HomeService, type HomeWayOut, type SreHome,
} from "./Home.api";
import { IncidentCard } from "./IncidentCard";
import { OnWatch } from "./OnWatch";
import { RaisedList } from "./RaisedList";
import { Tools } from "./Tools";
import { WatchPanel } from "./WatchPanel";
import { plural, money } from "./homeWords";
import { isHomeSample, sampleHome, sampleRaise } from "./sample/home";
import { serviceHref, STATE_TONE, up } from "./serviceWords";

// The AI SRE home: what is wrong now (the watcher is off, or an incident is open), the services,
// and what was last raised, with who is on watch, today, blind spots and what is connected beside
// them. One read (GET /api/home) fills it; the buttons call their own routes.

const POLL_MS = 30_000;
// The cell keeps its answer for 5 seconds. The incident and answer routes do not clear it, so the
// page reads again just after it has expired.
const REFRESH_MS = 5_500;
const SERVICES_SHOWN = 10;
const SETUP_TO = "/settings#tools";

const asRow = (s: HomeService): ServiceRowItem => ({
  id: s.name,
  name: s.name,
  to: serviceHref(s.name),
  about: s.about,
  tone: STATE_TONE[s.state],
  stateWords: s.state_words,
  signals: s.signals_words,
  activity: [up(s.last_words)],
  spark: s.spark,
  sparkLabel: `${s.name}, errors in the last day`,
});

/** The two columns every state of the page shares. */
function Columns({ main, aside }: { main: React.ReactNode; aside: React.ReactNode }) {
  return (
    <div className="flex flex-wrap items-start gap-8">
      <div className="grid min-w-0 gap-8 [flex:999_1_540px]" data-testid="home-main">{main}</div>
      <div className="grid min-w-0 gap-6 [flex:1_1_300px]" data-testid="home-aside">{aside}</div>
    </div>
  );
}

function Head({ title, sub }: { title: React.ReactNode; sub?: string }) {
  return (
    <div className="min-w-0">
      <h1 className="m-0! mb-1.5! text-[22px]! md:text-[28px]!" data-testid="home-headline" data-user-text>{title}</h1>
      {sub ? <p className="m-0! max-w-[720px] text-[15px] text-muted-foreground" data-testid="home-sub" data-user-text>{sub}</p> : null}
    </div>
  );
}

function Loading() {
  const box = (rows: number, key: string) => (
    <div key={key} className="border border-border bg-card p-4">
      {Array.from({ length: rows }, (_, i) => (
        <div key={i} className="grid gap-2 pb-4 pt-1"><Skeleton width="28%" /><Skeleton width="60%" /><Skeleton width="42%" /></div>
      ))}
    </div>
  );
  return (
    <div className="grid gap-6 md:gap-8" role="status" aria-busy="true" aria-label="Loading" data-testid="home-loading">
      <div className="grid gap-2"><Skeleton width="40%" height={28} /><Skeleton width="60%" height={14} /></div>
      <Columns main={[box(3, "a"), box(3, "b")]} aside={[box(2, "c"), box(1, "d")]} />
    </div>
  );
}

function ServicesBox({ home }: { home: SreHome }) {
  const rows = home.services.slice(0, SERVICES_SHOWN);
  return (
    <ListSection title="Services" aside={home.services_from_words ? <span data-user-text>{home.services_from_words}</span> : undefined} data-testid="home-services">
      <ServiceRows rows={rows.map(asRow)} />
      {home.services.length > SERVICES_SHOWN && (
        <div className="border-t border-border px-[18px] py-3 text-[13px]">
          <Link to="/services" className="text-foreground underline underline-offset-2" data-testid="all-services">All {plural(home.services.length, "service")}</Link>
        </div>
      )}
    </ListSection>
  );
}

function Today({ home }: { home: SreHome }) {
  const t = home.today;
  const quiet = t.quiet >= t.checks ? "all quiet" : `all quiet but ${t.checks - t.quiet}`;
  return (
    <ListSection title="Today" data-testid="home-today">
      <div className="grid gap-3 p-[18px]">
        <div className="grid grid-cols-2 gap-4">
          <div><div className="text-[22px] font-semibold leading-tight">{t.checks}</div><div className="text-[13px] text-muted-foreground">{t.checks === 1 ? "check" : "checks"}, {quiet}</div></div>
          <div><div className="text-[22px] font-semibold leading-tight">{t.investigated}</div><div className="text-[13px] text-muted-foreground">investigated</div></div>
        </div>
        <div className="text-[13px] text-muted-foreground">Spent {money(t.spent_usd)} today</div>
      </div>
    </ListSection>
  );
}

export default function Home() {
  const named = useSample();
  const sample = isHomeSample(named) ? named : null;
  const { data, error, reload } = usePagePolling<SreHome>(() => (sample ? sampleHome(sample) : sreHome()), POLL_MS);

  // What the person's last action changed, until the next answer from the cell replaces it.
  const [shown, setShown] = React.useState<{ base: SreHome; home: SreHome } | null>(null);
  const [busy, setBusy] = React.useState<string | null>(null);
  const [failed, setFailed] = React.useState(false);
  const timer = React.useRef<ReturnType<typeof setTimeout>>();
  React.useEffect(() => () => clearTimeout(timer.current), []);

  const home = data && shown && shown.base === data ? shown.home : data;

  const act = async (key: string, run: () => Promise<SreHome | void>, refresh = true) => {
    if (!data || !home) return;
    setBusy(key);
    setFailed(false);
    try {
      const next = (await run()) ?? undefined;
      if (next) setShown({ base: data, home: next });
      if (refresh && !sample) { clearTimeout(timer.current); timer.current = setTimeout(reload, REFRESH_MS); }
    } catch {
      setFailed(true);
    } finally {
      setBusy(null);
    }
  };

  const wayOut = (w: HomeWayOut) =>
    act(w.action, () => (sample ? Promise.resolve(sampleRaise()) : homeAction(w.action, { amount_usd: w.amount_usd })), false);
  const change = (id: string, to: "on_it" | "resolved") =>
    act(`${id}:${to === "on_it" ? "on_it" : "resolve"}`, async () => {
      if (!sample) await (to === "on_it" ? pickUpIncident(id) : resolveIncident(id));
      return withIncident(home!, id, to);
    });
  const answer = (id: string, worth: boolean) =>
    act(`${id}:worth`, async () => {
      if (!sample) await answerWorth(id, worth);
      return withWorth(home!, id, worth);
    });

  const banner = error && (
    <Banner tone="error" action={<Button variant="secondary" onClick={reload}>Retry</Button>} data-testid="home-error">
      <b>The home page did not load.</b>{" "}
      <span className="text-foreground">{/authentication/i.test(error) ? "You are signed out. Sign in again." : "The cell did not answer. Try again in a moment."}</span>
    </Banner>
  );

  if (!home) return banner ? <div className="grid gap-6" data-testid="sre-home">{banner}</div> : <div data-testid="sre-home"><Loading /></div>;

  const isNew = home.headline.tone === "new";
  const off = !home.watch.on && !isNew;
  const top = (
    <>
      {off && <WatchPanel watch={home.watch} busy={busy} onWayOut={wayOut} />}
      {home.incidents.map((i) => (
        <IncidentCard key={i.id} incident={i} busy={busy} onPickUp={(id) => change(id, "on_it")} onResolve={(id) => change(id, "resolved")} />
      ))}
      {home.incidents_more > 0 && (
        <p className="m-0! text-[13px]"><Link to="/incidents" className="text-foreground underline underline-offset-2" data-testid="incidents-more">{home.incidents_more} more open</Link></p>
      )}
    </>
  );

  const main = isNew ? (
    <div className="border border-border bg-card" data-testid="home-empty">
      <EmptyState
        title="No services yet"
        action={<Button asChild><Link to={SETUP_TO} data-testid="connect-link">Connect</Link></Button>}
      >
        Connect where your logs, metrics or alerts come from. A service appears here as soon as Tares receives them.
      </EmptyState>
    </div>
  ) : (
    <>
      {top}
      <ServicesBox home={home} />
      <RaisedList items={home.raised} note={home.raised_note} busy={busy} onAnswer={answer} />
    </>
  );

  const aside = (
    <>
      {home.on_watch.length > 0 && <OnWatch items={home.on_watch} notesGoTo={home.notes_go_to} />}
      {home.watch.on && <Today home={home} />}
      <BlindSpots items={home.blind_spots} />
      <Tools items={home.tools} />
    </>
  );

  return (
    <div className="grid gap-6 md:gap-8" data-testid="sre-home" data-tone={home.headline.tone}>
      {banner}
      {failed && <Banner tone="error" data-testid="home-action-error">That did not go through. Try again in a moment.</Banner>}
      <Head title={home.headline.text} sub={home.headline.sub} />
      <Columns main={main} aside={aside} />
    </div>
  );
}
