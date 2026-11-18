import * as React from "react";
import { Link, useLocation } from "react-router-dom";

import {
  Banner, Button, Collapse, EmptyState, ListRow, ListSection, SkeletonRows, StatusLabel, Toaster,
} from "../../components/edition";
import { sinceWords, formatStampUTC } from "../../lib/time";
import { cn } from "../../lib/utils";
import { useSample } from "../sample";
import { usePagePolling } from "../usePagePolling";
import { CACHE_MS, POLL_MS, useHomeActions, type Done, type RowState } from "./FactoryHome";
import { setSurface } from "../../api";
import { getHome } from "./FactoryHome.api";
import { getAnswerTo, getAnsweredToday } from "./feed.api";
import { Detail } from "./HomeDetail";
import type { HomeAction, HomeData } from "./home.types";
import type { FeedAnswered, FeedCard, FeedData } from "./feed.types";
import { feedOrder, toFeedCard } from "./feedCard";
import { FEED_SAMPLES } from "./feedSamples";

// The feed (P-TR-273): every decision the crew waits on, one card each, cleared with one tap either way
// from a phone. Yes takes what the card offers, No puts it off or answers in words, More opens the
// context. The buttons are the items' own actions run by Home's handler, so the feed and Home cannot
// disagree on what an answer does. An answered card stays for the day, greyed, saying who, how and when.
//
// `/feed?item=<id>` (the link a Slack message carries, TR-1459) puts that card first and marks it. When it
// no longer waits the page says who answered it and when, from the answers record of any day, or that it
// is gone, and shows the rest of the feed either way.
//
// Live it reads GET /api/home every 15 seconds, as Home does, and the desk's answers of the day. `?sample=busy|empty|down|loading` behind
// the sample flag shows samples, which send nothing.

const MUTED = "text-[13px] text-muted-foreground";
const PAGE = "mx-auto flex w-full max-w-[640px] min-w-0 flex-col gap-6";
const VIA: Record<FeedAnswered["via"], string> = {
  feed: "from the feed", console: "on Home", session: "in the crew lead's session", message: "from a message",
};

/** The record names a desk answer `question:D7` or `permission:D7`; a team cell's card for it is `desk:D7`. */
const deskKey = (id: string) => id.replace(/^(question|permission|desk):/, "desk:");
const sameItem = (a: string, b: string) => deskKey(a) === deskKey(b);

/** One sentence from the cards: how many wait, how many hold the crew up, the longest wait. */
function headline(cards: FeedCard[]): string {
  if (cards.length === 0) return "Nothing waits for you.";
  const n = cards.length === 1 ? "One thing waits" : `${cards.length} things wait`;
  const blocking = cards.filter((c) => c.blocking).length;
  const held = blocking === 0 ? "" : blocking === 1 ? " One holds the crew up." : ` ${blocking} hold the crew up.`;
  const oldest = [...cards].sort((a, b) => (a.since ?? "").localeCompare(b.since ?? ""))[0];
  return `${n} for you.${held} The oldest has waited ${oldest.waited}.`;
}

/** A card's button: a link when the action is a page, else Home's handler runs it. */
function ActionButton({ action, variant, state, onPress, testId }: {
  action: HomeAction; variant: "primary" | "secondary"; state?: RowState; onPress: () => void; testId: string;
}) {
  if (typeof action.href === "string") {
    return <Button asChild variant={variant} className="h-10" data-testid={testId}><Link to={action.href}>{action.label}</Link></Button>;
  }
  return (
    <Button variant={variant} className="h-auto min-h-10 max-w-full whitespace-normal py-2 text-left" busy={state?.busy === action.do}
      disabled={!!state?.busy} onClick={onPress} data-testid={testId} data-user-text={action.do === "choose" ? true : undefined}>
      {state?.copied ? "Copied" : action.label}
    </Button>
  );
}

/** The DOM id of a card, for the link that opens the feed at it. */
const cardDomId = (id: string) => `feed-card-${id.replace(/[^a-zA-Z0-9-]/g, "-")}`;

function Card({ card, top, focused, now, deeper, state, panel, onPress, onDeeper }: {
  card: FeedCard; top: boolean; focused: boolean; now: number; deeper: boolean; state?: RowState; panel?: React.ReactNode;
  onPress: (action: HomeAction, choice: "yes" | "no") => void; onDeeper: () => void;
}) {
  const id = `feed-more-${card.id.replace(/[^a-zA-Z0-9-]/g, "-")}`;
  return (
    <ListRow id={cardDomId(card.id)} className={cn("flex-col items-stretch gap-y-3 scroll-mt-6", focused && "shadow-[inset_3px_0_0_var(--accent)] bg-muted pl-3")}
      data-testid="feed-card" data-kind={card.kind} data-focus={focused ? "1" : undefined} aria-current={focused ? "true" : undefined}>
      <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
        <span className="font-mono text-[12px] uppercase tracking-wide text-muted-foreground">
          {focused && <span className="text-foreground" data-testid="feed-focus-label">From your link · </span>}
          {card.label}
          {card.project && <> · <span data-user-text>{card.project.name}</span></>}
        </span>
        <span className="font-mono text-[12px] text-muted-foreground" data-testid="feed-waited">{card.waited}</span>
      </div>
      <h3 className="m-0 text-[17px] font-semibold leading-snug [overflow-wrap:anywhere]" data-user-text data-testid="feed-ask">
        <Detail text={card.ask} />
      </h3>
      {card.holds_up && (
        <p className={cn(MUTED, "m-0")} data-testid="feed-holds">
          Holds up <span className="text-foreground" data-user-text>{card.holds_up}</span>
        </p>
      )}
      {card.recommendation && (
        <p className="m-0 text-[14px]" data-testid="feed-recommendation">
          {card.from ? `${card.from[0].toUpperCase()}${card.from.slice(1)}` : "The crew"} suggests{" "}
          <span className="font-semibold" data-user-text>{card.recommendation.answer}</span>
          {card.recommendation.why && <span className="text-muted-foreground" data-user-text>. {card.recommendation.why}</span>}
        </p>
      )}
      {/* Home's panel is indented under its row's icon; a card has none */}
      {panel ? <div className="[&>*]:pl-0">{panel}</div> : (
        <div className="flex flex-wrap gap-2">
          {card.yes && (
            <ActionButton action={card.yes} variant={top ? "primary" : "secondary"} state={state}
              onPress={() => onPress(card.yes!, "yes")} testId="feed-yes" />
          )}
          {card.no && (
            <ActionButton action={card.no} variant="secondary" state={state} onPress={() => onPress(card.no!, "no")} testId="feed-no" />
          )}
          <Button variant="quiet" className="h-10" aria-expanded={deeper} aria-controls={id} onClick={onDeeper} data-testid="feed-more">
            {deeper ? "Less" : "More"}
          </Button>
        </div>
      )}
      {state?.error && <p role="alert" className="m-0 text-[13px] text-err" data-testid="feed-error" data-words-exempt>{state.error}</p>}
      <Collapse>
        {deeper && (
          <div id={id} className={cn(MUTED, "flex flex-col gap-2 border-t border-border pt-3")} data-testid="feed-deeper">
            {card.deeper.detail && <p className="m-0 text-foreground" data-user-text><Detail text={card.deeper.detail} /></p>}
            {card.deeper.lines.length > 0 && (
              <ul className="m-0 grid list-none gap-1 p-0">
                {card.deeper.lines.map((l) => <li key={l} data-user-text><Detail text={l} /></li>)}
              </ul>
            )}
            {card.deeper.checks && <p className="m-0" data-testid="feed-checks">{card.deeper.checks}</p>}
            <p className="m-0">
              {card.from && <>From {card.from}. </>}
              {card.since && <>Waiting since <span className="font-mono" title={sinceWords(Date.parse(card.since), now)}>{formatStampUTC(card.since)}</span>.</>}
            </p>
            {card.deeper.href && (
              <Link to={card.deeper.href} className="text-foreground underline decoration-border underline-offset-[3px]">Open it in full</Link>
            )}
          </div>
        )}
      </Collapse>
    </ListRow>
  );
}

const CHOICE = (a: FeedAnswered) => (a.choice === "yes" ? "Yes" : a.choice === "no" ? "No" : "Answered");

/** What the page says when the item a link named no longer waits: who answered it and when, or that it is gone. */
function FocusNote({ answer, now, more }: { answer: FeedAnswered | null; now: number; more: boolean }) {
  const rest = more ? " Here is what still waits." : "";
  if (!answer) {
    return <Banner data-testid="feed-focus-gone">The item that link named no longer waits for you.{rest}</Banner>;
  }
  return (
    <Banner data-testid="feed-focus-answered" data-choice={answer.choice ?? "answered"}>
      <span>
        Already answered: <span data-user-text>{answer.ask}</span>{/[.?!]$/.test(answer.ask) ? " " : ". "}
        {/* an option or words alone when it was neither yes nor no, as the record says it */}
        {answer.choice && <>{CHOICE(answer)}{answer.said && answer.said !== CHOICE(answer) ? ", " : ""}</>}
        {answer.said && answer.said !== CHOICE(answer) && <span data-user-text>{answer.said}</span>}
        , by {answer.by}, {VIA[answer.via]}, {sinceWords(Date.parse(answer.at), now)}.{rest}
      </span>
    </Banner>
  );
}

function AnsweredRow({ a, now }: { a: FeedAnswered; now: number }) {
  return (
    <ListRow className="flex-col items-stretch gap-y-1 text-muted-foreground" data-testid="feed-answered" data-choice={a.choice ?? "answered"}>
      <span className="text-[15px] [overflow-wrap:anywhere]" data-user-text><Detail text={a.ask} /></span>
      <span className="text-[13px]">
        <StatusLabel tone="done" className="mr-2">{CHOICE(a)}</StatusLabel>
        {a.said && <><span data-user-text>{a.said}</span>, </>}by {a.by}, {VIA[a.via]}, {sinceWords(Date.parse(a.at), now)}
      </span>
    </ListRow>
  );
}

/** The feed for one FeedData. `live` sends the answers to the cell; `reload` reads the home again. */
function FeedPage({ data, live, stale, reload }: { data: FeedData; live: boolean; stale?: boolean; reload?: () => void }) {
  const now = live ? Date.now() : Date.parse(data.now);
  // answered on this page, shown until the cell's record of the same item replaces it
  const [mine, setMine] = React.useState<FeedAnswered[]>([]);
  const answered = [...data.answered, ...mine.filter((m) => !data.answered.some((a) => sameItem(a.id, m.id)))]
    .sort((a, b) => b.at.localeCompare(a.at));
  // ids answered here, hidden until the daemon's cached answer has passed (live) or for good (a sample)
  const [hidden, setHidden] = React.useState<Map<string, number>>(new Map());
  const [deeper, setDeeper] = React.useState<Set<string>>(new Set());
  const pressed = React.useRef(new Map<string, Omit<FeedAnswered, "by" | "at" | "via">>());
  const timer = React.useRef<ReturnType<typeof setTimeout>>();
  React.useEffect(() => () => clearTimeout(timer.current), []);

  const changed = (done?: Done) => {
    const id = done?.item;
    const p = id ? pressed.current.get(id) : undefined;
    if (id && p) {
      pressed.current.delete(id);
      setMine((list) => [{ ...p, by: "you", at: live ? new Date().toISOString() : data.now, via: "feed" }, ...list]);
      setHidden((h) => new Map(h).set(id, Date.now() + CACHE_MS));
    }
    if (!live || !reload) return;
    reload();
    clearTimeout(timer.current);
    timer.current = setTimeout(reload, CACHE_MS);
  };
  const { rows, onAction, dialogs, panels } = useHomeActions(live, changed);

  const focus = data.focus?.id;
  const isFocus = (c: FeedCard) => (focus && sameItem(c.id, focus) ? 1 : 0);
  const cards = data.items
    .filter((i) => { const until = hidden.get(i.id); return until === undefined || (live && Date.now() >= until); })
    .map(toFeedCard)
    .sort((a, b) => isFocus(b) - isFocus(a) || feedOrder(a, b));
  const focusCard = focus ? cards.find((c) => isFocus(c)) : undefined;
  const focusWaits = !!focusCard;
  // answered here or today first (the newest), then the record of any day the link's read found
  const focusAnswer = !focus || focusWaits ? null
    : answered.find((a) => sameItem(a.id, focus)) ?? data.focus?.answer ?? null;

  // the linked card is first; bring it into view once, when it is there
  const scrolled = React.useRef(false);
  React.useEffect(() => {
    if (!focusWaits || scrolled.current) return;
    scrolled.current = true;
    document.getElementById(cardDomId(focusCard!.id))?.scrollIntoView?.({ block: "nearest" });
  }, [focusWaits, focusCard]);

  const press = (card: FeedCard, action: HomeAction, choice: "yes" | "no") => {
    // an answer given in the panel is neither yes nor no; which option or words it was, the panel knows
    const panel = action.do === "answer";
    pressed.current.set(card.id, {
      id: card.id, ask: card.ask, choice: panel ? null : choice,
      said: panel ? (Array.isArray(action.options) && action.options.length ? "Picked another answer" : "Answered in words") : action.label,
    });
    onAction(action, card.item);
  };
  const toggle = (id: string) => setDeeper((s) => {
    const next = new Set(s);
    if (next.has(id)) next.delete(id); else next.add(id);
    return next;
  });

  return (
    <div className={PAGE} data-testid="factory-feed" data-live={live ? "1" : undefined}>
      <div>
        <p className="m-0 mb-1 font-mono text-[12px] uppercase tracking-wide text-muted-foreground">{live ? "Feed" : "Feed · sample"}</p>
        <h1 className="m-0 mb-1.5 text-[22px] md:text-[28px]">Waiting for you</h1>
        <p className="m-0 text-[15px] text-muted-foreground" data-testid="feed-headline">{headline(cards)}</p>
      </div>
      {stale && <Banner tone="warn" data-testid="feed-stale">Could not refresh. Showing what we had.</Banner>}
      {focus && !focusWaits && <FocusNote answer={focusAnswer} now={now} more={cards.length > 0} />}
      {!data.crew.running && (
        <Banner tone="warn" data-testid="feed-crew-down">
          The crew is not running{data.crew.since ? `, last heard ${sinceWords(Date.parse(data.crew.since), now)}` : ""}. Nothing new reaches this feed until it is back.
        </Banner>
      )}
      <ListSection paper title="To answer" aside={cards.length ? `${cards.length}` : undefined}>
        {cards.length === 0 ? (
          <EmptyState data-testid="feed-empty">
            {data.crew.running ? `Nothing needs you right now. ${data.crew.summary}` : "Nothing here, because nothing is being built."}
          </EmptyState>
        ) : cards.map((c, i) => (
          <Card key={c.id} card={c} top={i === 0} focused={isFocus(c) === 1} now={now} deeper={deeper.has(c.id)} state={rows[c.id]} panel={panels[c.id]}
            onPress={(a, choice) => press(c, a, choice)} onDeeper={() => toggle(c.id)} />
        ))}
      </ListSection>
      {answered.length > 0 && (
        <ListSection paper title="Answered today" aside={`${answered.length}`}>
          {answered.map((a) => <AnsweredRow key={`${a.id}-${a.at}`} a={a} now={now} />)}
        </ListSection>
      )}
      {dialogs}
      <Toaster />
    </div>
  );
}

/** The home and the desk's answers of the day, read into the feed's shape. */
function fromHome(h: HomeData, answered: FeedAnswered[], focus?: FeedData["focus"]): FeedData {
  return {
    focus,
    now: h.now,
    crew: { running: h.crew.running, since: h.crew.heard_at, summary: h.crew.summary },
    items: h.waiting_for_you,
    answered,
  };
}

function Loading() {
  return <div className={PAGE} data-testid="feed-loading"><SkeletonRows /></div>;
}

/** The item a link opened the feed at, `?item=<id>`, or null. */
function useFocusItem(): string | null {
  const { search } = useLocation();
  return new URLSearchParams(search).get("item")?.trim() || null;
}

function LiveFeed({ item }: { item: string | null }) {
  // the answers sent from here say so (TR-1451)
  React.useEffect(() => {
    setSurface("feed");
    return () => setSurface(null);
  }, []);
  // the answers are a nicety: when they fail to load, the cards still show
  // with a link's item, its answer of any day too, read with the rest so the page never says "gone" first
  const { data, error, reload } = usePagePolling(
    () => Promise.all([getHome(), getAnsweredToday().catch(() => []),
                       item ? getAnswerTo(item).catch(() => null) : Promise.resolve(null)]), POLL_MS);
  const feed = React.useMemo(
    () => (data ? fromHome(data[0], data[1], item ? { id: item, answer: data[2] } : undefined) : null), [data, item]);
  if (!feed) {
    return error ? (
      <div className={PAGE}>
        <EmptyState title="Could not reach the cell" data-testid="feed-error-page"
          action={<Button variant="secondary" onClick={reload}>Try again</Button>}>
          The feed reads what waits for you from the cell, and it did not answer.
        </EmptyState>
      </div>
    ) : <Loading />;
  }
  return <FeedPage data={feed} live stale={!!error} reload={reload} />;
}

export default function FactoryFeed() {
  const sample = useSample();
  const item = useFocusItem();
  if (sample === "loading") return <Loading />;
  const data = sample ? FEED_SAMPLES[sample] : undefined;
  return data
    ? <FeedPage key={`${sample}-${item}`} data={item ? { ...data, focus: { id: item, answer: null } } : data} live={false} />
    : <LiveFeed key={item ?? ""} item={item} />;
}
