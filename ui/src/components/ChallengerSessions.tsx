import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import { api } from "../api";
import { TimeAgo } from "./bits";
import type { ChallengerSession, TimelineEventRow, ProjectSummary } from "../types";

// The challenger workflow's own panels on the project page. Sessions is a list that opens in
// place: the selected row is marked and its detail (the summary, that session's memory proposals,
// the Claude/Codex exchange) sits directly under it, so with several sessions there is never a
// question of which one is open. The open session lives in the URL (?session=) so a refresh or a
// shared link lands on it. Proposals are decided there and nowhere else; a row whose session
// still has proposals waiting says so, so you know where to click.
//
// A proposal is text inside a finding until the user accepts it; accept is the only thing that
// writes memory, so the plugin only ever hands Claude what a person chose to keep.

type Run = NonNullable<ProjectSummary["runs"]>[number];
type Decision = "accepted" | "rejected";

// Codex's verdict word stays in the data (the `verdict` label); on screen we say what happened.
// "FAIL" elsewhere in the console means a run or delivery that did not work; here the review
// worked and found things.
function verdictClass(v: string | undefined | null) {
  return v === "PASS" ? "ok" : v === "FAIL" ? "paused" : "blanked";
}
function verdictText(v: string | undefined | null, findings?: string | number, blocking?: string | number) {
  if (v === "PASS") return "no findings";
  if (v === "FAIL") {
    const n = Number(findings), b = Number(blocking);
    if (!Number.isFinite(n) || n <= 0) return "findings";
    return `${n} finding${n === 1 ? "" : "s"}` + (Number.isFinite(b) && b > 0 ? `, ${b} blocking` : "");
  }
  return "no verdict";
}

function Verdict({ v, findings, blocking, compact, what }: {
  v: string | undefined | null; findings?: string | number; blocking?: string | number;
  compact?: boolean;   // the collapsed row: just the finding count, wording on hover
  what?: string;
}) {
  if (!v) return <span className="dim">{compact ? "none" : "not reviewed"}</span>;
  const text = verdictText(v, findings, blocking);
  const title = `${what ? what + ": " : ""}${text} (Codex verdict ${v})`;
  if (compact) {
    const n = Number(findings);
    const label = v === "PASS" ? "Pass" : v === "FAIL" ? `Findings (${Number.isFinite(n) ? n : "?"})` : "No verdict";
    return <span className={`badge ${verdictClass(v)}`} title={title}>{label}</span>;
  }
  return <span className={`badge ${verdictClass(v)}`} title={title}>{text}</span>;
}

// One line for a session's commits: how many, how many passed, how many findings and how many of
// those block. The per-commit verdicts live in the detail.
function commitRollup(s: ChallengerSession) {
  const n = s.commits.length;
  if (!n) return { text: "none", dim: true, blocking: 0 };
  const pass = s.commits.filter((c) => c.verdict === "PASS").length;
  const findings = s.commits.reduce((a, c) => a + (Number(c.findings) || 0), 0);
  const blocking = s.commits.reduce((a, c) => a + (Number(c.blocking) || 0), 0);
  const parts = [`${n} commit${n === 1 ? "" : "s"}`];
  if (pass) parts.push(`${pass} pass`);
  if (findings) parts.push(`${findings} finding${findings === 1 ? "" : "s"}` + (blocking ? `, ${blocking} blocking` : ""));
  return { text: parts.join(" · "), dim: false, blocking };
}

function useDecide() {
  const [busy, setBusy] = useState<string>();
  const [err, setErr] = useState<string>();
  // decided locally this render cycle, until the next poll brings the state back from Tares
  const [local, setLocal] = useState<Record<string, Decision>>({});
  const decide = async (runId: string, i: number, repo: string, text: string, d: Decision) => {
    const k = `${runId}:${i}`;
    setBusy(k); setErr(undefined);
    try {
      await api.remember({ key: repo, content: text, memory_type: d === "accepted" ? "decision" : "rejected_proposal" });
      setLocal((cur) => ({ ...cur, [k]: d }));
    } catch (e) { setErr(String((e as Error).message ?? e)); }
    setBusy(undefined);
  };
  return { busy, err, local, decide };
}

function ProposalRows({ run, repo }: { run: Run; repo: string }) {
  const { busy, err, local, decide } = useDecide();
  const items = (run.proposals ?? []).map((text, i) => ({
    i, text, decision: local[`${run.id}:${i}`] ?? run.decisions?.[String(i)],
  }));
  if (!items.length) return null;
  return (
    <>
      {err && <div className="alert error">{err}</div>}
      <table style={{ marginTop: 8 }}>
        <tbody>
          {items.map((x) => (
            <tr key={x.i}>
              <td style={{ whiteSpace: "pre-wrap" }}>{x.text}</td>
              <td style={{ width: 1, whiteSpace: "nowrap" }}>
                {x.decision ? (
                  x.decision === "accepted" ? <span className="badge ok">accepted</span> : <span className="help">rejected</span>
                ) : (
                  <div className="btnrow" style={{ justifyContent: "flex-end", flexWrap: "nowrap" }}>
                    <button className="primary" disabled={!repo || busy === `${run.id}:${x.i}`}
                            title={repo ? "store as a decision on the memory source, keyed by the repo" : "the session's repo is unknown, so accept has no key"}
                            onClick={() => decide(run.id!, x.i, repo, x.text, "accepted")}>
                      {busy === `${run.id}:${x.i}` ? "saving…" : "Accept"}
                    </button>
                    <button disabled={!repo || busy === `${run.id}:${x.i}`} onClick={() => decide(run.id!, x.i, repo, x.text, "rejected")}>Reject</button>
                  </div>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  );
}

export function SessionsPanel({ sessions, runs, project, onSummarize, busy, message }: {
  sessions: ChallengerSession[]; runs?: Run[]; project: string;   // the project whose sources hold the sessions
  onSummarize?: (session: string) => Promise<unknown>; busy?: boolean; message?: string;
}) {
  const [params, setParams] = useSearchParams();
  const fromUrl = params.get("session") ?? undefined;
  // the URL wins; otherwise a live session opens by itself, since that is what you came to see
  const [open, setOpenState] = useState<string | undefined>(() =>
    fromUrl && sessions.some((x) => x.session === fromUrl) ? fromUrl : sessions.find((x) => !x.ended)?.session);
  useEffect(() => {
    if (fromUrl && fromUrl !== open && sessions.some((x) => x.session === fromUrl)) setOpenState(fromUrl);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fromUrl]);
  const setOpen = (sid?: string) => {
    setOpenState(sid);
    const next = new URLSearchParams(params);
    if (sid) next.set("session", sid); else next.delete("session");
    setParams(next, { replace: true });
  };

  if (!sessions.length) {
    return (
      <div className="panel">
        <h2 style={{ marginTop: 0 }}>Sessions</h2>
        <div className="empty">
          no challenger sessions yet; in Claude Code run <span className="mono">/tares:challenger</span>, then leave plan mode or commit
        </div>
      </div>
    );
  }
  return (
    <div className="panel">
      <h2 style={{ marginTop: 0 }}>Sessions</h2>
      <p className="help" style={{ marginTop: 0 }}>
        Every Claude Code session Codex challenged. Open one for its summary, memory proposals and the exchange.
      </p>
      <table>
        <thead><tr><th style={{ width: 22 }} aria-label="open" /><th>when</th><th>repo</th><th title="findings on the plan">plan</th><th title="the reviewed commits, rolled up">commits</th><th>state</th></tr></thead>
        <tbody>
          {sessions.map((x) => {
            const isOpen = open === x.session;
            const roll = commitRollup(x);
            const run = runs?.find((r) => r.id && r.id === x.run_id);
            const waiting = run ? (run.proposals ?? []).filter((_, i) => !run.decisions?.[String(i)]).length : 0;
            return [
              <tr key={x.session} className={`clickable${isOpen ? " sel" : ""}`} onClick={() => setOpen(isOpen ? undefined : x.session)}
                  aria-expanded={isOpen}>
                <td className="mono dim" style={{ paddingRight: 0 }}>{isOpen ? "▾" : "▸"}</td>
                <td style={{ whiteSpace: "nowrap" }}><TimeAgo ts={x.started_at ?? null} /></td>
                <td className="mono">{x.repo ?? <span className="dim">unknown</span>}{x.branch && <span className="dim"> on {x.branch}</span>}</td>
                <td><Verdict compact what="plan" v={x.plan_verdict} findings={x.plan_findings ?? undefined} blocking={x.plan_blocking ?? undefined} /></td>
                <td>
                  <span className={roll.dim ? "dim" : roll.blocking ? "" : "help"}>{roll.text}</span>
                  {x.waived > 0 && <span className="help"> · {x.waived} waived</span>}
                </td>
                <td style={{ whiteSpace: "nowrap" }}>
                  {x.ended ? (x.run_id ? <span className="badge ok">summarized</span> : <span className="badge paused">ended</span>)
                    : <span className="badge agent">live</span>}
                  {waiting > 0 && <span className="help" title="memory proposals nobody has accepted or rejected yet"> · {waiting} waiting</span>}
                </td>
              </tr>,
              isOpen && (
                <tr key={`${x.session}-detail`} className="sel-detail">
                  <td colSpan={6} style={{ padding: "14px 20px 18px 34px", background: "var(--panel)" }}>
                    <SessionDetail s={x} run={runs?.find((r) => r.id && r.id === x.run_id)} project={project}
                                   onSummarize={onSummarize} busy={busy} message={message} onClose={() => setOpen(undefined)} />
                  </td>
                </tr>
              ),
            ];
          })}
        </tbody>
      </table>
    </div>
  );
}

function SessionDetail({ s, run, project, onSummarize, busy, message, onClose }: {
  s: ChallengerSession; run?: Run; project: string;
  onSummarize?: (session: string) => Promise<unknown>; busy?: boolean; message?: string; onClose: () => void;
}) {
  const [all, setAll] = useState(false);
  const [rows, setRows] = useState<TimelineEventRow[]>();
  const [err, setErr] = useState<string>();
  useEffect(() => {
    if (!all) return;
    let live = true;
    setRows(undefined); setErr(undefined);
    api.read({ session: s.session }, "30d", { project })
      .then((r) => { if (live) setRows(r.rows ?? []); })
      .catch((e) => { if (live) setErr(String((e as Error).message ?? e)); });
    return () => { live = false; };
  }, [all, s.session, project]);

  const blocking = s.commits.reduce((n, c) => n + (Number(c.blocking) || 0), 0);
  // the summary text without its proposals section: the proposals render as rows with a decision
  // (and without a leading "Summary" heading of its own: the section already says so)
  const summaryText = (run?.finding ?? "").split(/^\s*#*\s*memory proposals\s*:?\s*$/im)[0]
    .replace(/^\s*#*\s*summary\s*:?\s*\n/i, "").trim();
  return (
    <div>
      <div className="tl-head" style={{ marginBottom: 10 }}>
        <div>
          <h3 style={{ margin: 0 }}><span className="mono">{s.repo ?? s.session}</span>{s.branch && <span className="help"> on {s.branch}</span>}</h3>
          <span className="subtitle" style={{ margin: 0 }}>
            started <TimeAgo ts={s.started_at ?? null} /> · plan <Verdict v={s.plan_verdict} findings={s.plan_findings ?? undefined} blocking={s.plan_blocking ?? undefined} /> · {s.commits.length} commit{s.commits.length === 1 ? "" : "s"} reviewed
            {blocking > 0 && <> · {blocking} blocking finding{blocking === 1 ? "" : "s"}</>}
            {s.waived > 0 && <> · {s.waived} waived</>}
          </span>
        </div>
        <div className="btnrow">
          {onSummarize && (
            <button disabled={busy} onClick={() => onSummarize(s.session)}
                    title={run ? "run the summarizer again on this session" : "summarize this session now, without waiting for it to end"}>
              {busy ? "working…" : run ? "Summarize again" : "Summarize"}
            </button>
          )}
          <button onClick={onClose}>Close</button>
        </div>
      </div>
      {message && <p className="help" style={{ margin: "0 0 8px" }}>{message}</p>}

      <h4 style={{ margin: "12px 0 4px" }}>Summary</h4>
      {run ? (
        <>
          {summaryText ? (
            <div className="md" style={{ maxWidth: 900 }}><ReactMarkdown remarkPlugins={[remarkGfm]}>{summaryText}</ReactMarkdown></div>
          ) : <p className="help" style={{ margin: 0 }}>the summarizer wrote nothing beyond its proposals</p>}
          {run.proposals?.length ? (
            <>
              <h4 style={{ margin: "14px 0 0" }}>Memory proposals</h4>
              <p className="help" style={{ margin: "2px 0 0" }}>Accept stores it as a decision on the memory source, keyed by the repo; the plugin hands it to Claude at the start of the next session there. Reject keeps it out of memory for good.</p>
              <ProposalRows run={run} repo={s.repo ?? ""} />
            </>
          ) : <p className="help" style={{ margin: "6px 0 0" }}>no memory proposals from this session</p>}
        </>
      ) : (
        <p className="help" style={{ margin: 0 }}>
          {s.ended ? "not summarized yet" : "the session is still running; the summarizer runs when it ends"}
          {onSummarize && <> · or press Summarize now</>}
        </p>
      )}

      <div className="tl-head" style={{ marginTop: 18, marginBottom: 0, alignItems: "center" }}>
        <h4 style={{ margin: 0 }}>Exchange</h4>
        <div className="seg small">
          <button className={!all ? "active" : ""} onClick={() => setAll(false)}>challenges only</button>
          <button className={all ? "active" : ""} onClick={() => setAll(true)}>whole session</button>
        </div>
      </div>
      {err && <div className="alert error">{err}</div>}
      {!all ? (
        s.thread.length ? (
          <table style={{ marginTop: 8 }}>
            <thead><tr><th style={{ width: 90 }}>when</th><th>event</th></tr></thead>
            <tbody>
              {s.thread.map((t, i) => (
                <tr key={i}>
                  <td style={{ whiteSpace: "nowrap" }}><TimeAgo ts={t.at} /></td>
                  <td>
                    {t.findings?.length ? (
                      <>
                        {/* the first line of the text is the header; the findings themselves come
                            from the payload, complete (the event text is capped) */}
                        <span className="mono">{t.text.split("\n")[0].split(": [P")[0]}</span>
                        {t.labels.verdict && <span className="tl-labels"><Verdict v={t.labels.verdict} findings={t.labels.finding_count} blocking={t.labels.blocking_count} /></span>}
                        <ul style={{ margin: "6px 0 0", paddingLeft: 18 }}>
                          {t.findings.map((f, j) => (
                            <li key={j} className={f.waived ? "dim" : undefined} style={{ marginBottom: 2 }}>
                              {f.priority && <span className="badge" style={{ marginRight: 6 }}>{f.priority}</span>}
                              <span className="mono" style={{ whiteSpace: "pre-wrap" }}>{f.title}</span>
                              {f.waived && <span className="help"> waived</span>}
                            </li>
                          ))}
                        </ul>
                      </>
                    ) : (
                      <>
                        <span className="mono" style={{ whiteSpace: "pre-wrap" }}>{t.text}</span>
                        {t.labels.verdict && <span className="tl-labels"><Verdict v={t.labels.verdict} findings={t.labels.finding_count} blocking={t.labels.blocking_count} /></span>}
                      </>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : <div className="empty" style={{ marginTop: 8 }}>no challenge events in this session yet</div>
      ) : rows === undefined && !err ? <div className="help" style={{ marginTop: 10 }}>reading session…</div>
        : rows && rows.length ? (
          <table style={{ marginTop: 8 }}>
            <thead><tr><th style={{ width: 74 }}>when</th><th>event</th></tr></thead>
            <tbody>
              {rows.map((r, i) => (
                <tr key={i}>
                  <td className="mono dim" style={{ whiteSpace: "nowrap" }}>{r.offset}</td>
                  <td><span className="mono" style={{ whiteSpace: "pre-wrap" }}>{r.text}</span></td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : rows && <div className="empty" style={{ marginTop: 8 }}>the project's sources hold nothing for this session in the last 30 days</div>}
    </div>
  );
}
