import * as React from "react";
import { Link } from "react-router-dom";

import {
  Banner, Button, EmptyState, LoadingBlocks, ServiceRows, type ServiceRowItem,
} from "../../components/edition";
import { useSample } from "../sample";
import { usePagePolling } from "../usePagePolling";
import { sreServices, type SreServiceRow, type SreServices } from "./Services.api";
import { isListSample, sampleServices } from "./sample/services";
import { count, serviceHref, STATE_TONE, up } from "./serviceWords";

// Services: every service as one row, from one read (GET /api/sre/services). The rows come in
// this order: incident, not watched, unusual, silent, no signal of its own, normal, then by name.
// The first PAGE_SIZE show and "Show more" adds that many again; the "Showing N of M" line counts the rows on the page. Hidden services stay out until the person asks
// (a second read with ?hidden=1). A name or what the person calls it is their own text.

const POLL_MS = 30_000;
const PAGE_SIZE = 20;
const SETUP_TO = "/settings#tools";

const asRow = (r: SreServiceRow): ServiceRowItem => ({
  id: r.name,
  name: r.name,
  to: serviceHref(r.name),
  about: r.about,
  tone: STATE_TONE[r.state],
  stateWords: r.state_words,
  signals: r.signals_words,
  feeds: r.feeds_words,
  activity: [r.events_24h_words, up(r.last_words)],
  spark: r.spark,
  sparkLabel: `${r.name}, errors in the last day`,
  hidden: r.hidden,
});

/** "12 services. 1 has an open incident, 3 need a look." */
export function summary(rows: SreServiceRow[]): string {
  const incident = rows.filter((r) => r.state === "incident").length;
  const look = rows.filter((r) => r.state === "unusual" || r.state === "silent" || r.state === "not_watched").length;
  const parts = [
    incident ? `${incident} ${incident === 1 ? "has" : "have"} an open incident` : "",
    look ? `${look} ${look === 1 ? "needs" : "need"} a look` : "",
  ].filter(Boolean);
  return `${count(rows.length, "service")}. ${parts.length ? up(parts.join(", ")) : "All normal"}.`;
}


function Head({ intro, children }: { intro?: string; children?: React.ReactNode }) {
  return (
    <div className="grid gap-6 md:gap-8" data-testid="sre-services">
      <div className="flex flex-wrap items-start justify-between gap-x-6 gap-y-3">
        <div className="min-w-0">
          <h1 className="m-0! mb-1.5! text-[22px]! md:text-[28px]!">Services</h1>
          <p className="m-0! max-w-[640px] text-[15px] text-muted-foreground" data-testid="services-summary">
            {intro ?? "Everything your logs and deploys tell Tares about."}
          </p>
        </div>
      </div>
      {children}
    </div>
  );
}

/** Pure: draws the answer it is given. `showHidden` and `onToggleHidden` belong to the page that reads. */
export function ServicesView({ data, showHidden = false, onToggleHidden }: {
  data: SreServices;
  showHidden?: boolean;
  onToggleHidden?: () => void;
}) {
  const [shown, setShown] = React.useState(PAGE_SIZE);
  const rows = data.services;
  const visible = rows.slice(0, shown);
  const toggle = data.hidden_count > 0 && (
    <Button variant="secondary" onClick={onToggleHidden} data-testid="toggle-hidden" className="h-8 text-[13px]!">
      {showHidden ? "Hide hidden" : `Show hidden (${data.hidden_count})`}
    </Button>
  );

  if (rows.length === 0) {
    const allHidden = data.hidden_count > 0 && !showHidden;
    return (
      <Head>
        {toggle && <div>{toggle}</div>}
        <div className="border border-border bg-card" data-testid="services-empty">
          {allHidden ? (
            <EmptyState title="Every service is hidden">Show hidden to see them. Hidden services are not checked.</EmptyState>
          ) : (
            <EmptyState
              title="No services yet"
              action={<Button asChild><Link to={SETUP_TO} data-testid="connect-link">Connect where your logs come from</Link></Button>}
            >
              A service appears here as soon as Tares receives its logs.
            </EmptyState>
          )}
        </div>
      </Head>
    );
  }

  return (
    <Head intro={summary(rows)}>
      <div className="grid gap-3">
        {toggle && <div className="flex justify-end">{toggle}</div>}
        <div className="border border-border bg-card" data-testid="services-list">
          <ServiceRows rows={visible.map(asRow)} />
          {rows.length > PAGE_SIZE && (
            <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2 border-t border-border px-[18px] py-3 text-xs text-muted-foreground">
              <span data-testid="services-showing">Showing {visible.length} of {rows.length}</span>
              {visible.length < rows.length && (
                <Button variant="secondary" onClick={() => setShown((n) => n + PAGE_SIZE)} data-testid="show-more" className="h-8 text-[13px]!">
                  Show {Math.min(PAGE_SIZE, rows.length - visible.length)} more
                </Button>
              )}
            </div>
          )}
        </div>
        {data.merged_words && <p className="m-0 text-xs text-muted-foreground" data-testid="services-merged">{data.merged_words}</p>}
      </div>
    </Head>
  );
}

export default function Services() {
  const named = useSample();
  const sample = isListSample(named) ? named : null;
  const [showHidden, setShowHidden] = React.useState(sample === "hidden");
  const askRef = React.useRef(showHidden);
  const { data, error, reload } = usePagePolling<SreServices>(
    () => (sample ? sampleServices(sample, askRef.current) : sreServices(askRef.current)), POLL_MS);

  const toggle = () => {
    askRef.current = !showHidden;
    setShowHidden(!showHidden);
    reload();
  };

  const banner = error && (
    <Banner tone="error" action={<Button variant="secondary" onClick={reload}>Retry</Button>} data-testid="services-error">
      <b>Services did not load.</b> <span className="text-foreground">The cell did not answer. Try again in a moment.</span>
    </Banner>
  );
  if (!data) return <Head>{banner || <LoadingBlocks data-testid="services-loading" count={4} widths={["28%", "60%", "42%"]} />}</Head>;
  return (
    <>
      {banner && <div className="mb-6">{banner}</div>}
      <ServicesView data={data} showHidden={showHidden} onToggleHidden={toggle} />
    </>
  );
}
