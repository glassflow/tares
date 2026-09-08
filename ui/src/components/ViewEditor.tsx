import { useEffect, useState } from "react";

import { api } from "../api";
import { Combo, Picker } from "./bits";
import type { ConnectorSpec, View, ViewFilter } from "../types";

// The one view editor, used in place: on /views/new (create) and on /views/<name> (edit).
// Sources are picked one at a time — chips above, an add-picker below — so the control scales
// with any number of sources instead of a wall of checkboxes.

const NUMERIC_OPS = new Set(["gt", "lt", "gte", "lte"]);

export default function ViewEditor({ initial, prefill, sourceNames, onSaved, onCancel }: {
  initial?: View;                     // absent = create
  prefill?: boolean;                  // initial is a proposal for a NEW view: create, editable name
  sourceNames: string[];
  onSaved: (name: string) => void;
  onCancel: () => void;
}) {
  const isNew = !initial || !!prefill;
  const [name, setName] = useState(initial?.name ?? "");
  const [keyField, setKeyField] = useState(initial?.key_field ?? "");
  const [sources, setSources] = useState<string[]>(initial?.sources ?? []);
  const [pick, setPick] = useState("");
  const [fRows, setFRows] = useState(
    (initial?.filters ?? []).map((f) => ({ field: f.field, op: f.op as string, value: String(f.value) })));
  const [labelOpts, setLabelOpts] = useState<string[]>([]);
  const [fieldOpts, setFieldOpts] = useState<string[]>([]);
  const [srcTypes, setSrcTypes] = useState<Record<string, string>>({});  // source name -> connector, for the picker tag
  const [error, setError] = useState<string>();
  const [busy, setBusy] = useState(false);

  // connector type per source, so the picker shows a type tag next to each name
  useEffect(() => {
    let live = true;
    api.sources().then((all) => {
      if (live) setSrcTypes(Object.fromEntries(all.map((s) => [s.name, s.connector])));
    }).catch(() => {});
    return () => { live = false; };
  }, []);

  // The key field must be a LABEL name (what extract_labels produces), never a raw payload
  // field — so suggest labels from the source definitions: config.labels plus any labels the
  // connector synthesizes (`provides`). Keys should be shared, so intersect across the selected
  // sources (ignoring sources that declare no labels rather than zeroing the intersection).
  // Filters accept both namespaces (label first, raw field second) — list labels, then raw names.
  useEffect(() => {
    if (!sources.length) { setLabelOpts([]); setFieldOpts([]); return; }
    let live = true;
    Promise.all([
      api.sources().catch(() => []),
      api.connectors().catch(() => ({}) as Record<string, ConnectorSpec>),
      Promise.all(sources.map((s) => api.sourceFields(s).catch(() => null))),
    ]).then(([all, specs, profiles]) => {
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
      setFieldOpts([...[...union].sort(), ...[...raw].filter((n) => !union.has(n)).sort()]);
    });
    return () => { live = false; };
  }, [sources.join("|")]);

  const addSource = (s: string) => {
    if (sourceNames.includes(s) && !sources.includes(s)) setSources([...sources, s]);
    setPick("");
  };
  const remaining = sourceNames.filter((s) => !sources.includes(s));

  const save = async () => {
    setBusy(true);
    setError(undefined);
    const filters: ViewFilter[] = fRows.filter((r) => r.field.trim())
      .map((r) => ({ field: r.field.trim(), op: r.op as ViewFilter["op"],
                     value: NUMERIC_OPS.has(r.op) ? Number(r.value) : r.value }));
    const body: View = { name: name.trim(), key_field: keyField.trim(), sources, filters };
    try {
      if (isNew) await api.createView(body);
      else await api.updateView(initial!.name, body);
      onSaved(body.name);
    } catch (e) { setError(String((e as Error).message ?? e)); }
    setBusy(false);
  };

  return (
    <div className="panel">
      {error && <div className="alert error">{error}</div>}

      <label className="field" style={{ maxWidth: 340 }}>
        <span className="lbl">name</span>
        <input type="text" value={name} disabled={!isNew} placeholder="e.g. service_timeline"
               onChange={(e) => setName(e.target.value)} />
        <span className="help">agents query it by this name</span>
      </label>

      <div className="field">
        <span className="lbl">sources to correlate</span>
        <span className="help" style={{ display: "block", marginTop: 0, marginBottom: 6 }}>
          events from every selected source merge into one time-ordered timeline
        </span>
        {sources.length > 0 && (
          <div style={{ marginBottom: 6 }}>
            {sources.map((s) => (
              <span className="chip mono" key={s} style={{ paddingRight: 4 }}>
                {s}{" "}
                <button type="button" onClick={() => setSources(sources.filter((x) => x !== s))}
                        style={{ border: "none", background: "none", padding: "0 2px", cursor: "pointer",
                                 color: "var(--err)" }}
                        title={`remove ${s}`}>×</button>
              </span>
            ))}
          </div>
        )}
        {remaining.length > 0 ? (
          <Combo style={{ maxWidth: 340 }} value={pick} options={remaining}
                 placeholder={sources.length ? "add another source…" : "add a source…"}
                 hints={srcTypes} hintClass="chip"
                 onChange={(v) => (remaining.includes(v) ? addSource(v) : setPick(v))} />
        ) : sourceNames.length === 0 ? (
          <span className="help">no sources yet; add one under Sources first</span>
        ) : (
          <span className="help">all sources selected</span>
        )}
      </div>

      <div className="field" style={{ marginTop: 14 }}>
        <span className="lbl">key field</span>
        <span className="help" style={{ display: "block", marginTop: 0, marginBottom: 6 }}>
          the label agents look an entity up by; pick one the selected sources share
        </span>
        <Combo value={keyField} options={labelOpts} style={{ maxWidth: 340 }}
               placeholder={labelOpts.length ? `e.g. ${labelOpts[0]}` : "e.g. service"}
               onChange={setKeyField} />
        {labelOpts.length > 0 && (
          <div style={{ marginTop: 6 }}>
            {labelOpts.map((f) => (
              <button key={f} type="button" className="chip"
                      style={{ cursor: "pointer", marginRight: 6,
                               outline: keyField === f ? "2px solid var(--accent)" : undefined }}
                      onClick={() => setKeyField(f)}>
                {f}
              </button>
            ))}
          </div>
        )}
      </div>

      <div className="field">
        <span className="lbl">filters (optional)</span>
        <span className="help" style={{ display: "block", marginTop: 0, marginBottom: 6 }}>
          applied on reads and trigger evaluation
        </span>
        {fRows.map((r, i) => (
          <div key={i} className="btnrow" style={{ marginBottom: 6, alignItems: "center" }}>
            <Combo value={r.field} options={fieldOpts} placeholder="field"
                   style={{ maxWidth: 200, flex: 1 }}
                   onChange={(v) => setFRows(fRows.map((x, j) => j === i ? { ...x, field: v } : x))} />
            {/* Picker, not <select>: the native menu is drawn by the OS and can't take our theme. */}
            <Picker value={r.op} style={{ maxWidth: 130 }} ariaLabel="filter operator"
                    options={["eq", "neq", "contains", "gt", "lt", "gte", "lte"]}
                    onChange={(v) => setFRows(fRows.map((x, j) => j === i ? { ...x, op: v } : x))} />
            <input type="text" className="mono" style={{ maxWidth: 180 }} placeholder="value" value={r.value}
                   onChange={(e) => setFRows(fRows.map((x, j) => j === i ? { ...x, value: e.target.value } : x))} />
            <button type="button" className="danger" onClick={() => setFRows(fRows.filter((_, j) => j !== i))}>×</button>
          </div>
        ))}
        <button type="button" onClick={() => setFRows([...fRows, { field: "", op: "eq", value: "" }])}>
          + Add filter
        </button>
      </div>

      <div className="btnrow">
        <button className="primary" onClick={save}
                disabled={busy || !name.trim() || !keyField.trim() || !sources.length}>
          {isNew ? "Create view" : "Save changes"}
        </button>
        <button onClick={onCancel}>Cancel</button>
      </div>
    </div>
  );
}
