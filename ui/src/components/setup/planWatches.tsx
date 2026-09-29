import { useState } from "react";

import SourceForm from "../SourceForm";
import type { PlanWatch } from "../../types";
import { Choice, EditorFrame, ItemActions, Problems } from "./planBits";
import {
  agoWords, newKey, needsFor, offeredConnector, putWatch, removeWatch, type CellData,
} from "./planEdit";
import type { CardCtx } from "./plan";

// Card 1, Watches: each source the project reads, changed in place for another source already on
// Tares or a new one (its connector and settings), removed, or added. A swap follows into the
// wake-ups that counted the old source.

export function WatchesCard({ ctx }: { ctx: CardCtx }) {
  const { plan, edit, probs, open, openEditor, closeEditor, busy, cell } = ctx;
  const editingHere = !!open?.startsWith("watches.");
  const addId = "su-add-watches";
  return (
    <li className={`su-card${editingHere ? " editing" : ""}`}>
      <h2 className="su-card-n">1. Watches</h2>
      <Problems list={probs["watches"]} />
      {plan.watches.length === 0 && open !== "watches.new" && <p className="help">Nothing to watch yet. Add a source.</p>}
      {plan.watches.map((w) => {
        const id = `watches.${w.key}`;
        const editId = `su-edit-${id}`;
        if (open === id) {
          return (
            <SourceEditor key={w.key} watch={w} cell={cell} taken={plan.watches.filter((x) => x.key !== w.key).map((x) => x.name)}
                          onSave={(nw) => { edit(putWatch(plan, nw, w.key)); closeEditor(editId); }}
                          onCancel={() => closeEditor(editId)} />
          );
        }
        return (
          <div className="su-item" key={w.key}>
            <span className="su-item-what">{w.sentence || w.name}</span>
            <span className="help">
              {w.existing ? "Already on Tares."
                : w.needs === "send" ? "You connect it in the next step."
                : w.needs === "credential" ? "Needs a credential; you add it in the next step."
                : "Tares connects it for you."}
            </span>
            <ItemActions id={editId} what={w.name || "this source"} disabled={busy || !!open}
                         onChange={() => openEditor(id)}
                         onRemove={() => { edit(removeWatch(plan, w.key)); closeEditor(addId); }} />
            <Problems list={probs[id]} />
          </div>
        );
      })}
      {open === "watches.new" ? (
        <SourceEditor cell={cell} taken={plan.watches.map((x) => x.name)}
                      onSave={(nw) => { edit(putWatch(plan, nw)); closeEditor(`su-edit-watches.${nw.key}`); }}
                      onCancel={() => closeEditor(addId)} />
      ) : (
        <div className="su-card-actions">
          <button type="button" id={addId} disabled={busy || !!open} onClick={() => openEditor("watches.new")}>
            Add a source
          </button>
        </div>
      )}
    </li>
  );
}

/** Where a source stands, in a few words: receiving, quiet, paused. */
function sourceState(s: NonNullable<CellData["sources"]>[number]): string {
  if (s.paused) return "paused";
  const last = agoWords(s.health?.last_ingest);
  return last ? `last event ${last}` : "no events yet";
}

function SourceEditor({ watch, cell, taken, onSave, onCancel }: {
  watch?: PlanWatch; cell: CellData; taken: string[];
  onSave: (w: PlanWatch) => void; onCancel: () => void;
}) {
  const specs = cell.connectors;
  const candidates = (cell.sources ?? []).filter((s) =>
    !(specs?.[s.connector]?.internal) && s.connector !== "finding" && !taken.includes(s.name));
  const [mode, setMode] = useState<"existing" | "new">(
    watch ? (watch.existing ? "existing" : "new") : "existing");
  const [pick, setPick] = useState(watch?.existing ? watch.name : "");
  const [filter, setFilter] = useState("");
  const [conn, setConn] = useState(watch && !watch.existing ? watch.connector : "");
  const [key] = useState(() => watch?.key ?? newKey("w", []));
  const spec = conn ? specs?.[conn] : undefined;
  const sameNew = !!watch && !watch.existing && watch.connector === conn;
  const title = watch ? `Change ${watch.name || "this source"}` : "Add a source";

  const pickExisting = () => {
    const s = candidates.find((x) => x.name === pick);
    if (!s) return;
    const same = !!watch?.existing && watch.name === s.name;
    onSave({ key, existing: true, name: s.name, connector: s.connector, sentence: same ? watch!.sentence : "",
             needs: "none", sample: null });
  };

  return (
    <EditorFrame title={title} onCancel={onCancel}
                 onSave={mode === "existing" ? pickExisting : undefined}
                 saveLabel={pick ? `Use ${pick}` : "Use this source"} canSave={!!pick}>
      <fieldset className="su-choices">
        <legend className="sr-only">Where the events come from</legend>
        <Choice name={`su-src-${key}`} checked={mode === "existing"} onChange={() => setMode("existing")}
                title="A source already on Tares">Its events are already arriving, or will be.</Choice>
        <Choice name={`su-src-${key}`} checked={mode === "new"} onChange={() => setMode("new")}
                title="Connect a new one">Pick the kind of source and fill in its settings.</Choice>
      </fieldset>

      {mode === "existing" && (
        cell.sources === undefined ? <p className="help">Reading the sources on Tares…</p>
        : candidates.length === 0 ? <p className="help">There is no other source on Tares yet. Connect a new one.</p>
        : (
          <fieldset className="su-pick-list">
            <legend className="lbl">Which source</legend>
            {candidates.length > 8 && (
              <input type="search" className="su-filter" aria-label="Filter the sources"
                     placeholder="Filter by name or kind" value={filter}
                     onChange={(e) => setFilter(e.target.value)} />
            )}
            {candidates.filter((s) => !filter.trim()
              || `${s.name} ${specs?.[s.connector]?.label ?? s.connector}`.toLowerCase()
                   .includes(filter.trim().toLowerCase())).map((s) => (
              <Choice key={s.name} name={`su-pick-${key}`} checked={pick === s.name} onChange={() => setPick(s.name)}
                      title={<span className="mono">{s.name}</span>}>
                {specs?.[s.connector]?.label ?? s.connector}, {sourceState(s)}
              </Choice>
            ))}
          </fieldset>
        )
      )}

      {mode === "new" && (
        !specs ? <p className="help">Reading the kinds of source…</p> : (
          <>
            <fieldset className="su-pick-list su-connectors">
              <legend className="lbl">What kind of source</legend>
              {Object.entries(specs).filter(([id, s]) => offeredConnector(id, s)).map(([id, s]) => (
                <Choice key={id} name={`su-conn-${key}`} checked={conn === id}
                        onChange={() => setConn(id)} title={s.label}>
                  {s.mode === "push" ? "Your system sends events to Tares." : "Tares collects the events itself."}
                </Choice>
              ))}
            </fieldset>
            {spec && (
              <div className="su-source-form">
                <p className="help">{spec.description}</p>
                <SourceForm key={conn} connector={conn} spec={spec}
                            initial={sameNew ? { name: watch!.name, type: "", poll: watch!.poll ?? spec.poll ?? "5s",
                                                 config: watch!.config ?? {} } : undefined}
                            submitLabel="Use this source"
                            onSubmit={async (body) => {
                              const name = body.name.trim();
                              if (!name) throw new Error("Give the source a name.");
                              if (taken.includes(name)) throw new Error(`The plan already has a source called ${name}.`);
                              // a secret typed before and left blank now (blank keeps it) stays
                              const kept: Record<string, unknown> = {};
                              if (sameNew) {
                                for (const f of spec.fields) {
                                  if (f.secret && watch!.config?.[f.name] && !body.config[f.name]) kept[f.name] = watch!.config[f.name];
                                }
                              }
                              const config = { ...kept, ...body.config };
                              onSave({
                                key, existing: false, name, connector: conn, config,
                                poll: spec.mode === "poll" ? body.poll : undefined,
                                sentence: sameNew && watch!.name === name ? watch!.sentence : "",
                                needs: needsFor(spec, config),
                                sample: sameNew ? watch!.sample : null,
                              });
                            }} />
              </div>
            )}
          </>
        )
      )}
    </EditorFrame>
  );
}
