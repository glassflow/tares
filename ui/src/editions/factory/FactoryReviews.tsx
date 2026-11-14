import * as React from "react";
import { Link, useSearchParams } from "react-router-dom";

import { Banner, Button, ListRow, ListSection, LoadingBlocks, STATUS_TONES, StatusDot } from "../../components/edition";
import { cn } from "../../lib/utils";
import { usePagePolling } from "../usePagePolling";
import { useSample } from "../sample";
import { factoryReviews, type FactoryReviewsData, type ReviewRow } from "./FactoryReviews.api";
import { isSample, sampleLoad } from "./reviewsSamples";

// Reviews: every pull request in review, across projects, in three lists: the ones that wait for the
// person to merge, the ones a named role of the crew has next, and the ones nobody is on, each with
// its reason (TR-1009). One read (GET /api/factory/reviews), which
// the page repeats every 20 seconds. `?project=<id>` in the address cuts the lists to one project.
// Every string from the cell is a React text node; project, ticket and pull request titles, the repo
// and the milestone are the person's own text and marked data-user-text.

const POLL_MS = 20_000;
const MUTED = "text-[13px] text-muted-foreground";
const LINK = "text-foreground! underline! decoration-border! underline-offset-[3px]";

/** A link only to a web address; anything else is shown as plain text. */
const webUrl = (u: string | null): u is string => !!u && /^https?:\/\//i.test(u);

function Row({ r }: { r: ReviewRow }) {
  const { number, url, title, repo } = r.pr;
  return (
    <ListRow className="grid grid-cols-1 items-start gap-x-4 gap-y-1.5 md:grid-cols-[1fr_auto]"
             data-testid="review-row" data-project={r.project.id} data-ci={r.ci_state}>
      <div className="min-w-0">
        <div className="font-semibold">
          <Link to={r.ticket.href} data-user-text data-testid="review-title" className={LINK}>{r.ticket.title}</Link>
        </div>
        <div className={MUTED} data-testid="review-meta">
          <span data-user-text data-testid="review-project">{r.project.name}</span>
          {" · "}{r.ticket.ref}
          {number != null && (
            <>
              {" · "}
              {webUrl(url)
                ? <a href={url} target="_blank" rel="noreferrer" title={title ?? undefined} data-testid="review-pr" className="text-muted-foreground! underline! decoration-border! underline-offset-[3px]">#{number}</a>
                : <span data-testid="review-pr">#{number}</span>}
              {repo && <> on <span data-user-text>{repo}</span></>}
            </>
          )}
        </div>
      </div>
      <div className="whitespace-nowrap font-mono text-[13px] md:text-right" data-testid="review-waited">{r.waited}</div>
      <div className={cn("col-span-full flex flex-wrap gap-x-4 gap-y-1", MUTED)}>
        <span data-testid="review-verdict">{r.review}</span>
        <span data-testid="review-ci"><StatusDot tone={STATUS_TONES.ci[r.ci_state] ?? "quiet"} className="mr-1.5 rounded-full" />{r.ci}</span>
      </div>
      {r.why && (
        <div className="col-span-full text-[13px] text-foreground" data-testid="review-why">{r.why}</div>
      )}
      {r.holds_up && (
        <div className="col-span-full text-[13px] font-semibold text-foreground [overflow-wrap:anywhere]" data-user-text data-testid="review-holds">
          {r.holds_up}
        </div>
      )}
    </ListRow>
  );
}


/** The lists for one read of the cell. `unknown` is a project in the address the cell does not know. */
export function FactoryReviewsView({ data, unknown = false }: { data: FactoryReviewsData; unknown?: boolean }) {
  const { waiting_for_you: mine, with_the_crew: theirs } = data;
  const nobody = data.nobody_on_it ?? [];
  // the crew's list is folded away while something waits for you, and open when nothing does
  const [folded, setFolded] = React.useState<boolean | null>(null);
  const open = folded === null ? mine.length === 0 : !folded;
  return (
    <ReviewsHead>
      {data.project && (
        <div className={cn("flex flex-wrap items-center gap-2", MUTED)} data-testid="reviews-filter">
          <span>Showing <b data-user-text>{data.project.name}</b>.</span>
          <Link to="/reviews" className="text-foreground! underline!">Show all</Link>
        </div>
      )}
      {unknown && (
        <div className={cn("flex flex-wrap items-center gap-2", MUTED)} data-testid="reviews-unknown">
          <span>No project has that address.</span>
          <Link to="/reviews" className="text-foreground! underline!">Show all</Link>
        </div>
      )}
      <p className="m-0 text-[13px] text-muted-foreground" data-testid="reviews-autonomy">{data.autonomy_text}</p>
      <ListSection title="Waiting for you" aside={<span className="font-mono" data-testid="waiting-count">{mine.length}</span>} data-testid="reviews-waiting">
        {mine.length === 0
          ? <div className="px-4 py-7 text-muted-foreground" data-testid="reviews-waiting-none">Nothing waits for your review.</div>
          : mine.map((r) => <Row key={`${r.project.id}/${r.ticket.ref}`} r={r} />)}
      </ListSection>
      {nobody.length > 0 && (
        <ListSection title="Nobody is on it" aside={<span className="font-mono" data-testid="nobody-count">{nobody.length}</span>} data-testid="reviews-nobody">
          {nobody.map((r) => <Row key={`${r.project.id}/${r.ticket.ref}`} r={r} />)}
        </ListSection>
      )}
      {theirs.length > 0 && (
        <div data-testid="reviews-crew">
          <button type="button" aria-expanded={open} aria-controls="reviews-crew-list" data-testid="crew-fold"
                  onClick={() => setFolded(open)}
                  className="flex! w-full cursor-pointer items-center justify-between! gap-3 border border-border bg-card px-4 py-3 text-left font-semibold hover:bg-[var(--row-hover)]">
            <span>With the crew</span>
            <span className="font-mono font-normal" data-testid="crew-count">{theirs.length} {open ? "−" : "+"}</span>
          </button>
          {open && (
            <div id="reviews-crew-list" className="min-w-0 border border-t-0 border-border bg-card [overflow-wrap:anywhere]" data-testid="crew-list">
              {theirs.map((r) => <Row key={`${r.project.id}/${r.ticket.ref}`} r={r} />)}
            </div>
          )}
        </div>
      )}
    </ReviewsHead>
  );
}

function Reviews({ project }: { project: string }) {
  const named = useSample();
  const sample = isSample(named) ? named : null;
  const { data, error, reload } = usePagePolling<FactoryReviewsData>(
    () => (sample ? sampleLoad(sample) : factoryReviews(project || undefined)), POLL_MS);

  const banner = error && (
    <Banner tone="error" action={<Button variant="secondary" onClick={reload}>Retry</Button>}>
      <b>Reviews did not load.</b> <span className="text-foreground">The cell did not answer. Try again in a moment.</span>
    </Banner>
  );
  if (!data) return <ReviewsHead>{banner || <LoadingBlocks data-testid="reviews-loading" widths={["55%", "35%", "70%"]} />}</ReviewsHead>;
  return (
    <>
      {banner && <div className="mb-6">{banner}</div>}
      <FactoryReviewsView data={data} unknown={!!project && !sample && !data.project} />
    </>
  );
}

/** The page: `key` starts a fresh read when the project in the address changes. */
export default function FactoryReviews() {
  const [params] = useSearchParams();
  const project = params.get("project") ?? "";
  return <Reviews key={project} project={project} />;
}

/** The title, over what the page has to show. */
function ReviewsHead({ children }: { children?: React.ReactNode }) {
  return (
    <div className="grid gap-5 md:gap-6" data-testid="factory-reviews">
      <div>
        <h1 className="m-0! mb-1.5! text-[22px]! md:text-[28px]!">Reviews</h1>
        <p className="m-0! max-w-[640px] text-[15px] text-muted-foreground">Every pull request, across projects, that waits for a person.</p>
      </div>
      {children}
    </div>
  );
}
