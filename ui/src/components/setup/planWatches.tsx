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
              {cell.connectors?.[w.connector]?.label && `${cell.connectors[w.connector].label}. `}
              {w.existing ? `Already on Tares as ${w.name}.`
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

const NEW = "__new__";

/** What a source reads, when its settings say it plainly (a GitHub source: its repository);
 *  a name like ctx_glassflow_rius_argus_core tells a person less than glassflow/argus-core. */
function whatItReads(s: NonNullable<CellData["sources"]>[number]): string | null {
  const repo = s.config?.repo;
  return typeof repo === "string" && repo.trim() ? repo.trim() : null;
}

/** A source in the plan changed or added, kind first: the kind the plan needs (a GitHub source
 *  for commits), the sources of that kind already on Tares, or a new one of that kind; another
 *  kind is one link away. Adding a source starts from the list of kinds. */
function SourceEditor({ watch, cell, taken, onSave, onCancel }: {
  watch?: PlanWatch; cell: CellData; taken: string[];
  onSave: (w: PlanWatch) => void; onCancel: () => void;
}) {
  const specs = cell.connectors;
  const usable = (cell.sources ?? []).filter((s) =>
    !(specs?.[s.connector]?.internal) && s.connector !== "finding" && !taken.includes(s.name));
  const [kind, setKind] = useState(watch?.connector ?? "");
  const [choosingKind, setChoosingKind] = useState(!watch?.connector);
  const ofKind = usable.filter((s) => s.connector === kind);
  const [pick, setPick] = useState(watch ? (watch.existing ? watch.name : NEW) : "");
  const [filter, setFilter] = useState("");
  const [key] = useState(() => watch?.key ?? newKey("w", []));
  const spec = kind ? specs?.[kind] : undefined;
  const sameNew = !!watch && !watch.existing && watch.connector === kind;
  const title = watch ? `Change ${watch.name || "this source"}` : "Add a source";
  // no source of this kind yet: connecting a new one is the only choice
  const choice = !choosingKind && cell.sources !== undefined && ofKind.length === 0 ? NEW : pick;

  const pickExisting = () => {
    const s = usable.find((x) => x.name === choice);
    if (!s) return;
    const same = !!watch?.existing && watch.name === s.name;
    onSave({ key, existing: true, name: s.name, connector: s.connector, sentence: same ? watch!.sentence : "",
             needs: "none", sample: null });
  };
  const chooseKind = (id: string) => { setKind(id); setChoosingKind(false); setPick(""); setFilter(""); };
  const kindLabel = spec?.label ?? kind;

  return (
    <EditorFrame title={title} onCancel={onCancel}
                 onSave={!choosingKind && choice && choice !== NEW ? pickExisting : undefined}
                 saveLabel={choice && choice !== NEW
                   ? `Use ${(() => { const s = usable.find((x) => x.name === choice); return (s && whatItReads(s)) || choice; })()}`
                   : "Use this source"}
                 canSave={!!choice && choice !== NEW}>
      {choosingKind ? (
        !specs ? <p className="help">Reading the kinds of source…</p> : (
          <fieldset className="su-pick-list su-connectors">
            <legend className="lbl">What kind of source</legend>
            {Object.entries(specs).filter(([id, s]) => offeredConnector(id, s)).map(([id, s]) => {
              const n = usable.filter((x) => x.connector === id).length;
              return (
                <Choice key={id} name={`su-kind-${key}`} checked={kind === id} onChange={() => chooseKind(id)}
                        title={s.label}>
                  {s.mode === "push" ? "Your system sends events to Tares." : "Tares collects the events itself."}
                  {n > 0 && ` ${n} already on Tares.`}
                </Choice>
              );
            })}
          </fieldset>
        )
      ) : (
        <>
          <p className="su-kind-line">
            <span>This watch needs a <strong>{kindLabel}</strong> source.</span>{" "}
            <button type="button" className="linklike su-small" onClick={() => setChoosingKind(true)}>
              Use another kind of source
            </button>
          </p>
          {cell.sources === undefined ? <p className="help">Reading the sources on Tares…</p> : (
            <fieldset className="su-pick-list">
              <legend className="lbl">
                {ofKind.length ? `Your ${kindLabel} sources` : `There is no ${kindLabel} source on Tares yet`}
              </legend>
              {ofKind.length > 8 && (
                <input type="search" className="su-filter" aria-label="Filter the sources"
                       placeholder="Filter by name" value={filter} onChange={(e) => setFilter(e.target.value)} />
              )}
              {ofKind.filter((s) => !filter.trim() || s.name.toLowerCase().includes(filter.trim().toLowerCase()))
                .map((s) => (
                  <Choice key={s.name} name={`su-pick-${key}`} checked={choice === s.name} onChange={() => setPick(s.name)}
                          title={whatItReads(s) ?? <span className="mono">{s.name}</span>}>
                    {whatItReads(s) && <><span className="mono">{s.name}</span>, </>}{sourceState(s)}
                  </Choice>
                ))}
              {ofKind.length > 0 && (
                <Choice name={`su-pick-${key}`} checked={choice === NEW} onChange={() => setPick(NEW)}
                        title={`Connect a new ${kindLabel} source`}>
                  Fill in its settings here.
                </Choice>
              )}
            </fieldset>
          )}
          {choice === NEW && spec && (
            <div className="su-source-form">
              <p className="help">{spec.description}</p>
              <SourceForm key={kind} connector={kind} spec={spec}
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
                              key, existing: false, name, connector: kind, config,
                              poll: spec.mode === "poll" ? body.poll : undefined,
                              sentence: sameNew && watch!.name === name ? watch!.sentence : "",
                              needs: needsFor(spec, config),
                              sample: sameNew ? watch!.sample : null,
                            });
                          }} />
            </div>
          )}
        </>
      )}
    </EditorFrame>
  );
}
