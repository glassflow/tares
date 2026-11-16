import * as React from "react";
import { Link } from "react-router-dom";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";

import { ApiError } from "../../api";
import { Banner, Button, EmptyState, Panel, Skeleton } from "../../components/edition";
import { Input as TextInput } from "../../components/ui/input";
import { cn } from "../../lib/utils";
import { askBrain, askBrainRepos, type AskBrainData, type BrainAnswer } from "./BrainAsk.api";
import {
  answeredWords, askErrorWords, indexingNames, indexingTail, MAX_QUESTION, nameSeparator, NOT_SET_UP_WORDS, NOTHING_TO_ASK_TITLE,
  CONNECT_FIRST_WORDS, questionLength, READ_FAILED_WORDS, readyRepos, reposOnBrain, sourceHref, sourceLabel, sourceText, subtitle,
  TOO_LONG_WORDS, WAITING_DEEP_WORDS, WAITING_WORDS,
} from "./brainAskText";

// Ask (TR-1206), a page of the Second Brain: one question, a quick answer with its sources, and a link to look
// deeper. The page reads the repos once (GET /api/brain) for "N repos" and the repos still indexing, then asks
// with POST /api/brain/ask {question, deep}. Anyone who can sign in can ask; asking needs no admin. The answer is
// the brain's own text (markdown, never raw HTML) and is exempt from the words rule; the page's own words are not.
// Look deeper asks the question the answer belongs to (not what is typed in the box now) and replaces the answer.

const MUTED = "m-0 text-[13px] text-muted-foreground";
const LINK = "text-[color:var(--accent-deep)]! underline!";

// The answer is a few paragraphs: block markdown, no pictures (they would call out), links open in a new tab.
// Code and tables scroll inside their block instead of widening the page.
const MARKDOWN: Components = {
  p: ({ children }) => <p className="m-0! mb-2.5! last:mb-0!">{children}</p>,
  img: ({ alt }) => <>{alt}</>,
  a: ({ href, children }) => <a href={href} target="_blank" rel="noreferrer noopener" className="underline!">{children}</a>,
  code: ({ children }) => <code className="bg-muted px-[5px] py-px font-mono text-[12.5px]">{children}</code>,
  pre: ({ children }) => (
    <pre className="my-2! max-w-full overflow-x-auto border border-border bg-muted p-2.5 text-[12.5px] [&_code]:bg-transparent [&_code]:p-0">{children}</pre>
  ),
  ul: ({ children }) => <ul className="m-0! mb-2.5! list-disc! pl-5!">{children}</ul>,
  ol: ({ children }) => <ol className="m-0! mb-2.5! list-decimal! pl-5!">{children}</ol>,
  h1: ({ children }) => <p className="m-0! mb-2! text-base font-bold">{children}</p>,
  h2: ({ children }) => <p className="m-0! mb-2! text-base font-bold">{children}</p>,
  h3: ({ children }) => <p className="m-0! mb-2! text-[15px] font-semibold">{children}</p>,
  h4: ({ children }) => <p className="m-0! mb-2! text-[15px] font-semibold">{children}</p>,
  blockquote: ({ children }) => <blockquote className="m-0! mb-2.5! border-l-2 border-border pl-3 text-muted-foreground">{children}</blockquote>,
  table: ({ children }) => <div className="my-2 max-w-full overflow-x-auto"><table className="border-collapse text-[13px]">{children}</table></div>,
  th: ({ children }) => <th className="border border-border px-2 py-1 text-left">{children}</th>,
  td: ({ children }) => <td className="border border-border px-2 py-1">{children}</td>,
};

function Head({ n, children }: { n?: number; children?: React.ReactNode }) {
  return (
    <div className="grid max-w-[900px] gap-5 md:gap-6" data-testid="brain-ask">
      <div>
        <h1 className="m-0! mb-1.5! text-[22px]! md:text-[28px]!">Ask</h1>
        {n === undefined
          ? <Skeleton width={320} height={12} className="max-w-full" />
          : <p className="m-0! max-w-[640px] text-[15px] text-muted-foreground" data-testid="brain-ask-subtitle">{subtitle(n)}</p>}
      </div>
      {children}
    </div>
  );
}

function Waiting({ deep }: { deep: boolean }) {
  return (
    <Panel className="[&>div]:px-5" aria-label="Answer" data-testid="brain-ask-waiting">
      <div role="status" aria-busy="true" className="grid gap-3">
        <p className="m-0 text-sm" data-testid="brain-ask-waiting-words">{deep ? WAITING_DEEP_WORDS : WAITING_WORDS}</p>
        <Skeleton width="92%" height={10} /><Skeleton width="78%" height={10} /><Skeleton width="55%" height={10} />
      </div>
    </Panel>
  );
}

/** What a finished ask holds: the answer, the question it answers, and how long it took when the cell did not say. */
interface Shown { question: string; deep: boolean; data: BrainAnswer; tookMs: number }

function Answer({ shown, onDeeper }: { shown: Shown; onDeeper: () => void }) {
  const { data } = shown;
  const cites = data.citations ?? [];
  return (
    <Panel className="[&>div]:px-5" aria-label="Answer" data-testid="brain-ask-answer" data-deep={shown.deep ? "true" : "false"}>
      <div data-words-exempt data-testid="brain-ask-text" className="text-sm">
        <ReactMarkdown remarkPlugins={[remarkGfm]} components={MARKDOWN}>{data.answer}</ReactMarkdown>
      </div>
      {cites.length > 0 && (
        <ol aria-label="Where this came from" className="m-0! mt-3.5! flex list-none! flex-col gap-1.5 border-t border-dotted border-border p-0! pt-2.5! text-[13px]" data-testid="brain-ask-sources">
          {cites.map((c, i) => {
            const href = sourceHref(c, data.repos ?? []);
            return (
              <li key={i} className="flex min-w-0 flex-wrap gap-x-2.5" data-testid="brain-ask-source">
                <span className="font-mono text-muted-foreground" data-testid="brain-ask-source-label">{sourceLabel(c, i)}</span>
                {href
                  ? <a href={href} target="_blank" rel="noreferrer noopener" className="min-w-0 font-mono text-[12.5px] [overflow-wrap:anywhere]" data-testid="brain-ask-source-link" data-user-text>{sourceText(c, data.repos ?? [])}</a>
                  : <span className="min-w-0 font-mono text-[12.5px] [overflow-wrap:anywhere]" data-testid="brain-ask-source-link" data-user-text>{sourceText(c, data.repos ?? [])}</span>}
              </li>
            );
          })}
        </ol>
      )}
      <div className="mt-3.5 flex flex-wrap items-baseline gap-x-4 gap-y-1 text-xs text-muted-foreground">
        <span data-testid="brain-ask-took">{answeredWords(data.duration_ms, shown.tookMs)}</span>
        {!shown.deep && (
          <button type="button" onClick={onDeeper} data-testid="brain-ask-deeper"
                  className="cursor-pointer border-0! bg-transparent! p-0! text-[13px]! font-normal! text-foreground! underline!">
            Look deeper
          </button>
        )}
      </div>
    </Panel>
  );
}

function IndexingLine({ names }: { names: string[] }) {
  if (names.length === 0) return null;
  return (
    <p className={cn(MUTED, "[overflow-wrap:anywhere]")} data-testid="brain-ask-indexing">
      {names.map((n, i) => (
        <React.Fragment key={n}><span className="font-mono" data-user-text>{n}</span>{nameSeparator(i, names.length)}</React.Fragment>
      ))}
      {indexingTail(names.length)}
    </p>
  );
}

function Loading() {
  return (
    <div role="status" aria-busy="true" aria-label="Loading" className="grid gap-3 border border-border bg-card p-4" data-testid="brain-ask-loading">
      <Skeleton width="60%" height={14} /><Skeleton width="85%" height={10} /><Skeleton width="45%" height={10} />
    </div>
  );
}

type Load =
  | { state: "loading" }
  | { state: "error" }
  | { state: "ok"; data: AskBrainData };

function Asker({ data }: { data: AskBrainData }) {
  const [question, setQuestion] = React.useState("");
  // The answer on screen, while a call runs (`pending`: which kind) and the words of the last failure.
  const [shown, setShown] = React.useState<Shown | null>(null);
  const [pending, setPending] = React.useState<null | "quick" | "deep">(null);
  const [failure, setFailure] = React.useState("");
  // The cell said no repo is ready when the page thought one was (it was removed meanwhile).
  const [none, setNone] = React.useState(false);
  const latest = React.useRef(0);
  const alive = React.useRef(true);
  React.useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);

  const ask = async (q: string, deep: boolean) => {
    const id = ++latest.current;
    const started = Date.now();
    setPending(deep ? "deep" : "quick"); setFailure(""); setNone(false);
    if (!deep) setShown(null);
    try {
      const got = await askBrain(q, deep);
      if (!alive.current || id !== latest.current) return;
      if (got.ready === false) { setShown(null); setNone(true); return; }
      setShown({ question: q, deep, data: got, tookMs: Date.now() - started });
    } catch (e) {
      if (!alive.current || id !== latest.current) return;
      setFailure(askErrorWords(e instanceof ApiError ? { message: e.message, status: e.status } : undefined, deep));
    } finally {
      if (alive.current && id === latest.current) setPending(null);
    }
  };

  const length = questionLength(question);
  const tooLong = length > MAX_QUESTION;
  const empty = length === 0;
  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    if (pending || empty || tooLong) return;
    void ask(question.trim(), false);
  };

  const ready = readyRepos(data.repos);
  const names = indexingNames(data.repos);
  const n = reposOnBrain(data.repos).length;
  if (ready.length === 0 || none) {
    return (
      <Head n={n}>
        <Panel><EmptyState title={NOTHING_TO_ASK_TITLE} data-testid="brain-ask-empty"
                           action={<Button asChild variant="secondary"><Link to="/brain" data-testid="brain-ask-repos">Repos</Link></Button>}>
          {CONNECT_FIRST_WORDS}
        </EmptyState></Panel>
        <IndexingLine names={names} />
      </Head>
    );
  }
  return (
    <Head n={n}>
      <form onSubmit={submit} className="flex flex-wrap gap-2" data-testid="brain-ask-form">
        <div className="min-w-0 flex-[1_1_360px]">
          <label htmlFor="brain-ask-question" className="sr-only">Question</label>
          <TextInput id="brain-ask-question" value={question} autoComplete="off" data-testid="brain-ask-question"
                     className="h-10 border-foreground! bg-[var(--input-bg)]!" aria-invalid={tooLong ? true : undefined}
                     aria-describedby={tooLong ? "brain-ask-too-long" : undefined}
                     onChange={(e) => setQuestion(e.target.value)} />
        </div>
        <Button type="submit" busy={pending === "quick"} disabled={pending !== null || empty || tooLong} className="h-10" data-testid="brain-ask-submit">Ask</Button>
      </form>
      {tooLong && <p id="brain-ask-too-long" role="alert" className="m-0! -mt-3 text-[13px] text-[color:var(--err)]" data-testid="brain-ask-too-long">{TOO_LONG_WORDS}</p>}
      {failure && <Banner tone="error" data-testid="brain-ask-error"><span data-user-text>{failure}</span></Banner>}
      {pending ? <Waiting deep={pending === "deep"} /> : shown && <Answer shown={shown} onDeeper={() => void ask(shown.question, true)} />}
      <IndexingLine names={names} />
    </Head>
  );
}

export default function BrainAsk() {
  const [load, setLoad] = React.useState<Load>({ state: "loading" });
  const alive = React.useRef(true);
  const read = React.useCallback(async () => {
    setLoad({ state: "loading" });
    try {
      const data = await askBrainRepos();
      if (alive.current) setLoad({ state: "ok", data });
    } catch {
      if (alive.current) setLoad({ state: "error" });
    }
  }, []);
  React.useEffect(() => { alive.current = true; void read(); return () => { alive.current = false; }; }, [read]);

  if (load.state === "loading") return <Head><Loading /></Head>;
  if (load.state === "error") {
    return (
      <Head>
        <Banner tone="error" action={<Button variant="secondary" onClick={() => void read()} data-testid="brain-ask-retry">Retry</Button>} data-testid="brain-ask-read-error">
          <b>Ask did not load.</b> <span className="text-foreground">{READ_FAILED_WORDS}</span>
        </Banner>
      </Head>
    );
  }
  if (!load.data.configured) {
    return <Head><Panel><EmptyState title="Not set up" data-testid="brain-ask-not-set-up">{NOT_SET_UP_WORDS}</EmptyState></Panel></Head>;
  }
  return <Asker data={load.data} />;
}
