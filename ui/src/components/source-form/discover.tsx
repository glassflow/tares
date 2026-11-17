import { useMemo, useState } from "react";

import { formatCount } from "../../lib/number";
import type { ColumnsProposal } from "../../types";
import { Button, Checkbox, EmptyState, Input, Tabs } from "../edition";
import { CHIP, CODE, GROUP_LABEL, TABLE, TEXT } from "./parts";

/** What Discover proposes and the pickers that confirm it: Prometheus alert rules and metrics, the
 *  queries they become, and Postgres columns (TR-1394). Kit parts only. */

export type AlertRule = { name: string; severity: string; group: string; state: string };

const plural = (n: number, one: string, many = `${one}s`) => `${formatCount(n)} ${n === 1 ? one : many}`;
const LIST = "grid max-h-[300px] min-w-0 gap-0.5 overflow-y-auto border border-border bg-card p-2";

/** Prometheus alerts curation: the configured alerting rules (read-only, for transparency), an
 *  optional severity filter (blank = all), and the pending toggle. Unlike metrics, you don't pick
 *  single rules: you receive every alert that goes off, narrowed by severity, and the list previews
 *  exactly that as the filter changes. */
export function AlertRulesCuration({ rules, severities, sev, onSevToggle, includePending, onIncludePending,
                                     onConfirm, busy, summary }: {
  rules: AlertRule[]; severities: string[]; sev: Set<string>; onSevToggle: (s: string) => void;
  includePending: boolean; onIncludePending: (v: boolean) => void;
  onConfirm: () => void; busy: boolean; summary: string;
}) {
  const [q, setQ] = useState("");
  const shown = rules.filter((r) =>
    (!sev.size || sev.has(r.severity)) &&
    (!q || `${r.name} ${r.group}`.toLowerCase().includes(q.toLowerCase())));
  return (
    <div className="grid min-w-0 gap-3" data-testid="alert-curation">
      <p className={TEXT}><span data-user-text>{summary}</span>. Tares receives these as they go off; narrow them by severity if you like.</p>
      {severities.length > 0 && (
        <div className="grid gap-1.5">
          <p className={GROUP_LABEL}>Severity</p>
          <div className="flex flex-wrap gap-2">
            {severities.map((s) => (
              <Button key={s} variant={sev.has(s) ? "primary" : "secondary"} aria-pressed={sev.has(s)}
                      className="font-mono" onClick={() => onSevToggle(s)}>{s}</Button>
            ))}
          </div>
        </div>
      )}
      <Checkbox label="Also receive pending alerts" hint="Alerts still waiting out their for: duration."
                checked={includePending} onCheckedChange={(v) => onIncludePending(v === true)} />
      <Input label="Filter rules" value={q} onChange={(e) => setQ(e.target.value)} className="sm:max-w-[360px]" />
      <div className={LIST}>
        {shown.map((r, i) => (
          <div key={`${r.group}/${r.name}/${r.severity}/${i}`}
               className="grid grid-cols-[minmax(0,1fr)_auto_auto] items-center gap-2 px-1 py-1">
            <span className="truncate font-mono text-[13px]" data-user-text>{r.name}</span>
            {r.severity ? <span className={CHIP} data-user-text>{r.severity}</span> : <span />}
            {r.state === "firing" || r.state === "pending"   // words-ok
              ? <span className={CHIP + (r.state === "pending" ? "" : " border-err-border text-err")} data-user-text>{r.state}</span>
              : <span />}
          </div>
        ))}
        {!shown.length && <EmptyState>No rules match.</EmptyState>}
      </div>
      <div className="flex flex-wrap items-center gap-3">
        <Button variant="primary" disabled={busy} onClick={onConfirm}>
          Receive {sev.size ? `${[...sev].join(", ")} ` : "all "}alerts
        </Button>
        <span className={TEXT}>{formatCount(shown.length)} of {plural(rules.length, "rule")} shown</span>
      </div>
    </div>
  );
}

/** Prometheus metric picker: two tabs (by name pattern, by label) that both add to one shared basket.
 *  No PromQL: the user picks metric names, and the connector compiles the basket into a single
 *  {__name__=~"(a|b|…)"} selector on confirm. */
export function MetricBasket({ catalog, basket, onAdd, onRemove, fetchLabelMetrics, onConfirm, busy }: {
  catalog: { metrics: string[]; labels: string[] };
  basket: Set<string>;
  onAdd: (names: string[]) => void;
  onRemove: (name: string) => void;
  fetchLabelMetrics: (label: string) => Promise<string[]>;
  onConfirm: () => void;
  busy: boolean;
}) {
  const [nameQ, setNameQ] = useState("");
  const [labelQ, setLabelQ] = useState("");
  const [pickedLabel, setPickedLabel] = useState<string>();
  const [labelMetrics, setLabelMetrics] = useState<string[]>();
  const [loadingLabel, setLoadingLabel] = useState(false);

  const nameMatches = useMemo(() => {
    const q = nameQ.trim().toLowerCase();
    if (!q) return [];
    const star = q.endsWith("*");
    const pat = star ? q.slice(0, -1) : q;
    return catalog.metrics
      .filter((m) => { const lm = m.toLowerCase(); return star ? lm.startsWith(pat) : lm.includes(pat); })
      .slice(0, 500);
  }, [nameQ, catalog.metrics]);

  const labelMatches = useMemo(() => {
    const q = labelQ.trim().toLowerCase();
    return catalog.labels.filter((l) => !q || l.toLowerCase().includes(q)).slice(0, 300);
  }, [labelQ, catalog.labels]);

  const pickLabel = async (l: string) => {
    setPickedLabel(l); setLoadingLabel(true); setLabelMetrics(undefined);
    try { setLabelMetrics(await fetchLabelMetrics(l)); } finally { setLoadingLabel(false); }
  };

  const results = (items: string[]) => (
    <div className="grid gap-2">
      <div className="flex flex-wrap items-center gap-3">
        <Button variant="secondary" disabled={!items.length} onClick={() => onAdd(items)}>
          Add all {formatCount(items.length)}
        </Button>
        <span className={TEXT}>
          {plural(items.length, "match", "matches")}{items.length === 500 ? ". Showing the first 500; narrow the pattern." : ""}
        </span>
      </div>
      <div className={LIST + " max-h-[240px]"} data-testid="metric-results">
        {items.map((m) => (
          <Checkbox key={m} label={m} className="font-mono" checked={basket.has(m)}
                    onCheckedChange={() => (basket.has(m) ? onRemove(m) : onAdd([m]))} />
        ))}
      </div>
    </div>
  );

  const byName = (
    <div className="grid gap-3 pt-3">
      <Input label="Metric name or prefix" hint="End with * for a prefix match, for example node_*."
             className="font-mono" spellCheck={false} value={nameQ} onChange={(e) => setNameQ(e.target.value)} />
      {nameQ.trim()
        ? results(nameMatches)
        : <p className={TEXT}>{plural(catalog.metrics.length, "metric")} available. Start typing to filter.</p>}
    </div>
  );

  const byLabel = (
    <div className="grid gap-3 pt-3">
      <Input label="Filter labels" hint="For example namespace or service."
             className="font-mono" spellCheck={false} value={labelQ} onChange={(e) => setLabelQ(e.target.value)} />
      <div className={LIST + " max-h-[160px]"} data-testid="metric-labels">
        {labelMatches.map((l) => (
          <Button key={l} variant={pickedLabel === l ? "secondary" : "quiet"} aria-pressed={pickedLabel === l}
                  className="justify-start font-mono" onClick={() => void pickLabel(l)}>{l}</Button>
        ))}
      </div>
      {loadingLabel && <p className={TEXT} role="status">Loading the metrics that carry <code className={CODE}>{pickedLabel}</code>&hellip;</p>}
      {labelMetrics && !loadingLabel && (
        <div className="grid gap-2">
          <p className={TEXT}>Metrics that carry <code className={CODE}>{pickedLabel}</code>:</p>
          {results(labelMetrics)}
        </div>
      )}
    </div>
  );

  return (
    <div className="grid min-w-0 gap-3" data-testid="metric-basket">
      <p className={TEXT}>Choose the metrics to receive, by name or by a label they carry. No PromQL to write.</p>
      <Tabs aria-label="Find metrics" defaultValue="name"
            items={[{ value: "name", label: "By metric name", content: byName },
                    { value: "label", label: "By label", content: byLabel }]} />
      <div className="grid gap-2 border-t border-border pt-3">
        <div className="flex flex-wrap items-center gap-3">
          <strong className="text-sm" data-testid="basket-count">{plural(basket.size, "metric")} chosen</strong>
          {basket.size > 0 && (
            <Button variant="quiet" onClick={() => [...basket].forEach(onRemove)}>Clear</Button>
          )}
          <Button variant="primary" className="ml-auto" disabled={busy || basket.size === 0} onClick={onConfirm}>
            Use {basket.size ? `these ${formatCount(basket.size)}` : "these"} metrics
          </Button>
        </div>
        {basket.size > 0 && (
          <div className="flex max-h-[120px] flex-wrap gap-1 overflow-y-auto">
            {[...basket].map((m) => (
              <Button key={m} variant="secondary" className="h-auto px-1.5 py-0.5 font-mono text-xs"
                      aria-label={`Remove ${m}`} onClick={() => onRemove(m)}>
                {m} <span aria-hidden>×</span>
              </Button>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

/** Prometheus queries, one line each (a whole-family {__name__=~"fam_.*"} query or a derived series),
 *  with a remove button. Full hand-editing stays in the Advanced group below. */
export function PromQueriesView({ rows, onChange }:
  { rows: Array<Record<string, string>>; onChange: (r: Array<Record<string, string>>) => void }) {
  const summarize = (q: Record<string, string>) => {
    if (q.by_name) {
      // inner of __name__=~"..." → either a single family (prefix_.*) or an (a|b|c) basket
      const inner = (q.promql?.match(/=~"(.*)"/)?.[1] ?? "").replace(/^\(|\)$/g, "");
      if (/^[A-Za-z0-9_]+_\.\*$/.test(inner)) {
        return { title: inner.replace(/_\.\*$/, "_*"),
                 note: `Whole family${q.exclude ? `, except ${q.exclude}` : ""}` };
      }
      const names = inner.split("|").filter(Boolean);
      return { title: plural(names.length, "metric"),
               note: names.slice(0, 4).join(", ") + (names.length > 4 ? ", …" : "") };
    }
    return { title: (q.text ?? "").replace(" {key}={val}", "") || q.event_type || "Derived",
             note: q.promql ?? "" };
  };
  return (
    <div className="grid min-w-0 gap-2" data-testid="prom-queries">
      <div className="grid gap-1.5">
        <p className={GROUP_LABEL}>Metrics to receive</p>
        <p className="m-0 text-xs text-muted-foreground">
          The metrics this connection receives. Remove any you don&rsquo;t want, or change the metrics above.
        </p>
      </div>
      {rows.length === 0 && <EmptyState>None yet. Discover the metrics above and pick some.</EmptyState>}
      {rows.map((q, i) => {
        const s = summarize(q);
        return (
          <div key={i} className="flex min-w-0 items-center gap-3 border border-border bg-card px-3 py-1.5" data-testid="prom-query">
            <span className="flex-none font-mono text-[13px] text-foreground" data-user-text>{s.title}</span>
            <span className="min-w-0 flex-1 truncate font-mono text-xs text-muted-foreground" title={s.note} data-user-text>{s.note}</span>
            <Button variant="quiet" className="flex-none text-err hover:text-err" aria-label={`Remove ${s.title}`}
                    onClick={() => onChange(rows.filter((_, j) => j !== i))}>Remove</Button>
          </div>
        );
      })}
    </div>
  );
}

/** Postgres column selection as a checklist, once Discover knows the table's columns. Emits the same
 *  comma-separated string the API uses: all columns checked -> "" (SELECT *); a subset -> "a,b,c".
 *  The cursor, key and time columns are always pulled, so they show checked and locked. */
export function ColumnPicker({ columns, value, mandatory, onChange }: {
  columns: string[]; value: string; mandatory: Set<string>; onChange: (v: string) => void;
}) {
  const [q, setQ] = useState("");
  const listed = value.trim()
    ? new Set(value.split(",").map((s) => s.trim()).filter(Boolean))
    : null;   // null = "all"
  const isChecked = (c: string) => mandatory.has(c) || listed === null || listed.has(c);
  const emit = (checked: Set<string>) => {
    mandatory.forEach((m) => checked.add(m));
    onChange(checked.size >= columns.length ? "" : columns.filter((c) => checked.has(c)).join(","));
  };
  const toggle = (c: string) => {
    const checked = new Set(columns.filter(isChecked));
    if (checked.has(c)) checked.delete(c); else checked.add(c);
    emit(checked);
  };
  const nChecked = columns.filter(isChecked).length;
  const shown = columns.filter((c) => !q || c.toLowerCase().includes(q.toLowerCase()));
  return (
    <div className="grid min-w-0 gap-2" data-testid="column-picker">
      <div className="flex flex-wrap items-center gap-2">
        <Button variant="secondary" onClick={() => onChange("")}>All</Button>
        <Button variant="secondary" onClick={() => emit(new Set())}>None</Button>
        <span className={TEXT}>
          {formatCount(nChecked)} of {plural(columns.length, "column")} chosen{nChecked >= columns.length ? ": every column is pulled" : ""}
        </span>
      </div>
      {columns.length > 8 && (
        <Input label="Filter columns" value={q} className="font-mono sm:max-w-[360px]" spellCheck={false}
               onChange={(e) => setQ(e.target.value)} />
      )}
      <div className={LIST + " max-h-[220px]"}>
        {shown.map((c) => (
          <Checkbox key={c} label={mandatory.has(c) ? `${c} (always pulled)` : c} className="font-mono"
                    checked={isChecked(c)} disabled={mandatory.has(c)} onCheckedChange={() => toggle(c)} />
        ))}
      </div>
    </div>
  );
}

/** Table-shaped Discover result (Postgres): every column with its type and the role the proposal gives
 *  it (cursor, key, label), so the user sees the choices before applying them. */
export function ColumnsProposalView({ proposal, onApply }: { proposal: ColumnsProposal; onApply: () => void }) {
  const cfg = proposal.proposed_config as { cursor_column?: string; key_column?: string;
                                            labels?: { field?: string }[] };
  const labelFields = new Set((cfg.labels ?? []).map((l) => l.field).filter(Boolean));
  const role = (col: string) =>
    col === cfg.cursor_column ? "cursor" : col === cfg.key_column ? "key"
      : labelFields.has(col) ? "label" : "";
  return (
    <div className="grid min-w-0 gap-3" data-testid="columns-proposal">
      <p className={TEXT} data-user-text>{proposal.summary}</p>
      <div className="overflow-x-auto">
        <table className={TABLE}>
          <thead><tr><th>Column and type</th><th className="whitespace-nowrap">Proposed as</th></tr></thead>
          <tbody>
            {proposal.columns.map((c) => (
              <tr key={c.name}>
                <td className="[overflow-wrap:anywhere]" data-user-text>
                  <span className="font-mono">{c.name}</span>{" "}
                  <span className="font-mono text-xs text-muted-foreground">{c.type}</span>
                </td>
                <td className="whitespace-nowrap">{role(c.name) && <span className={CHIP}>{role(c.name)}</span>}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="flex flex-wrap items-center gap-3">
        <Button variant="primary" onClick={onApply}>Apply to form</Button>
        <span className={TEXT}>Fills the fields below. Review them, test the connection, then save.</span>
      </div>
    </div>
  );
}
