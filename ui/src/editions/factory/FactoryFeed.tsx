import * as React from "react";
import { Link } from "react-router-dom";

import {
  Banner, Button, Collapse, EmptyState, ListRow, ListSection, SkeletonRows, StatusLabel, Textarea, Toaster, useToast,
} from "../../components/edition";
import { formatStampUTC, sinceWords } from "../../lib/time";
import { cn } from "../../lib/utils";
import { useSample } from "../sample";
import { Detail } from "./HomeDetail";
import type { FeedAnswered, FeedCard, FeedChoice, FeedData, FeedVia } from "./feed.types";
import { feedOrder, toFeedCard } from "./feedCard";
import { FEED_SAMPLES } from "./feedSamples";

// The feed (P-TR-273, M2 mock): every decision the crew waits on, one card each, cleared with one tap
// either way from a phone. Yes takes what the card offers; No asks for a reason or an option when the
// crew needs one; More opens the context. An answered card stays for the day, greyed, saying who
// answered, when and from where.
//
// A mock: it shows samples only (`?sample=busy|empty|down|loading` behind the sample flag) and sends
// nothing. On a live cell it says so and points at Home. Wiring it to /api/home is M3, after the mock
// is approved.

const MUTED = "text-[13px] text-muted-foreground";
const NOTE = "Sample only: nothing was sent to the crew.";
const VIA: Record<FeedVia, string> = { feed: "from the feed", console: "on Home", session: "in the crew lead's session", message: "from a message" };

function clock(iso: string, now: number): string {
  return sinceWords(Date.parse(iso), now);
}

/** One sentence from the cards: how many wait, how many hold the crew up, the longest wait. */
function headline(cards: FeedCard[]): string {
  if (cards.length === 0) return "Nothing waits for you.";
  const n = cards.length === 1 ? "One thing waits" : `${cards.length} things wait`;
  const blocking = cards.filter((c) => c.blocking).length;
  const held = blocking === 0 ? "" : blocking === 1 ? " One holds the crew up." : ` ${blocking} hold the crew up.`;
  const oldest = [...cards].sort((a, b) => (a.since ?? "").localeCompare(b.since ?? ""))[0];
  return `${n} for you.${held} The oldest has waited ${oldest.waited}.`;
}

function NoPanel({ card, onSend, onCancel }: {
  card: FeedCard; onSend: (words: string) => void; onCancel: () => void;
}) {
  const [text, setText] = React.useState("");
  const words = text.trim();
  const options = card.no.options ?? [];
  return (
    <div className="flex flex-col gap-3 border-t border-border pt-3" data-testid="feed-no-panel">
      {options.length > 0 && (
        <div className="flex flex-wrap gap-2" role="group" aria-label={`Answers to ${card.ask}`}>
          {options.map((o) => (
            <Button key={o} variant="secondary" className="h-auto min-h-10 whitespace-normal py-2 text-left" onClick={() => onSend(o)} data-user-text>
              {o}
            </Button>
          ))}
        </div>
      )}
      {card.no.words !== "none" && (
        <Textarea label={card.no.words === "required" ? "What should change" : "Your reason, if you want to give one"} rows={3}
          value={text} onChange={(e) => setText(e.target.value)} data-testid="feed-no-words" />
      )}
      <div className="flex flex-wrap gap-2">
        <Button variant="secondary" className="h-10" disabled={card.no.words === "required" && !words}
          onClick={() => onSend(words)} data-testid="feed-no-send">
          {card.no.label}
        </Button>
        <Button variant="quiet" className="h-10" onClick={onCancel}>Cancel</Button>
      </div>
    </div>
  );
}

function Card({ card, top, now, deeper, noOpen, onAnswer, onDeeper, onNo }: {
  card: FeedCard; top: boolean; now: number; deeper: boolean; noOpen: boolean;
  onAnswer: (choice: FeedChoice, words: string | null) => void; onDeeper: () => void; onNo: (open: boolean) => void;
}) {
  const id = `feed-more-${card.id.replace(/[^a-zA-Z0-9-]/g, "-")}`;
  // No in one tap when it takes no words and offers nothing to pick: Later.
  const quickNo = card.no.words === "none" && !(card.no.options?.length);
  return (
    <ListRow className="flex-col items-stretch gap-y-3" data-testid="feed-card" data-kind={card.kind}>
      <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
        <span className="font-mono text-[12px] uppercase tracking-wide text-muted-foreground">
          {card.label}
          {card.project && <> · <span data-user-text>{card.project.name}</span></>}
        </span>
        <span className="font-mono text-[12px] text-muted-foreground" data-testid="feed-waited">{card.waited}</span>
      </div>
      <h3 className="m-0 text-[17px] font-semibold leading-snug" data-user-text data-testid="feed-ask">{card.ask}</h3>
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
      {!noOpen && (
        <div className="flex flex-wrap gap-2">
          {card.yes && (
            <Button variant={top ? "primary" : "secondary"} className="h-10" onClick={() => onAnswer("yes", null)} data-testid="feed-yes">
              {card.yes.label}
            </Button>
          )}
          <Button variant="secondary" className="h-10" data-testid="feed-no"
            onClick={() => (quickNo ? onAnswer("no", null) : onNo(true))}>
            {card.no.label}
          </Button>
          <Button variant="quiet" className="h-10" aria-expanded={deeper} aria-controls={id} onClick={onDeeper} data-testid="feed-more">
            {deeper ? "Less" : "More"}
          </Button>
        </div>
      )}
      <Collapse>
        {noOpen && <NoPanel card={card} onSend={(w) => onAnswer("no", w || null)} onCancel={() => onNo(false)} />}
      </Collapse>
      <Collapse>
        {deeper && (
          <div id={id} className={cn(MUTED, "flex flex-col gap-2 border-t border-border pt-3")} data-testid="feed-deeper">
            <p className="m-0 text-foreground" data-user-text><Detail text={card.deeper.detail} /></p>
            {card.deeper.lines.length > 0 && (
              <ul className="m-0 grid list-none gap-1 p-0">
                {card.deeper.lines.map((l) => <li key={l} data-user-text><Detail text={l} /></li>)}
              </ul>
            )}
            {card.deeper.checks && <p className="m-0" data-testid="feed-checks">{card.deeper.checks}</p>}
            {card.yes && <p className="m-0">{card.yes.label}: {card.yes.does}</p>}
            <p className="m-0">
              {card.from && <>From {card.from}. </>}
              {card.since && <>Waiting since <span className="font-mono" title={clock(card.since, now)}>{formatStampUTC(card.since)}</span>.</>}
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

function AnsweredRow({ a, now }: { a: FeedAnswered; now: number }) {
  // a picked option is the answer itself; written words follow what No was called
  const picked = a.words != null && (a.card.no.options ?? []).includes(a.words);
  const said = a.choice === "yes" ? (a.card.yes?.label ?? "Yes")
    : picked ? a.words : a.words ? `${a.card.no.label}: ${a.words}` : a.card.no.label;
  return (
    <ListRow className="flex-col items-stretch gap-y-1 text-muted-foreground" data-testid="feed-answered" data-choice={a.choice}>
      <span className="text-[15px]" data-user-text>{a.card.ask}</span>
      <span className="text-[13px]">
        <StatusLabel tone="done" className="mr-2">{a.choice === "yes" ? "Yes" : "No"}</StatusLabel>
        <span data-user-text>{said}</span>, by {a.by}, {VIA[a.via]}, {clock(a.at, now)}
      </span>
    </ListRow>
  );
}

export function FeedPage({ data }: { data: FeedData }) {
  const { toast } = useToast();
  const now = Date.parse(data.now);
  const [answered, setAnswered] = React.useState<FeedAnswered[]>(data.answered);
  const [deeper, setDeeper] = React.useState<Set<string>>(new Set());
  const [noOpen, setNoOpen] = React.useState<string | null>(null);
  const done = new Set(answered.map((a) => a.card.id));
  const cards = data.items.map((i) => toFeedCard(i, data.extras[i.id])).filter((c) => !done.has(c.id)).sort(feedOrder);

  const answer = (card: FeedCard, choice: FeedChoice, words: string | null) => {
    setAnswered((list) => [{ card, choice, words, by: "you", at: data.now, via: "feed" }, ...list]);
    setNoOpen(null);
    toast(NOTE);
  };
  const toggle = (id: string) => setDeeper((s) => {
    const next = new Set(s);
    if (next.has(id)) next.delete(id); else next.add(id);
    return next;
  });

  return (
    <div className="mx-auto flex w-full max-w-[640px] min-w-0 flex-col gap-6" data-testid="factory-feed">
      <div>
        <p className="m-0 mb-1 font-mono text-[12px] uppercase tracking-wide text-muted-foreground">Feed · sample</p>
        <h1 className="m-0 mb-1.5 text-[22px] md:text-[28px]">Waiting for you</h1>
        <p className="m-0 text-[15px] text-muted-foreground" data-testid="feed-headline">{headline(cards)}</p>
      </div>
      {!data.crew.running && (
        <Banner tone="warn" data-testid="feed-crew-down">
          The crew is not running{data.crew.since ? ` since ${clock(data.crew.since, now)}` : ""}. Nothing new reaches this feed until it is back.
        </Banner>
      )}
      <ListSection paper title="To answer" aside={cards.length ? `${cards.length}` : undefined}>
        {cards.length === 0 ? (
          <EmptyState data-testid="feed-empty">
            {data.crew.running ? `Nothing needs you right now. ${data.crew.summary}` : "Nothing here, because nothing is being built."}
          </EmptyState>
        ) : cards.map((c, i) => (
          <Card key={c.id} card={c} top={i === 0} now={now} deeper={deeper.has(c.id)} noOpen={noOpen === c.id}
            onAnswer={(choice, words) => answer(c, choice, words)} onDeeper={() => toggle(c.id)}
            onNo={(open) => setNoOpen(open ? c.id : null)} />
        ))}
      </ListSection>
      {answered.length > 0 && (
        <ListSection paper title="Answered today" aside={`${answered.length}`}>
          {answered.map((a) => <AnsweredRow key={a.card.id} a={a} now={now} />)}
        </ListSection>
      )}
      <Toaster />
    </div>
  );
}

function NotYet() {
  return (
    <div className="mx-auto w-full max-w-[640px]" data-testid="factory-feed-mock">
      <EmptyState title="The feed is a mock for now" action={<Button asChild variant="secondary"><Link to="/">Open Home</Link></Button>}>
        It shows sample decisions only, while its shape is agreed. Answer what waits for you on Home.
      </EmptyState>
    </div>
  );
}

export default function FactoryFeed() {
  const sample = useSample();
  if (sample === "loading") return <div className="mx-auto w-full max-w-[640px]"><SkeletonRows /></div>;
  const data = sample ? FEED_SAMPLES[sample] : undefined;
  return data ? <FeedPage key={sample} data={data} /> : <NotYet />;
}
