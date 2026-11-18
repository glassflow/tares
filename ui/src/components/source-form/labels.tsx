import { useEffect, useState } from "react";

import { api } from "../../api";
import { formatCount } from "../../lib/number";
import { ruleSummary } from "../bits";
import { Banner, Button, Input } from "../edition";
import { BOX, CHIP, CODE, GROUP_LABEL, Pick, SuggestInput, TABLE, TEXT } from "./parts";

/** The labels and key of a connection, and the value rules of each label (TR-1394). Moved out of
 *  SourceForm.tsx onto the kit; what a row stores and how it becomes a spec are unchanged. */

// labels as an ordered [key, value] list (not a Record) so editing a key doesn't change a React
// key and remount the input, which was eating focus after one character.
export type Attachment = { name: string; format: string; content: string; labels: [string, string][] };
type MapRow = { from: string; to: string };
export type LabelRow = { name: string; kind: "const" | "field"; value: string; primary: boolean;
                         type: "string" | "number";
                         pattern: string; replace: string; map: MapRow[]; normOpen: boolean };

export function labelsToRows(arr: unknown): LabelRow[] {
  if (!Array.isArray(arr)) return [];
  const rows = arr.map((x) => {
    const o = x as Record<string, unknown>;
    const kind: "const" | "field" = "field" in o ? "field" : "const";
    const map = o.map && typeof o.map === "object"
      ? Object.entries(o.map as Record<string, string>).map(([from, to]) => ({ from, to: String(to) }))
      : [];
    return { name: String(o.name ?? ""), kind, value: String(o[kind] ?? ""), primary: !!o.primary,
             type: o.type === "number" ? "number" as const : "string" as const,
             pattern: String(o.pattern ?? ""), replace: String(o.replace ?? ""), map,
             // Collapsed on load: the Rules button says in a word whether a label carries any, and
             // each open editor is a few hundred pixels that buried the form.
             normOpen: false };
  });
  if (rows.length && !rows.some((r) => r.primary)) rows[0].primary = true;  // first is the key by default
  return rows;
}

/** Row -> label spec, including the optional value normalization (pattern/replace + alias map). */
export function rowToSpec(r: LabelRow): Record<string, unknown> {
  const spec: Record<string, unknown> = { name: r.name.trim(), [r.kind]: r.value,
                                          ...(r.primary ? { primary: true } : {}),
                                          // the key is always a string; number labels are aggregatable
                                          ...(r.type === "number" && !r.primary ? { type: "number" } : {}) };
  if (r.kind === "field") {
    if (r.pattern.trim()) { spec.pattern = r.pattern; spec.replace = r.replace; }
    const map = Object.fromEntries(r.map.filter((m) => m.from.trim()).map((m) => [m.from, m.to]));
    if (Object.keys(map).length) spec.map = map;
  }
  return spec;
}

/** What this label does to its values, shared with the read-only tables on the connection's page, so
 *  both name the same rule the same way. "" means it passes values through untouched. */
const normSummary = (row: LabelRow) => ruleSummary(row.pattern, row.map.length);

const plural = (n: number, one: string) => `${formatCount(n)} ${one}${n === 1 ? "" : "s"}`;

const emptyRow = (primary: boolean): LabelRow =>
  ({ name: "", kind: "const", value: "", primary, type: "string", pattern: "", replace: "", map: [], normOpen: false });

export function LabelsEditor({ rows, onChange, fields = [], fieldHints, sourceName, examples }:
  { rows: LabelRow[]; onChange: (r: LabelRow[]) => void; fields?: string[];
    fieldHints?: Record<string, string>; sourceName?: string;
    examples?: Record<string, string> }) {   // a sample value per field, from Discover
  const set = (i: number, patch: Partial<LabelRow>) =>
    onChange(rows.map((r, j) => (j === i ? { ...r, ...patch } : r)));
  const makePrimary = (i: number) => onChange(rows.map((r, j) =>
    j === i ? { ...r, primary: true, type: "string" as const } : { ...r, primary: false }));
  const remove = (i: number) => onChange(rows.filter((_, j) => j !== i));
  // -1: no key picked; the connection keys by its own name (the connector's fallback)
  const keyIndex = rows.findIndex((r) => r.primary);
  const keyRow = keyIndex >= 0 ? rows[keyIndex] : undefined;
  const keyValue = keyRow
    ? (keyRow.kind === "const" ? keyRow.value : examples?.[keyRow.value])
    : undefined;
  return (
    <div className="grid min-w-0 gap-3" data-testid="labels-editor">
      <p className={GROUP_LABEL}>Labels and key</p>
      {/* The key is ONE decision about the connection, so it gets ONE control, not a radio per row. */}
      {rows.length > 0 && (
        <Pick label="Key" className="sm:max-w-[360px]" value={String(keyIndex)}
              hint={keyRow ? (keyValue ? `For example ${keyValue}.` : "The thing each event is about.") : undefined}
              options={["-1", ...rows.map((_, i) => String(i))]}
              labels={{ "-1": sourceName ? `None: the connection's name (${sourceName})` : "None: the connection's name",
                        ...Object.fromEntries(rows.map((r, i) => [String(i), r.name || `Unnamed label ${i + 1}`])) }}
              onChange={(v) => makePrimary(Number(v))} data-testid="label-key" />
      )}
      {rows.map((row, i) => (
        <div key={i} className="grid min-w-0 gap-3 border border-border bg-card p-3" data-testid="label-row">
          <div className="grid items-start gap-3 sm:grid-cols-[minmax(0,1fr)_200px_minmax(0,1fr)_140px]">
            <Input label="Label name" placeholder="service" className="font-mono" spellCheck={false}
                   value={row.name} onChange={(e) => set(i, { name: e.target.value })} />
            <Pick label="Value comes from" value={row.kind} options={["const", "field"]}
                  labels={{ const: "A fixed value", field: "A field of each event" }}
                  onChange={(v) => set(i, { kind: v as LabelRow["kind"] })} />
            {row.kind === "field" && fields.length ? (
              <SuggestInput label="Field" placeholder="service" className="font-mono" spellCheck={false}
                            value={row.value} options={fields} hints={fieldHints}
                            onChange={(e) => set(i, { value: e.target.value })} />
            ) : (
              <Input label={row.kind === "const" ? "Value" : "Field"} className="font-mono" spellCheck={false}
                     placeholder={row.kind === "const" ? "api-server" : "service"}
                     value={row.value} onChange={(e) => set(i, { value: e.target.value })} />
            )}
            <Pick label="Type" value={row.primary ? "string" : row.type} disabled={row.primary}
                  hint={row.primary ? "The key is always text." : undefined}
                  options={["string", "number"]} labels={{ string: "Text", number: "Number" }}
                  onChange={(v) => set(i, { type: v as LabelRow["type"] })} />
          </div>
          <div className="flex flex-wrap items-center gap-2">
            {row.primary && <span className={CHIP} data-testid="label-key-chip">Key</span>}
            {/* Says WHAT the rule is, so a pass-through field and one rewriting its values read differently. */}
            {row.kind === "field" && (
              <Button variant="quiet" aria-expanded={row.normOpen} data-testid="label-rules"
                      onClick={() => set(i, { normOpen: !row.normOpen })}>
                {normSummary(row) ? `Rules: ${normSummary(row)}` : "Add rules"}
              </Button>
            )}
            <Button variant="quiet" className="ml-auto text-err hover:text-err" onClick={() => remove(i)}
                    aria-label={`Remove the label ${row.name || i + 1}`}>
              Remove
            </Button>
          </div>
          {row.kind === "field" && row.normOpen && (
            <NormalizeEditor row={row} onChange={(patch) => set(i, patch)} sourceName={sourceName} />
          )}
        </div>
      ))}
      <div>
        <Button variant="secondary" onClick={() => onChange([...rows, emptyRow(rows.length === 0)])}>Add label</Button>
      </div>
      {fields.length > 0 && (
        <p className={TEXT}>
          This connector provides{" "}
          {fields.map((f, i) => <span key={f}><code className={CODE}>{f}</code>{i < fields.length - 1 ? ", " : ""}</span>)}.
          Pick a field from these; the connection&rsquo;s page shows how often each one is set once data arrives.
        </p>
      )}
    </div>
  );
}

type Preview = { sampled: number; distinct_before: number; distinct_after: number;
                 results: { from: string; to: string; events: number }[] };

/** Merge value variants for one field label. User-first flow: show the values this field actually
 *  has, let the user rename the odd ones onto a canonical one, and reflect the result live. The regex
 *  lives behind a toggle for whole families of variants. */
function NormalizeEditor({ row, onChange, sourceName }:
  { row: LabelRow; onChange: (patch: Partial<LabelRow>) => void; sourceName?: string }) {
  const [preview, setPreview] = useState<Preview>();
  const [pErr, setPErr] = useState<string>();
  const [advanced, setAdvanced] = useState(!!row.pattern);

  // live preview: on open (bare field -> the observed values), and after every edit (debounced)
  // keyed on the spec's text, so a re-render with an equal row does not ask again
  const specKey = row.value.trim() ? JSON.stringify(rowToSpec(row)) : "";
  useEffect(() => {
    if (!sourceName || !specKey) return;
    const t = setTimeout(async () => {
      try {
        setPErr(undefined);
        setPreview(await api.labelPreview(sourceName, JSON.parse(specKey)));
      } catch (e) { setPreview(undefined); setPErr(String((e as Error).message ?? e)); }
    }, 500);
    return () => clearTimeout(t);
  }, [sourceName, specKey]);

  const observed = preview?.results ?? [];
  const merges = observed.filter((r2) => r2.from !== r2.to).length;
  const fromOpts = observed.map((r2) => r2.from);
  const fromHints = Object.fromEntries(observed.map((r2) => [r2.from, plural(r2.events, "event")]));

  const hasRules = !!row.pattern || row.map.length > 0;
  // The editor sits under its row, so the heading names the label it edits.
  const label = row.name.trim();

  return (
    <div className={BOX} data-testid="normalize-editor">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <p className="m-0 text-sm font-semibold text-foreground">
          Merge value variants{label && <> of <code className={CODE}>{label}</code></>}
        </p>
        <div className="flex flex-wrap gap-2">
          {hasRules && (
            <Button variant="quiet" className="text-err hover:text-err"
                    onClick={() => onChange({ pattern: "", replace: "", map: [] })}>
              Clear rules
            </Button>
          )}
          <Button variant="secondary" onClick={() => onChange({ normOpen: false })}>Done</Button>
        </div>
      </div>
      {/* Both sentences carry a fact the screen cannot show: why merging matters, and that the stored
          event keeps its original. */}
      <p className={TEXT}>
        Variants of the same thing (<code className={CODE}>checkout</code>, <code className={CODE}>checkout-svc</code>)
        count as different things and don&rsquo;t correlate. Renaming makes a copy; the original stays in the stored event.
      </p>

      {pErr && <Banner tone="error">{pErr}</Banner>}

      {sourceName && preview && (
        <div className="grid gap-2">
          <p className={TEXT}>
            {label ? <code className={CODE}>{label}</code> : "This field"} has{" "}
            <strong className="text-foreground">{plural(preview.distinct_before, "distinct value")}</strong> in recent events
            {merges > 0 && <>. With your rules it has <strong className="text-foreground">{formatCount(preview.distinct_after)}</strong></>}.
          </p>
          <div className="overflow-x-auto">
            <table className={TABLE}>
              <thead><tr><th>Value seen</th><th className="text-right">Events</th><th>Becomes</th></tr></thead>
              <tbody>
                {observed.slice(0, 12).map((r2, k) => (
                  <tr key={k}>
                    <td className="font-mono" data-user-text>{r2.from}</td>
                    <td className="text-right tabular-nums">{formatCount(r2.events)}</td>
                    {/* Blanking a value is a legitimate rule (a pattern that keeps only what matches),
                        so it is stated, not flagged. */}
                    <td className="font-mono">
                      {r2.from === r2.to
                        ? <span className="text-muted-foreground">Unchanged</span>
                        : r2.to === ""
                        ? <span className="text-muted-foreground">Emptied</span>
                        : <span data-user-text>{r2.to}</span>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
      {sourceName && !preview && !pErr && <p className={TEXT} role="status">Loading the values seen&hellip;</p>}
      {!sourceName && (
        <p className={TEXT}>Once this connection has data, the values it has seen show here, so you can pick what to rename.</p>
      )}

      {row.map.map((m, j) => (
        <div key={j} className="grid items-end gap-3 sm:grid-cols-[minmax(0,1fr)_minmax(0,1fr)_auto]" data-testid="rename-row">
          <SuggestInput label="Rename" placeholder="A value seen in the data" className="font-mono" spellCheck={false}
                        value={m.from} options={fromOpts} hints={fromHints}
                        onChange={(e) => onChange({ map: row.map.map((x, k) => k === j ? { ...x, from: e.target.value } : x) })} />
          <Input label="To" placeholder="The name it should have" className="font-mono" spellCheck={false}
                 value={m.to}
                 onChange={(e) => onChange({ map: row.map.map((x, k) => k === j ? { ...x, to: e.target.value } : x) })} />
          <Button variant="quiet" className="text-err hover:text-err" aria-label={`Remove the rename of ${m.from || "a value"}`}
                  onClick={() => onChange({ map: row.map.filter((_, k) => k !== j) })}>
            Remove
          </Button>
        </div>
      ))}
      <div className="flex flex-wrap gap-2">
        <Button variant="secondary" onClick={() => onChange({ map: [...row.map, { from: "", to: "" }] })}>
          Rename a value
        </Button>
        <Button variant="quiet" aria-expanded={advanced} onClick={() => setAdvanced((a) => !a)}>
          {advanced ? "Hide the pattern rule" : "Add a pattern rule"}
        </Button>
      </div>

      {advanced && (
        <PatternRule key={row.pattern === "" ? "empty" : "set"} row={row} onChange={onChange} />
      )}
    </div>
  );
}

const escRe = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

type RuleKind = "suffix" | "prefix" | "after" | "custom";

const RULE_INPUT: Record<Exclude<RuleKind, "custom">, { label: string; placeholder: string; hint: string }> = {
  suffix: { label: "Ending to remove", placeholder: "-svc", hint: "For example -svc turns checkout-svc into checkout." },
  prefix: { label: "Beginning to remove", placeholder: "prod-", hint: "For example prod- turns prod-checkout into checkout." },
  after: { label: "Cut from", placeholder: ".", hint: "For example . turns checkout.internal.eu into checkout." },
};

/** Builds the pattern from plain choices ("remove the ending -svc"), so nobody has to write a regex
 *  unless they pick custom. The stored config is still just pattern/replace. */
function PatternRule({ row, onChange }:
  { row: LabelRow; onChange: (patch: Partial<LabelRow>) => void }) {
  const [kind, setKind] = useState<RuleKind>(row.pattern ? "custom" : "suffix");
  const [text, setText] = useState("");

  const apply = (k: RuleKind, t: string) => {
    if (k === "suffix") onChange({ pattern: t ? `${escRe(t)}$` : "", replace: "" });
    else if (k === "prefix") onChange({ pattern: t ? `^${escRe(t)}` : "", replace: "" });
    else if (k === "after") onChange({ pattern: t ? `${escRe(t)}.*$` : "", replace: "" });
  };

  return (
    <div className="grid gap-3 border-t border-border pt-3" data-testid="pattern-rule">
      <p className={TEXT}>
        A pattern rule fixes a whole family of values at once. It applies before the renames above, and the
        table shows its effect live.
      </p>
      <div className="grid items-start gap-3 sm:grid-cols-[260px_minmax(0,1fr)]">
        <Pick label="Pattern rule" value={kind}
              options={["suffix", "prefix", "after", "custom"]}
              labels={{ suffix: "Remove this ending", prefix: "Remove this beginning",
                        after: "Cut everything from this on", custom: "Custom regular expression" }}
              onChange={(v) => { const k = v as RuleKind; setKind(k);
                                 if (k === "custom") return; apply(k, text); }} />
        {kind !== "custom" ? (
          <Input label={RULE_INPUT[kind].label} hint={RULE_INPUT[kind].hint} placeholder={RULE_INPUT[kind].placeholder}
                 className="font-mono" spellCheck={false} value={text}
                 onChange={(e) => { setText(e.target.value); apply(kind, e.target.value); }} />
        ) : (
          <div className="grid items-start gap-3 sm:grid-cols-[2fr_1fr]">
            <Input label="Regular expression" placeholder="-(service|svc)$" className="font-mono" spellCheck={false}
                   value={row.pattern} onChange={(e) => onChange({ pattern: e.target.value })} />
            <Input label="Replacement" hint="Empty removes the match." className="font-mono" spellCheck={false}
                   value={row.replace} onChange={(e) => onChange({ replace: e.target.value })} />
          </div>
        )}
      </div>
    </div>
  );
}
