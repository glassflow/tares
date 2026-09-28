import { useEffect, useState } from "react";

import { api } from "../api";
import { Combo, Picker } from "./bits";
import type { Trigger } from "../types";

// The one trigger editor — used in place on /triggers/new and /triggers/<name>.
// The condition's `field` is suggested from the selected view's sources: their NUMERIC typed
// fields (from the catalog schema), because that's what aggregates can actually compute over.

const AGGREGATES = ["max", "min", "sum", "avg", "count", "any"];

export default function TriggerEditor({ initial, prefill, presetView, onSaved, onCancel }: {
  initial?: Trigger;            // absent = create
  prefill?: boolean;            // initial is a proposal for a NEW trigger: create, editable name
  presetView?: string;          // create mode: view preselected (e.g. from a view page)
  onSaved: (name: string) => void;
  onCancel: () => void;
}) {
  const isNew = !initial || !!prefill;
  const [t, setT] = useState<Trigger>(initial ?? {
    name: "", view: presetView ?? "",
    condition: { aggregate: "max", predicate: "> 1.0", window: "1m", field: "" },
    emit: { kind: "", context_window: "15m" },
    cooldown: "5m",
  });
  const [viewNames, setViewNames] = useState<string[]>([]);
  const [viewSources, setViewSources] = useState<Record<string, string[]>>({});
  const [fieldOpts, setFieldOpts] = useState<string[]>([]);
  const [error, setError] = useState<string>();
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api.views().then((vs) => {
      setViewNames(vs.map((v) => v.name));
      setViewSources(Object.fromEntries(vs.map((v) => [v.name, v.sources])));
    }).catch(() => {});
  }, []);

  // numeric fields the selected view's events actually carry — the only valid aggregate targets
  useEffect(() => {
    const srcs = viewSources[t.view] ?? [];
    if (!srcs.length) { setFieldOpts([]); return; }
    let live = true;
    Promise.all(srcs.map((s) => api.describe(`source:${s}`).catch(() => null))).then((descs) => {
      if (!live) return;
      const nums = new Set<string>();
      for (const d of descs) {
        for (const [fname, ftype] of Object.entries(d?.schema?.fields ?? {})) {
          if (ftype === "number") nums.add(fname);
        }
      }
      setFieldOpts(Array.from(nums).sort());
    });
    return () => { live = false; };
  }, [t.view, Object.keys(viewSources).length]);

  const save = async () => {
    setBusy(true);
    setError(undefined);
    try {
      if (isNew) await api.createTrigger(toSave());
      else await api.updateTrigger(initial!.name, toSave());
      onSaved(t.name);
    } catch (e) { setError(String((e as Error).message ?? e)); }
    setBusy(false);
  };

  const cond = (patch: Partial<Trigger["condition"]>) =>
    setT({ ...t, condition: { ...t.condition, ...patch } });

  // "on a schedule" fires every interval for the whole view (TR-320); the condition fields then
  // mean nothing, so the saved condition carries only the schedule
  const scheduled = !!t.condition.every;
  const [summaryText, setSummaryText] = useState((t.condition.summary_by ?? []).join(", "));
  const setMode = (m: string) => {
    if (m === "schedule") setT({ ...t, condition: { ...t.condition, every: t.condition.every || "10m" } });
    else {
      const { every: _e, summary_by: _s, ...rest } = t.condition;
      setT({ ...t, condition: { ...rest, aggregate: rest.aggregate || "max",
                                predicate: rest.predicate || "> 1.0", window: rest.window || "1m" } });
    }
  };
  const toSave = (): Trigger => scheduled
    ? { ...t, condition: { every: t.condition.every, aggregate: "count", predicate: "> 0",
                           window: t.condition.every!,
                           summary_by: summaryText.split(",").map((x) => x.trim()).filter(Boolean) } }
    : t;

  return (
    <div className="panel">
      {error && <div className="alert error">{error}</div>}
      <div className="row2">
        <label className="field">
          <span className="lbl">name</span>
          <input type="text" value={t.name} disabled={!isNew} placeholder="e.g. error_spike"
                 onChange={(e) => setT({ ...t, name: e.target.value })} />
        </label>
        <div className="field">
          <span className="lbl">view</span>
          <Combo value={t.view} options={viewNames}
                 placeholder="the view this condition watches"
                 onChange={(v) => setT({ ...t, view: v })} />
        </div>
      </div>
      <div className="row2">
        <div className="field">
          <span className="lbl">fires</span>
          <Picker value={scheduled ? "schedule" : "condition"} options={["condition", "schedule"]}
                  labels={{ condition: "when a condition holds", schedule: "on a schedule" }}
                  ariaLabel="fires" onChange={setMode} />
          <span className="help">
            {scheduled
              ? "once every interval for the whole view, whether or not anything happened; the agent gets counts for the window"
              : "for each entity whose events match the condition, at most once per cooldown"}
          </span>
        </div>
      </div>
      {scheduled ? (
        <div className="row2">
          <label className="field">
            <span className="lbl">run every</span>
            <input type="text" value={t.condition.every ?? ""} onChange={(e) => cond({ every: e.target.value })} />
            <span className="help">e.g. 10m; at least 1m</span>
          </label>
          <label className="field">
            <span className="lbl">count by</span>
            <input type="text" value={summaryText} placeholder="e.g. service, status_code"
                   onChange={(e) => setSummaryText(e.target.value)} />
            <span className="help">labels the agent gets counts for, this window against the last; empty = the view's key</span>
          </label>
        </div>
      ) : (<>
      <div className="row2">
        <div className="field">
          <span className="lbl">aggregate</span>
          <Picker value={t.condition.aggregate} options={AGGREGATES} ariaLabel="aggregate"
                  onChange={(v) => cond({ aggregate: v })} />
        </div>
        <div className="field">
          <span className="lbl">field</span>
          <Combo value={t.condition.field ?? ""} options={fieldOpts}
                 placeholder={fieldOpts.length ? `e.g. ${fieldOpts[0]}` : "numeric field (empty for count)"}
                 onChange={(v) => cond({ field: v })} />
          <span className="help">
            {t.view
              ? fieldOpts.length
                ? `${fieldOpts.length} numeric field${fieldOpts.length === 1 ? "" : "s"} in this view; click the box to pick. Leave empty to count events.`
                : "no numeric fields found in this view's events yet; leave empty to count events"
              : "pick a view first; its numeric fields will be suggested here"}
          </span>
        </div>
      </div>
      <div className="row2">
        <label className="field">
          <span className="lbl">predicate</span>
          <input type="text" value={t.condition.predicate} onChange={(e) => cond({ predicate: e.target.value })} />
          <span className="help">e.g. &gt; 1.0, &gt;= 100, == 0</span>
        </label>
        <label className="field">
          <span className="lbl">window</span>
          <input type="text" value={t.condition.window} onChange={(e) => cond({ window: e.target.value })} />
          <span className="help">detection window, e.g. 1m</span>
        </label>
      </div>
      <div className="row2">
        <label className="field">
          <span className="lbl">context window</span>
          <input type="text" value={String(t.emit.context_window ?? "15m")}
                 onChange={(e) => setT({ ...t, emit: { ...t.emit, context_window: e.target.value } })} />
          <span className="help">how much timeline the woken agent receives</span>
        </label>
        <label className="field">
          <span className="lbl">cooldown</span>
          <input type="text" value={t.cooldown} onChange={(e) => setT({ ...t, cooldown: e.target.value })} />
          <span className="help">minimum gap between firings per entity</span>
        </label>
      </div>
      </>)}
      <div className="btnrow">
        <button className="primary" onClick={save} disabled={busy || !t.name.trim() || !t.view.trim()}>
          {isNew ? "Create trigger" : "Save changes"}
        </button>
        <button onClick={onCancel}>Cancel</button>
      </div>
    </div>
  );
}
