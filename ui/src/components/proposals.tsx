import { useEffect, useState } from "react";

import { api } from "../api";
import type { Trigger } from "../types";

// Shared proposal-card machinery for the in-app agent (Ask chat and the AI-guided project
// builder). The agent's propose_* tools stream these as structured cards; every mutation happens
// only when the user clicks Apply, through the normal validated management APIs.
//
// `source` and `agent` are build-mode cards (TR-243, TR-246): they arrive only on the builder
// page, which renders them as prefilled forms rather than through applyProposal, because both
// need values only the user has (a token, a Slack channel).

export type LabelSpec = { name: string; field?: string; const?: string; primary?: boolean;
                          type?: "string" | "number";
                          pattern?: string; replace?: string; map?: Record<string, string> };
export type TriggerCondition = { aggregate: string; predicate: string; window: string; field?: string };
export type Proposal =
  | { id: string; kind: "labels"; source: string; labels: LabelSpec[]; reasoning: string }
  | { id: string; kind: "view"; name: string; key_field: string; sources: string[];
      filters?: Array<Record<string, unknown>>; reasoning: string }
  | { id: string; kind: "trigger"; name: string; view: string; condition: TriggerCondition;
      emit?: { kind?: string; context_window?: string }; cooldown?: string; reasoning: string }
  | { id: string; kind: "source"; name: string; connector: string; poll?: string;
      config?: Record<string, unknown>; needs: string[]; reasoning: string }
  | { id: string; kind: "project"; template: string; name: string; params?: Record<string, unknown>;
      needs: string[]; reasoning: string }
  | { id: string; kind: "agent"; name: string; trigger: string; prompt: string; model?: string;
      max_rounds?: number; budget_usd?: number;
      delivery: { kind: "slack" | "webhook" | "none"; url?: string };   // url only when the user typed it
      reasoning: string };

// The operators a view filter may use — the set `tares/config.py` validates against on Apply.
const FILTER_OPS = ["eq", "neq", "contains", "gt", "gte", "lt", "lte"];

/** One filter as it will be sent, plus whether it is well-formed.
 *
 *  The card used to say "2 filter(s)" and nothing else, which is the one place a human could have
 *  caught a bad filter before pressing Apply — and it was showing a count. A filter the assistant
 *  got wrong (a flat {label: value} pair, or "==" for the operator) renders as its raw JSON and is
 *  marked, so the mistake is visible on the card rather than arriving as a 400 afterwards. */
export function filterText(f: Record<string, unknown>): { text: string; ok: boolean } {
  const { field, op, value } = f as { field?: unknown; op?: unknown; value?: unknown };
  const ok = typeof field === "string" && typeof op === "string"
    && FILTER_OPS.includes(op) && value !== undefined && value !== null;
  return ok
    ? { text: `${field as string} ${op as string} ${String(value)}`, ok: true }
    : { text: JSON.stringify(f), ok: false };
}

export type Decision = "applied" | "skipped" | "error";
export type DecisionMap = Record<string, { status: Decision; detail?: string }>;

/** Apply one proposal via the normal management APIs. Throws with a readable message. Source
 *  and agent cards are not applied here: they become forms on the builder page. */
export async function applyProposal(p: Proposal): Promise<void> {
  if (p.kind === "source" || p.kind === "agent" || p.kind === "project") {
    throw new Error(`a ${p.kind} proposal is completed as a form, not applied as is`);
  }
  if (p.kind === "labels") {
    const src = await api.source(p.source);
    await api.updateSource(p.source, {
      name: src.name, type: src.type, connector: src.connector, poll: src.poll,
      config: { ...src.config, labels: p.labels },
    });
  } else if (p.kind === "view") {
    // Upsert: the agent proposes the same way for a new view and an edit to an existing one —
    // apply as an update when the name already exists (create-only 409'd with "already exists").
    const body = { name: p.name, key_field: p.key_field,
                   sources: p.sources, filters: (p.filters ?? []) as never };
    const exists = (await api.views()).some((v) => v.name === p.name);
    if (exists) await api.updateView(p.name, body);
    else await api.createView(body);
  } else {
    const t = {
      name: p.name, view: p.view,
      condition: p.condition,
      emit: { kind: p.emit?.kind ?? p.name, context_window: p.emit?.context_window ?? "15m" },
      cooldown: p.cooldown ?? "5m",
    } as Trigger;
    const exists = (await api.triggers()).some((x) => x.name === p.name);
    if (exists) await api.updateTrigger(p.name, t);
    else await api.createTrigger(t);
  }
}

/** The hard evidence for a proposed value merge: the actual before→after table computed against
 *  the source's observed values (same stateless preview endpoint the label editor uses). */
function NormPreview({ source, label }: { source: string; label: LabelSpec }) {
  const [preview, setPreview] = useState<{ sampled: number; distinct_before: number;
    distinct_after: number; results: { from: string; to: string; events: number }[] }>();
  const [err, setErr] = useState<string>();

  useEffect(() => {
    api.labelPreview(source, label as Record<string, unknown>)
      .then(setPreview)
      .catch((e) => setErr(String((e as Error).message ?? e)));
  }, [source, JSON.stringify(label)]);

  if (err) return <div className="alert error">merge preview failed: {err}</div>;
  if (!preview) return <div className="dim" style={{ marginBottom: 8 }}>computing merge preview…</div>;
  const merges = preview.results.filter((r) => r.from !== r.to);
  return (
    <div style={{ margin: "0 0 10px" }}>
      <span className="help">
        <span className="mono">{label.name}</span>: {preview.distinct_before} value{preview.distinct_before === 1 ? "" : "s"} →{" "}
        <strong>{preview.distinct_after}</strong> after this merge ({preview.sampled} events sampled)
        {merges.length === 0 && <> · <strong>no observed value actually changes</strong></>}
      </span>
      {merges.length > 0 && (
        <table style={{ marginTop: 6 }}>
          <thead><tr><th>value seen</th><th className="num">events</th><th>will become</th></tr></thead>
          <tbody>
            {merges.slice(0, 8).map((r, k) => (
              <tr key={k}>
                <td className="mono">{r.from}</td>
                <td className="num">{r.events}</td>
                <td className="mono"><span className="badge ok" style={{ marginRight: 6 }}>→</span>{r.to}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

/** What a proposal is called on its card: "View timeline", "Labels for logs", ... */
export function proposalTitle(p: Proposal) {
  if (p.kind === "labels") return <>Labels for <span className="mono">{p.source}</span></>;
  const noun = { view: "View", trigger: "Trigger", source: "Source", agent: "Agent", project: "Project" }[p.kind];
  return <>{noun} <span className="mono">{p.name}</span></>;
}

/** The card around any proposal: title and state badge, the body, the reasoning paragraph, the
 *  error if the last Apply failed, and the Apply/Skip row. Ask's ProposalCard and the builder's
 *  form cards share this so a proposal looks the same wherever it appears. `actions` replaces
 *  the default Apply/Skip row (the builder's forms carry their own submit button); `null` hides
 *  the row without replacing it. */
export function ProposalShell({ title, decision, reasoning, children, onApply, onSkip, actions, applyLabel }: {
  title: React.ReactNode; decision?: { status: Decision; detail?: string };
  reasoning: string; children?: React.ReactNode;
  onApply?: () => void; onSkip?: () => void;
  actions?: React.ReactNode | null; applyLabel?: string;
}) {
  const st = decision?.status;
  const open = !st || st === "error";
  return (
    <div className="card" style={{ margin: "10px 0", padding: 14, opacity: st === "skipped" ? 0.55 : 1 }}>
      <div className="pagehead" style={{ marginBottom: 8 }}>
        <h3 style={{ margin: 0 }}>{title}</h3>
        {st === "applied" && <span className="badge ok">applied</span>}
        {st === "skipped" && <span className="badge starting">skipped</span>}
        {st === "error" && <span className="badge error">failed</span>}
      </div>
      {children}
      <p className="help" style={{ whiteSpace: "normal", margin: "0 0 10px" }}>{reasoning}</p>
      {decision?.detail && <div className="alert error">{decision.detail}</div>}
      {open && (actions !== undefined ? actions : (
        <div className="btnrow">
          {onApply && <button className="primary" onClick={onApply}>{st === "error" ? "Retry" : (applyLabel ?? "Apply")}</button>}
          {onSkip && <button onClick={onSkip}>Skip</button>}
        </div>
      ))}
    </div>
  );
}

export function ProposalCard({ proposal, decision, onApply, onSkip }: {
  proposal: Proposal; decision?: { status: Decision; detail?: string };
  onApply: () => void; onSkip: () => void;
}) {
  return (
    <ProposalShell title={proposalTitle(proposal)} decision={decision} reasoning={proposal.reasoning}
                   onApply={onApply} onSkip={onSkip}>
      <ProposalBody proposal={proposal} />
    </ProposalShell>
  );
}

/** The kind-specific summary of a proposal: the label table, the view's key and filters, the
 *  trigger's condition, a source's connector and prefilled fields, an agent's trigger and
 *  delivery. */
export function ProposalBody({ proposal }: { proposal: Proposal }) {
  return (
    <>

      {proposal.kind === "labels" && (
        <table style={{ marginBottom: 8 }}>
          <thead><tr><th>label</th><th>from</th></tr></thead>
          <tbody>
            {proposal.labels.map((l) => (
              <tr key={l.name}>
                <td className="mono">{l.name}{l.primary && <span className="badge ok" style={{ marginLeft: 8 }}>key</span>}</td>
                <td className="mono">
                  {l.field != null
                    ? <><span className="badge push" style={{ marginRight: 8 }}>field</span>{l.field}</>
                    : <><span className="badge push" style={{ marginRight: 8 }}>const</span>{String(l.const ?? "")}</>}
                  {(l.pattern || l.map) && (
                    <div className="help">
                      normalize: {l.pattern && <>s/<span className="mono">{l.pattern}</span>/<span className="mono">{l.replace ?? ""}</span>/</>}
                      {l.pattern && l.map ? " · " : ""}
                      {l.map && `${Object.keys(l.map).length} alias(es)`}
                    </div>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {proposal.kind === "view" && (
        <p style={{ margin: "0 0 8px" }}>
          key <span className="chip mono">{proposal.key_field}</span> over{" "}
          {proposal.sources.map((s) => <span className="chip mono" key={s}>{s}</span>)}
          {!!proposal.filters?.length && (
            <> · keeping only{" "}
              {proposal.filters.map((f, i) => {
                const { text, ok } = filterText(f);
                return (
                  <span key={i} className={"chip" + (ok ? "" : " invalid")}
                        title={ok ? undefined
                                  : "not a valid filter; needs field, op and value, with op one of "
                                    + FILTER_OPS.join(", ") + ". Apply will be rejected."}>
                    {text}
                  </span>
                );
              })}
            </>
          )}
        </p>
      )}

      {proposal.kind === "trigger" && (
        <p style={{ margin: "0 0 8px" }} className="mono">
          {proposal.condition.aggregate}({proposal.condition.field ?? "*"}){" "}
          {proposal.condition.predicate} over {proposal.condition.window} on{" "}
          <span className="chip">{proposal.view}</span>
          <span className="help" style={{ display: "block", fontFamily: "inherit" }}>
            wakes subscribers with {proposal.emit?.context_window ?? "15m"} of timeline ·
            cooldown {proposal.cooldown ?? "5m"} per entity
          </span>
        </p>
      )}

      {proposal.kind === "source" && (
        <p style={{ margin: "0 0 8px" }}>
          connector <span className="chip mono">{proposal.connector}</span>
          {proposal.poll && <> · polls every <span className="chip mono">{proposal.poll}</span></>}
          {Object.keys(proposal.config ?? {}).length > 0 && (
            <> · prefilled{" "}
              {Object.entries(proposal.config ?? {}).map(([k, v]) => (
                <span key={k} className="chip mono" title={String(v)}>{k}</span>
              ))}
            </>
          )}
          {proposal.needs.length > 0 && (
            <span className="help" style={{ display: "block" }}>
              you fill in: {proposal.needs.map((n) => <span key={n} className="chip mono">{n}</span>)}
            </span>
          )}
        </p>
      )}

      {proposal.kind === "project" && (
        <p style={{ margin: "0 0 8px" }}>
          from the template <span className="chip mono">{proposal.template}</span>
          {Object.entries(proposal.params ?? {}).filter(([, v]) => v !== "" && v !== null && v !== undefined).length > 0 && (
            <> · prefilled{" "}
              {Object.entries(proposal.params ?? {}).filter(([, v]) => v !== "" && v !== null && v !== undefined).map(([k, v]) => (
                <span key={k} className="chip mono" title={typeof v === "string" ? v : JSON.stringify(v)}>{k}</span>
              ))}
            </>
          )}
          {proposal.needs.length > 0 && (
            <span className="help" style={{ display: "block" }}>
              you fill in: {proposal.needs.map((n) => <span key={n} className="chip mono">{n}</span>)}
            </span>
          )}
        </p>
      )}

      {proposal.kind === "agent" && (
        <p style={{ margin: "0 0 8px" }}>
          runs on <span className="chip mono">{proposal.trigger}</span>
          {proposal.model && <> · model <span className="chip mono">{proposal.model}</span></>}
          {" · "}
          {proposal.delivery.kind === "slack" ? "posts the finding to a Slack channel you pick"
            : proposal.delivery.kind === "webhook" ? (proposal.delivery.url ? <>posts the finding to <span className="mono">{proposal.delivery.url}</span></> : "posts the finding to a URL you give")
            : "writes the finding onto the timeline only"}
        </p>
      )}

      {proposal.kind === "labels" && proposal.labels
        .filter((l) => l.field && (l.pattern || l.map))
        .map((l) => <NormPreview key={l.name} source={proposal.source} label={l} />)}
    </>
  );
}
