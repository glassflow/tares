import { useEffect, useMemo, useState } from "react";

import { api } from "../api";
import type { TriggerBody } from "../api";
import { Combo, Picker } from "./bits";
import type { ConnectorSpec, Source, Trigger, TriggerFilter } from "../types";

// The one trigger editor, used in place: on /triggers/new, on /triggers/<name> and inside a
// project's page. A trigger belongs to one project and reads that project's sources: it counts the
// events that pass its filters, per entity (the entity label), and fires when the condition holds
// or on a schedule.
//
// Sources are picked one at a time from the project's own; a source from outside the project can
// be added too, and saving the trigger makes it a member of the project.
// The condition's `field` is suggested from the selected sources' NUMERIC typed fields (from the
// catalog schema), because that's what aggregates can actually compute over.

const AGGREGATES = ["max", "min", "sum", "avg", "count", "any"];
const OPS: TriggerFilter["op"][] = ["eq", "neq", "in", "gt", "lt", "gte", "lte", "contains"];
const OP_LABELS: Record<string, string> = {
  eq: "equals", neq: "is not", gt: "greater than", lt: "less than",
  gte: "at least", lte: "at most", contains: "contains", in: "is one of",
};
const NUMERIC_OPS = new Set(["gt", "lt", "gte", "lte"]);
// What a GitHub source's events carry (the GitHub event contract), offered as filter values. A
// token source polls commits and pull request state only; the App's webhooks carry everything.
const GH_APP_VALUES: Record<string, string[]> = {
  event_type: ["pull_request", "push", "pull_request_review", "pull_request_review_comment",
               "issues", "issue_comment", "release", "workflow_run"],
  action: ["opened", "synchronize", "merged", "closed", "reopened", "ready_for_review",
           "submitted", "created", "published", "completed", "pushed"],
};
const GH_TOKEN_VALUES: Record<string, string[]> = {
  event_type: ["commit", "pull_request"],
  action: ["opened", "merged", "closed"],
};

export default function TriggerEditor({ initial, prefill, project, onSaved, onCancel }: {
  initial?: Trigger;            // absent = create
  prefill?: boolean;            // initial is a proposal for a NEW trigger: create, editable name
  project?: string;             // the project it is made in (create); edit keeps initial.project
  onSaved: (name: string) => void;
  onCancel: () => void;
}) {
  const isNew = !initial || !!prefill;
  const projectId = project ?? initial?.project ?? "";
  const [t, setT] = useState<Trigger>(initial ?? {
    name: "", sources: [], filters: [], key_field: "",
    condition: { aggregate: "max", predicate: "> 1.0", window: "1m", field: "" },
    emit: { kind: "", context_window: "15m" },
    cooldown: "5m",
  });
  const [fRows, setFRows] = useState(
    (initial?.filters ?? []).map((f) => ({ field: f.field, op: f.op as string,
                                          value: Array.isArray(f.value) ? f.value.join(", ") : String(f.value) })));
  const [all, setAll] = useState<Source[]>([]);
  const [pick, setPick] = useState("");
  const [outside, setOutside] = useState(false);
  const [labelOpts, setLabelOpts] = useState<string[]>([]);
  const [filterOpts, setFilterOpts] = useState<string[]>([]);
  const [fieldOpts, setFieldOpts] = useState<string[]>([]);
  const [error, setError] = useState<string>();
  const [busy, setBusy] = useState(false);
  const sources = t.sources ?? [];

  useEffect(() => {
    let live = true;
    api.sources().then((s) => { if (live) setAll(s); }).catch(() => {});
    return () => { live = false; };
  }, []);

  const srcTypes = useMemo(() => Object.fromEntries(all.map((s) => [s.name, s.connector])), [all]);
  // GitHub sources selected: filter values come from the event contract, not free text
  const ghValues = useMemo(() => {
    const kinds = new Set(sources.map((s) => srcTypes[s]));
    if (kinds.has("github_app")) return GH_APP_VALUES;
    if (kinds.has("github")) return GH_TOKEN_VALUES;
    return undefined;
  }, [sources, srcTypes]);
  // with no project known (an older daemon), every source counts as the project's own
  const members = useMemo(() => all.filter((s) => !projectId || !s.projects || s.projects.includes(projectId))
    .map((s) => s.name), [all, projectId]);
  const remaining = members.filter((s) => !sources.includes(s));
  const others = all.map((s) => s.name).filter((s) => !members.includes(s) && !sources.includes(s));

  // Labels the selected sources declare (config.labels plus what the connector synthesizes). The
  // entity label should be shared, so intersect across sources (ignoring ones that declare none).
  // Filters accept labels first, raw payload fields second; aggregates want the numeric fields.
  useEffect(() => {
    if (!sources.length) { setLabelOpts([]); setFilterOpts([]); setFieldOpts([]); return; }
    let live = true;
    Promise.all([
      api.connectors().catch(() => ({}) as Record<string, ConnectorSpec>),
      Promise.all(sources.map((s) => api.sourceFields(s).catch(() => null))),
      Promise.all(sources.map((s) => api.describe(`source:${s}`).catch(() => null))),
    ]).then(([specs, profiles, descs]) => {
      if (!live) return;
      const perSource = sources.map((name) => {
        const src = all.find((s) => s.name === name);
        const declared = ((src?.config?.labels as { name?: string }[] | undefined) ?? [])
          .map((l) => l.name).filter((n): n is string => !!n);
        const provided = (src && specs[src.connector]?.provides?.map((p) => p.name)) ?? [];
        return new Set([...declared, ...provided]);
      }).filter((s) => s.size > 0);
      const shared = perSource.length
        ? [...perSource[0]].filter((n) => perSource.every((s) => s.has(n)))
        : [];
      const union = new Set(perSource.flatMap((s) => [...s]));
      const raw = new Set<string>();
      for (const p of profiles) for (const f of p?.fields ?? []) raw.add(f.name);
      setLabelOpts((shared.length ? shared : [...union]).sort());
      // GitHub events are told apart by event_type (a built-in column, not a label): offer it first
      const github = sources.some((n) => ["github", "github_app"].includes(all.find((s) => s.name === n)?.connector ?? ""));
      setFilterOpts([...(github ? ["event_type"] : []), ...[...union].sort(),
                     ...[...raw].filter((n) => !union.has(n) && n !== "event_type").sort()]);
      const nums = new Set<string>();
      for (const d of descs) {
        for (const [fname, ftype] of Object.entries(d?.schema?.fields ?? {})) {
          if (ftype === "number") nums.add(fname);
        }
      }
      setFieldOpts(Array.from(nums).sort());
    });
    return () => { live = false; };
  }, [sources.join("|"), all.length]);

  const addSource = (s: string) => {
    if (!sources.includes(s)) setT({ ...t, sources: [...sources, s] });
    setPick("");
  };

  const cond = (patch: Partial<Trigger["condition"]>) =>
    setT({ ...t, condition: { ...t.condition, ...patch } });

  // "on a schedule" fires every interval for the whole trigger (TR-320); the condition fields then
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
  const toSave = (): TriggerBody => {
    const filters: TriggerFilter[] = fRows.filter((r) => r.field.trim())
      .map((r) => ({ field: r.field.trim(), op: r.op as TriggerFilter["op"],
                     value: r.op === "in" ? r.value.split(",").map((v) => v.trim()).filter(Boolean)
                       : NUMERIC_OPS.has(r.op) && r.value.trim() !== "" && !isNaN(Number(r.value))
                       ? Number(r.value) : r.value }));
    const condition = scheduled
      ? { every: t.condition.every, aggregate: "count", predicate: "> 0", window: t.condition.every!,
          summary_by: summaryText.split(",").map((x) => x.trim()).filter(Boolean) }
      : t.condition;
    return {
      name: t.name.trim(), sources, filters, key_field: (t.key_field ?? "").trim() || null,
      description: (t.description ?? "").trim(),
      condition, emit: t.emit, cooldown: t.cooldown,
      ...(projectId ? { project: projectId } : {}),
    };
  };

  const save = async () => {
    setBusy(true);
    setError(undefined);
    try {
      const body = toSave();
      if (isNew) await api.createTrigger(body);
      else await api.updateTrigger(initial!.name, body);
      onSaved(body.name);
    } catch (e) { setError(String((e as Error).message ?? e)); }
    setBusy(false);
  };

  return (
    <div className="panel">
      {error && <div className="alert error">{error}</div>}
      <label className="field" style={{ maxWidth: 340 }}>
        <span className="lbl">name</span>
        <input type="text" value={t.name} disabled={!isNew} placeholder="e.g. error_spike"
               onChange={(e) => setT({ ...t, name: e.target.value })} />
      </label>

      <label className="field">
        <span className="lbl">in plain words (optional)</span>
        <input type="text" value={t.description ?? ""} maxLength={160}
               placeholder="e.g. an alert fires for the checkout service"
               onChange={(e) => setT({ ...t, description: e.target.value })} />
        <span className="help">
          what happens, as it would follow "When". The project page says this instead of the condition.
        </span>
      </label>

      <div className="field">
        <span className="lbl">sources</span>
        <span className="help" style={{ display: "block", marginTop: 0, marginBottom: 6 }}>
          the events this trigger watches; events from every source merge into one timeline per entity
        </span>
        {sources.length > 0 && (
          <div style={{ marginBottom: 6 }}>
            {sources.map((s) => (
              <span className="chip mono" key={s} style={{ paddingRight: 4 }}>
                {s}{" "}
                <button type="button" onClick={() => setT({ ...t, sources: sources.filter((x) => x !== s) })}
                        style={{ border: "none", background: "none", padding: "0 2px", cursor: "pointer",
                                 color: "var(--err)" }}
                        title={`remove ${s}`}>×</button>
              </span>
            ))}
          </div>
        )}
        {remaining.length > 0 ? (
          <Combo style={{ maxWidth: 340 }} value={outside ? "" : pick} options={remaining}
                 placeholder={sources.length ? "add another source…" : "add a source…"}
                 hints={srcTypes} hintClass="chip"
                 onChange={(v) => (remaining.includes(v) ? addSource(v) : setPick(v))} />
        ) : (
          <span className="help">
            {members.length === 0 ? "this project has no sources yet" : "all of this project's sources are selected"}
          </span>
        )}
        {others.length > 0 && (outside ? (
          <div style={{ marginTop: 6 }}>
            <Combo style={{ maxWidth: 340 }} value={pick} options={others}
                   placeholder="a source from another project…"
                   hints={srcTypes} hintClass="chip"
                   onChange={(v) => (others.includes(v) ? addSource(v) : setPick(v))} />
            <span className="help">it is added to this project when you save; its other projects keep it too</span>
          </div>
        ) : (
          <button type="button" className="linklike" style={{ marginTop: 6, display: "block" }}
                  onClick={() => { setOutside(true); setPick(""); }}>
            Use a source from outside this project
          </button>
        ))}
      </div>

      <div className="field">
        <span className="lbl">filters (optional)</span>
        <span className="help" style={{ display: "block", marginTop: 0, marginBottom: 6 }}>
          only events that match every row count
        </span>
        {fRows.map((r, i) => (
          <div key={i} className="btnrow" style={{ marginBottom: 6, alignItems: "center" }}>
            <Combo value={r.field} options={filterOpts} placeholder="label"
                   style={{ maxWidth: 200, flex: 1 }}
                   onChange={(v) => setFRows(fRows.map((x, j) => j === i ? { ...x, field: v } : x))} />
            <Picker value={r.op} style={{ maxWidth: 150 }} ariaLabel="filter operator"
                    options={OPS} labels={OP_LABELS}
                    onChange={(v) => setFRows(fRows.map((x, j) => j === i ? { ...x, op: v } : x))} />
            {ghValues?.[r.field.trim()] && r.op !== "in" ? (
              <Combo value={r.value} options={ghValues[r.field.trim()]} placeholder="value"
                     style={{ maxWidth: 180, flex: 1 }}
                     onChange={(v) => setFRows(fRows.map((x, j) => j === i ? { ...x, value: v } : x))} />
            ) : (
              <input type="text" className="mono" style={{ maxWidth: 180 }}
                     placeholder={r.op === "in" ? "a, b, c" : "value"} value={r.value}
                     onChange={(e) => setFRows(fRows.map((x, j) => j === i ? { ...x, value: e.target.value } : x))} />
            )}
            <button type="button" className="danger" aria-label="remove filter"
                    onClick={() => setFRows(fRows.filter((_, j) => j !== i))}>×</button>
          </div>
        ))}
        <button type="button" onClick={() => setFRows([...fRows, { field: "", op: "eq", value: "" }])}>
          + Add filter
        </button>
      </div>

      <div className="field" style={{ maxWidth: 340 }}>
        <span className="lbl">entity label (optional)</span>
        <Combo value={t.key_field ?? ""} options={labelOpts}
               placeholder={labelOpts.length ? `e.g. ${labelOpts[0]}` : "the first source's main label"}
               onChange={(v) => setT({ ...t, key_field: v })} />
        <span className="help">
          what a firing is about, e.g. service: the condition is checked per value of this label.
          Empty uses the first source's main label.
        </span>
      </div>

      <div className="row2">
        <div className="field">
          <span className="lbl">fires</span>
          <Picker value={scheduled ? "schedule" : "condition"} options={["condition", "schedule"]}
                  labels={{ condition: "when a condition holds", schedule: "on a schedule" }}
                  ariaLabel="fires" onChange={setMode} />
          <span className="help">
            {scheduled
              ? "once every interval, whether or not anything happened; the agent gets counts for the window"
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
            <span className="help">labels the agent gets counts for, this window against the last; empty = the entity label</span>
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
            {sources.length
              ? fieldOpts.length
                ? `${fieldOpts.length} numeric field${fieldOpts.length === 1 ? "" : "s"} in these sources; click the box to pick. Leave empty to count events.`
                : "no numeric fields found in these sources' events yet; leave empty to count events"
              : "pick a source first; its numeric fields will be suggested here"}
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
        <button className="primary" onClick={save} disabled={busy || !t.name.trim() || !sources.length}>
          {isNew ? "Create trigger" : "Save changes"}
        </button>
        <button onClick={onCancel}>Cancel</button>
      </div>
    </div>
  );
}
