import { useState } from "react";

import { Combo, Picker } from "../bits";
import type { PlanCondition, PlanKnob, PlanWake, TriggerFilter } from "../../types";
import { NumberStepper } from "./common";
import { Choice, EditorFrame, ItemActions, Problems } from "./planBits";
import { kebab, newKey, putWake, uniqueName, useFieldChoices } from "./planEdit";
import type { Baseline, CardCtx } from "./plan";

// Card 2, Wakes when: each wake-up as its sentence with the numbers tuned in place, and, opened,
// in plain form: which sources, what to count (events, or the average, highest, lowest or total
// of a number), the threshold and window, "only when" conditions built from the sources' real
// fields, counted separately for each of a label, or every N minutes instead. The wait before
// waking again stays its own line. Tares names the wake-up itself.

const esc = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

/** The sentence cut around its numbers, in knob order, so the steppers sit where the numbers were
 *  ("more than [5] errors in [5] minutes"). null when a number can't be found in order; the
 *  steppers then go on their own lines under the sentence. */
function splitAtKnobs(w: PlanWake, base: Baseline, knobs: PlanKnob[]): (string | PlanKnob)[] | null {
  const out: (string | PlanKnob)[] = [];
  let rest = w.sentence;
  for (const k of knobs) {
    const v = base[`${w.key}.${k.id}`];
    if (v === undefined) return null;
    const m = new RegExp(`(^|[^\\d.,])(${esc(String(v))})(?![\\d.,]\\d|\\d)`).exec(rest);
    if (!m) return null;
    const at = m.index + m[1].length;
    out.push(rest.slice(0, at), k);
    rest = rest.slice(at + String(v).length);
  }
  out.push(rest);
  return out;
}

/** A wake-up on every event: a count above 0 (or at least 1), not a schedule. */
function everyEvent(w: PlanWake): boolean {
  const c = w.condition;
  if (c.every || (c.aggregate && c.aggregate !== "count")) return false;
  const p = String(c.predicate ?? "").replace(/\s+/g, "");
  return p === ">0" || p === ">=1";
}

function WakeSentence({ w, base, onKnob, disabled }: {
  w: PlanWake; base: Baseline; onKnob: (id: string, v: number) => void; disabled?: boolean;
}) {
  const schedule = !!w.condition.every;
  // the wait after a wake is not in the sentence: it gets a line of its own (a schedule has none)
  const wait = schedule ? undefined : w.knobs.find((k) => k.id === "cooldown_minutes");
  // "more than 0 events in 15 minutes" is every event: the count and window say nothing a person
  // would change, and a "0 events" stepper reads as never, so they stay off the card
  const every = everyEvent(w);
  const inline = w.knobs.filter((k) => k.id !== "cooldown_minutes"
    && !(every && (k.id === "threshold" || k.id === "window_minutes")));
  const parts = inline.length && w.sentence ? splitAtKnobs(w, base, inline) : null;
  const stepper = (k: PlanKnob) => (
    <NumberStepper key={k.id} value={k.value} min={k.min} max={k.max} label={k.label}
                   onChange={(v) => { if (!disabled) onKnob(k.id, v); }} />
  );
  const waitLine = wait && (
    <p className="su-wake-then">
      <span className="su-wake-then-lead">Then waits before waking again</span>
      <span className="su-knob-row">{stepper(wait)} <span>minutes</span></span>
    </p>
  );
  if (parts) {
    return (
      <>
        <p className="su-wake">
          {parts.map((x, i) => (typeof x === "string" ? <span key={i}>{x}</span> : stepper(x)))}
        </p>
        {waitLine}
      </>
    );
  }
  return (
    <>
      <span className="su-item-what">{w.sentence || "This wake-up is not complete yet."}</span>
      {inline.map((k) => (
        <span className="su-knob-row" key={k.id}>{stepper(k)} <span>{k.label}</span></span>
      ))}
      {waitLine}
    </>
  );
}

export function WakesCard({ ctx }: { ctx: CardCtx }) {
  const { plan, edit, probs, open, openEditor, closeEditor, busy, base } = ctx;
  const editingHere = !!open?.startsWith("wakes.");
  const addId = "su-add-wakes";
  const setKnob = (key: string, id: string, v: number) => edit({
    ...plan,
    wakes: plan.wakes.map((w) => (w.key !== key ? w
      : { ...w, knobs: w.knobs.map((k) => (k.id === id ? { ...k, value: v } : k)) })),
  });
  return (
    <li className={`su-card${editingHere ? " editing" : ""}`}>
      <h2 className="su-card-n">2. Wakes when</h2>
      <Problems list={probs["wakes"]} />
      {plan.wakes.length === 0 && open !== "wakes.new" && <p className="help">Nothing wakes the project yet. Add a wake-up.</p>}
      {plan.wakes.map((w, i) => {
        const id = `wakes.${w.key}`;
        const editId = `su-edit-${id}`;
        if (open === id) {
          return (
            <WakeEditor key={w.key} wake={w} ctx={ctx}
                        onSave={(nw) => { edit(putWake(plan, nw, w.key)); closeEditor(editId); }}
                        onCancel={() => closeEditor(editId)} />
          );
        }
        return (
          <div className="su-item su-wake-item" key={w.key}>
            <WakeSentence w={w} base={base} disabled={busy} onKnob={(kid, v) => setKnob(w.key, kid, v)} />
            <ItemActions id={editId} what={plan.wakes.length > 1 ? `wake-up ${i + 1}` : "this wake-up"} disabled={busy || !!open}
                         onChange={() => openEditor(id)}
                         onRemove={() => { edit({ ...plan, wakes: plan.wakes.filter((x) => x.key !== w.key) }); closeEditor(addId); }} />
            <Problems list={probs[id]} />
          </div>
        );
      })}
      {open === "wakes.new" ? (
        <WakeEditor ctx={ctx}
                    onSave={(nw) => { edit(putWake(plan, nw)); closeEditor(`su-edit-wakes.${nw.key}`); }}
                    onCancel={() => closeEditor(addId)} />
      ) : (
        <div className="su-card-actions">
          <button type="button" id={addId} disabled={busy || !!open || plan.watches.length === 0}
                  onClick={() => openEditor("wakes.new")}>Add another wake-up</button>
        </div>
      )}
    </li>
  );
}

type Agg = NonNullable<PlanCondition["aggregate"]>;
type Op = ">" | ">=" | "<" | "<=" | "==";
const AGGS: Agg[] = ["count", "avg", "max", "min", "sum"];
const AGG_LABELS: Record<string, string> = {
  count: "the number of events", avg: "the average of a number", max: "the highest value of a number",
  min: "the lowest value of a number", sum: "the total of a number",
};
const OPS: Op[] = [">", ">=", "<", "<=", "=="];
const COUNT_OP_LABELS: Record<string, string> = { ">": "more than", ">=": "at least", "<": "fewer than", "<=": "at most", "==": "exactly" };
const VALUE_OP_LABELS: Record<string, string> = { ">": "goes above", ">=": "reaches", "<": "drops below", "<=": "drops to", "==": "is exactly" };
const FILTER_OPS: TriggerFilter["op"][] = ["eq", "neq", "contains", "gt", "gte", "lt", "lte"];
const FILTER_OP_LABELS: Record<string, string> = {
  eq: "is", neq: "is not", contains: "contains", gt: "is above", gte: "is at least", lt: "is below", lte: "is at most",
};
const minutesOf = (d: string | null | undefined, dflt: number) => {
  const m = /^(\d+(?:\.\d+)?)([smhd])$/.exec(String(d ?? "").trim());
  if (!m) return dflt;
  const n = Number(m[1]) * { s: 1 / 60, m: 1, h: 60, d: 1440 }[m[2] as "s" | "m" | "h" | "d"];
  return Math.max(1, Math.ceil(n));
};

function WakeEditor({ wake, ctx, onSave, onCancel }: {
  wake?: PlanWake; ctx: CardCtx; onSave: (w: PlanWake) => void; onCancel: () => void;
}) {
  const { plan, cell } = ctx;
  const knob = (id: string) => wake?.knobs.find((k) => k.id === id)?.value;
  const c = wake?.condition ?? {};
  const pred = /^(>=|<=|==|>|<)\s*(-?\d+(?:\.\d+)?)/.exec(String(c.predicate ?? "> 5"));
  const [key] = useState(() => wake?.key ?? newKey("k", plan.wakes.map((w) => w.key)));
  const [sources, setSources] = useState<string[]>(wake?.sources ?? (plan.watches[0] ? [plan.watches[0].name] : []));
  const [mode, setMode] = useState<"event" | "schedule">(c.every ? "schedule" : "event");
  const [agg, setAgg] = useState<Agg>((c.aggregate as Agg) ?? "count");
  const [field, setField] = useState(String(c.field ?? ""));
  const [op, setOp] = useState<Op>((pred?.[1] as Op) ?? ">");
  const [threshold, setThreshold] = useState(String(knob("threshold") ?? pred?.[2] ?? 5));
  const [windowMin, setWindowMin] = useState(String(knob("window_minutes") ?? minutesOf(c.window, 5)));
  const [every, setEvery] = useState(String(knob("every_minutes") ?? minutesOf(c.every, 60)));
  const [cooldown, setCooldown] = useState(String(knob("cooldown_minutes") ?? minutesOf(wake?.cooldown, 10)));
  const [filters, setFilters] = useState<TriggerFilter[]>(wake?.filters ?? []);
  const [keyField, setKeyField] = useState(wake?.key_field ?? "");
  const [description, setDescription] = useState(wake?.description ?? "");

  const watched = plan.watches.filter((w) => sources.includes(w.name));
  const fields = useFieldChoices(watched, cell.connectors);
  const all = fields?.all ?? [];
  const numbers = fields?.numeric.length ? fields.numeric : all;
  const count = agg === "count";

  const num = (s: string) => (s.trim() === "" ? NaN : Number(s));
  const bad = sources.length === 0 ? "Pick at least one source to watch."
    : mode === "schedule" ? (!(num(every) >= 1) ? "Say every how many minutes, 1 or more." : null)
    : !count && !field.trim() ? "Pick the number to take the value of."
    : !Number.isFinite(num(threshold)) ? "The threshold needs a number."
    : !(num(windowMin) >= 1) ? "The window is 1 minute or more."
    : !(num(cooldown) >= 0) ? "The wait before waking again is 0 minutes or more."
    : null;

  const save = () => {
    const old = Object.fromEntries((wake?.knobs ?? []).map((k) => [k.id, k]));
    const k = (id: string, value: number, label: string, min: number, max: number): PlanKnob => ({
      id, label, value,
      min: Math.min(old[id]?.min ?? min, value), max: Math.max(old[id]?.max ?? max, value),
    });
    const wait = k("cooldown_minutes", num(cooldown), "", 0, 1440);
    let condition: PlanCondition;
    let knobs: PlanKnob[];
    if (mode === "schedule") {
      condition = { every: `${num(every)}m` };
      knobs = [k("every_minutes", num(every), "", 1, 10080), wait];
    } else {
      condition = { aggregate: agg, predicate: `${op} ${num(threshold)}`, window: `${num(windowMin)}m`,
                    ...(count ? {} : { field: field.trim() }) };
      // a count keeps the unit the plan gave it ("errors"); anything else is labelled by its field
      const unit = count && (wake?.condition.aggregate ?? "count") === "count" && !wake?.condition.every
        ? (old.threshold?.label || "") : "";
      knobs = [k("threshold", num(threshold), unit, 0, count ? 100000 : 1e9),
               k("window_minutes", num(windowMin), "", 1, 1440), wait];
    }
    const name = wake?.name ?? uniqueName(
      kebab(`${sources[0] ?? "project"}-${mode === "schedule" ? "every" : count ? "spike" : agg}`) || "wake",
      plan.wakes.map((w) => w.name));
    onSave({
      key, name, sources, condition, knobs,
      filters: filters.map((f) => ({ ...f, field: f.field.trim() })),
      key_field: mode === "schedule" ? "" : keyField,
      description: description.trim(),
      cooldown: `${num(cooldown)}m`, window: wake?.window ?? "15m",
      sentence: wake?.sentence ?? "", cooldown_sentence: wake?.cooldown_sentence ?? null,
    });
  };

  const setFilter = (i: number, patch: Partial<TriggerFilter>) =>
    setFilters((cur) => cur.map((f, j) => (j === i ? { ...f, ...patch } : f)));
  const perOptions = ["", ...(fields?.labels ?? []).filter((l) => l !== fields?.primary)];
  const perLabels: Record<string, string> = {
    "": fields === undefined ? "the source's main label" : fields.primary ? `each ${fields.primary}` : "everything together",
  };
  for (const l of perOptions.slice(1)) perLabels[l] = `each ${l}`;
  if (keyField && !perOptions.includes(keyField)) { perOptions.push(keyField); perLabels[keyField] = `each ${keyField}`; }

  return (
    <EditorFrame title={wake ? "Change this wake-up" : "Add a wake-up"} onSave={save} onCancel={onCancel}
                 canSave={!bad} note={bad && <p className="help">{bad}</p>}>
      <fieldset className="su-pick-list">
        <legend className="lbl">Watch</legend>
        {plan.watches.map((w) => (
          <label key={w.key} className="su-check">
            <input type="checkbox" checked={sources.includes(w.name)}
                   onChange={(e) => setSources((cur) => (e.target.checked ? [...cur, w.name] : cur.filter((s) => s !== w.name)))} />
            <span><span className="mono">{w.name}</span>{w.sentence && w.sentence !== w.name && <span className="help"> {w.sentence}</span>}</span>
          </label>
        ))}
      </fieldset>

      <fieldset className="su-choices">
        <legend className="lbl">Wake</legend>
        <Choice name={`su-mode-${key}`} checked={mode === "event"} onChange={() => setMode("event")}
                title="When something happens">A number of events, or a value, crosses a line.</Choice>
        <Choice name={`su-mode-${key}`} checked={mode === "schedule"} onChange={() => setMode("schedule")}
                title="On a schedule">Every so many minutes, with what arrived since.</Choice>
      </fieldset>

      {mode === "event" ? (
        <>
          <div className="su-row">
            <div className="field">
              <span className="lbl">What to look at</span>
              <Picker value={agg} options={AGGS} labels={AGG_LABELS} ariaLabel="What to look at"
                      onChange={(v) => setAgg(v as Agg)} />
            </div>
            {!count && (
              <label className="field">
                <span className="lbl">Of the number</span>
                <Combo value={field} options={numbers} placeholder={numbers.length ? "pick a field" : "type a field name"}
                       onChange={setField} />
              </label>
            )}
          </div>
          <div className="su-row">
            <div className="field">
              <span className="lbl">Wake when it is</span>
              <Picker value={op} options={OPS} labels={count ? COUNT_OP_LABELS : VALUE_OP_LABELS}
                      ariaLabel="Comparison" onChange={(v) => setOp(v as Op)} />
            </div>
            <label className="field su-num-field">
              <span className="lbl">{count ? "Events" : "Value"}</span>
              <input type="number" inputMode="decimal" value={threshold} onChange={(e) => setThreshold(e.target.value)} />
            </label>
            <label className="field su-num-field">
              <span className="lbl">Within minutes</span>
              <input type="number" inputMode="numeric" min={1} value={windowMin} onChange={(e) => setWindowMin(e.target.value)} />
            </label>
          </div>
          <div className="field">
            <span className="lbl">Count separately for</span>
            <Picker value={keyField} options={perOptions} labels={perLabels} ariaLabel="Count separately for"
                    onChange={setKeyField} />
            <span className="help">
              {keyField || fields?.primary
                ? `One wake-up per ${keyField || fields?.primary}, so each is looked at on its own.`
                : "One wake-up for all the events together."}
            </span>
          </div>
        </>
      ) : (
        <label className="field su-num-field">
          <span className="lbl">Every how many minutes</span>
          <input type="number" inputMode="numeric" min={1} value={every} onChange={(e) => setEvery(e.target.value)} />
        </label>
      )}

      <fieldset className="su-filters">
        <legend className="lbl">Only when</legend>
        {filters.length === 0 && <p className="help">Every event counts. Add a condition to count only some.</p>}
        {filters.map((f, i) => (
          <div className="su-filter" key={i} role="group" aria-label={`Condition ${i + 1}`}>
            <label className="su-filter-field">
              <span className="sr-only">Field</span>
              <Combo value={f.field} options={all} placeholder="field" onChange={(v) => setFilter(i, { field: v })} />
            </label>
            <Picker value={f.op} options={FILTER_OPS} labels={FILTER_OP_LABELS} ariaLabel="Comparison"
                    onChange={(v) => setFilter(i, { op: v as TriggerFilter["op"] })} />
            <label className="su-filter-value">
              <span className="sr-only">Value</span>
              <input type="text" value={String(f.value ?? "")} placeholder="value"
                     onChange={(e) => setFilter(i, { value: e.target.value })} />
            </label>
            <button type="button" className="linklike su-small" aria-label={`Remove condition ${i + 1}`}
                    onClick={() => setFilters((cur) => cur.filter((_, j) => j !== i))}>Remove</button>
          </div>
        ))}
        <div>
          <button type="button" onClick={() => setFilters((cur) => [...cur, { field: "", op: "eq", value: "" }])}>
            Add a condition
          </button>
        </div>
      </fieldset>

      <label className="field">
        <span className="lbl">Say it in plain words (optional)</span>
        <input type="text" value={description} maxLength={160}
               placeholder="e.g. an alert fires for the checkout service"
               onChange={(e) => setDescription(e.target.value)} />
        <span className="help">What happens, as it would follow "When". The project page says this instead of the numbers.</span>
      </label>

      {mode === "event" && (
        <label className="field su-num-field">
          <span className="lbl">Then wait, in minutes, before waking again</span>
          <input type="number" inputMode="numeric" min={0} value={cooldown} onChange={(e) => setCooldown(e.target.value)} />
        </label>
      )}
    </EditorFrame>
  );
}
