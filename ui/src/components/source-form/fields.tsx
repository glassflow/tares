import { useEffect, useMemo, useState } from "react";

import { formatCount } from "../../lib/number";
import type { ConnectorField } from "../../types";
import { Button, EmptyState, Input } from "../edition";
import type { Attachment } from "./labels";
import { BOX, CHIP, CODE, GROUP_LABEL, TEXT, cap } from "./parts";

/** The connection form's editors for values that are lists: name and value pairs, rows of a list
 *  field, and the documents of a reference connection (TR-1394). Kit parts only. */

/** Key-value rows for a `map` field, kept in the form as the JSON text the save path already reads.
 *  Empty keys are dropped on the way out, so a half-filled row costs nothing. */
export function KeyValueRows({ value, noun, onChange }: { value: string; noun: string; onChange: (v: string) => void }) {
  const parsed = useMemo<[string, string][]>(() => {
    try { const o = JSON.parse(value || "{}"); return typeof o === "object" && o ? Object.entries(o).map(([k, v]) => [k, String(v)]) : []; }
    catch { return []; }
  }, [value]);
  // local rows so a key being typed is not dropped as an empty entry mid-keystroke
  const [rows, setRows] = useState<[string, string][]>(parsed);
  useEffect(() => { if (JSON.stringify(parsed) !== JSON.stringify(rows.filter(([k]) => k.trim()))) setRows(parsed); }, [parsed]);   // eslint-disable-line react-hooks/exhaustive-deps
  const emit = (next: [string, string][]) => {
    setRows(next);
    const kept = next.filter(([k]) => k.trim());
    onChange(kept.length ? JSON.stringify(Object.fromEntries(kept)) : "");
  };
  const Noun = cap(noun);
  return (
    <div className="grid min-w-0 gap-3" data-testid="kv-rows">
      {rows.map(([k, v], i) => (
        <div className="grid items-end gap-3 sm:grid-cols-[minmax(0,1fr)_minmax(0,1fr)_auto]" key={i} data-testid="kv-row">
          <Input label={`${Noun} name`} value={k} className="font-mono" spellCheck={false}
                 onChange={(e) => emit(rows.map((r, j) => (j === i ? [e.target.value, r[1]] : r)))} />
          <Input label="Value" value={v} className="font-mono" spellCheck={false}
                 onChange={(e) => emit(rows.map((r, j) => (j === i ? [r[0], e.target.value] : r)))} />
          <Button variant="quiet" className="text-err hover:text-err" aria-label={`Remove the ${noun} ${k || i + 1}`}
                  onClick={() => emit(rows.filter((_, j) => j !== i))}>Remove</Button>
        </div>
      ))}
      <div><Button variant="secondary" onClick={() => emit([...rows, ["", ""]])}>Add {noun}</Button></div>
    </div>
  );
}

export function ListFieldEditor({ field, rows, onChange }:
  { field: ConnectorField; rows: Array<Record<string, string>>; onChange: (r: Array<Record<string, string>>) => void }) {
  const sub = field.item ?? [];
  const blank = () => Object.fromEntries(sub.map((sf) => [sf.name, ""]));
  const setCell = (i: number, name: string, val: string) =>
    onChange(rows.map((r, j) => (j === i ? { ...r, [name]: val } : r)));
  const noun = field.name === "queries" ? "query" : "row";

  return (
    <div className="grid min-w-0 gap-3" data-testid={`list-field-${field.name}`}>
      <div className="grid gap-1.5">
        <p className={GROUP_LABEL}>{cap(field.name)}{field.required && " (required)"}</p>
        {field.help && <p className="m-0 text-xs text-muted-foreground">{field.help}</p>}
      </div>
      {rows.length === 0 && <EmptyState>No {noun === "query" ? "queries" : "rows"} yet. Add one below.</EmptyState>}
      {rows.map((row, i) => (
        <div key={i} className={BOX}>
          <div className="flex items-center justify-between gap-2">
            <span className="font-mono text-xs text-muted-foreground">{cap(noun)} {i + 1}</span>
            <Button variant="quiet" className="text-err hover:text-err" aria-label={`Remove ${noun} ${i + 1}`}
                    onClick={() => onChange(rows.filter((_, j) => j !== i))}>Remove</Button>
          </div>
          {sub.map((sf) => (
            <Input key={sf.name} label={cap(sf.name) + (sf.required ? " (required)" : "")} hint={sf.help}
                   type={sf.type === "number" ? "number" : "text"} value={row[sf.name] ?? ""}
                   className={sf.name === "promql" ? "font-mono" : undefined} spellCheck={sf.name === "promql" ? false : undefined}
                   onChange={(e) => setCell(i, sf.name, e.target.value)} />
          ))}
        </div>
      ))}
      <div><Button variant="secondary" onClick={() => onChange([...rows, blank()])}>Add {noun}</Button></div>
    </div>
  );
}

const ACCEPT = ".json,.csv,.md,.txt,.markdown,.text";

/** The kit's file field. Module-level, not inside ReferenceForm: a component defined inside another
 *  re-mounts on every parent render, which detaches the input mid-dialog and drops the selection. */
function RefUpload({ onFiles, label }: { onFiles: (f: FileList | null) => void; label: string }) {
  return (
    <Input type="file" label={label} hint="json, csv, md or txt files." accept={ACCEPT} multiple
           className="h-auto cursor-pointer py-1.5 file:mr-3 file:border-0 file:bg-transparent file:text-sm file:font-semibold file:text-foreground"
           data-testid="ref-upload"
           onChange={(e) => { onFiles(e.target.files); e.target.value = ""; }} />
  );
}

/** Reference connector: upload documents (json/csv/md/txt), attach per-file labels. No manual data
 *  entry; files are read in the browser into text. The stored connection mirrors this list
 *  (declarative), so re-opening and adding a file extends it. */
export function ReferenceForm({ attachments, setAttachments }: {
  attachments: Attachment[]; setAttachments: (fn: (prev: Attachment[]) => Attachment[]) => void;
}) {
  // which (attachment, label) pairs are open for editing; a filled label shows read-only + Edit
  const [editing, setEditing] = useState<Set<string>>(new Set());
  const openEdit = (id: string, on: boolean) =>
    setEditing((prev) => { const n = new Set(prev); if (on) n.add(id); else n.delete(id); return n; });

  const onFiles = (files: FileList | null) => {
    Array.from(files ?? []).forEach((f) => {
      const r = new FileReader();
      r.onload = () => {
        const ext = (f.name.split(".").pop() ?? "txt").toLowerCase();
        const format = ["json", "csv", "md", "txt"].includes(ext) ? ext : (ext === "markdown" ? "md" : "txt");
        setAttachments((prev) => [...prev,
          { name: f.name, format, content: String(r.result ?? ""), labels: [] }]);
      };
      r.readAsText(f);
    });
  };
  const patch = (i: number, labels: [string, string][]) =>
    setAttachments((prev) => prev.map((a, j) => (j === i ? { ...a, labels } : a)));
  const remove = (i: number) => setAttachments((prev) => prev.filter((_, j) => j !== i));
  const setPair = (i: number, li: number, next: [string, string]) =>
    patch(i, attachments[i].labels.map((p, j) => (j === li ? next : p)));
  const addPair = (i: number) => {
    patch(i, [...attachments[i].labels, ["", ""]]);
    openEdit(`${i}:${attachments[i].labels.length}`, true);
  };
  const delPair = (i: number, li: number) => patch(i, attachments[i].labels.filter((_, j) => j !== li));

  return (
    <div className="grid min-w-0 gap-3" data-testid="reference-form">
      <p className={GROUP_LABEL}>Documents</p>
      <p className={TEXT}>
        Tag each document with the labels of what it describes (for example <code className={CODE}>service=tares</code>).
        Those labels become Tares labels you can correlate on, and the document is always attached to what
        they name, with no time window.
      </p>

      {attachments.length === 0 ? (
        <div className="grid gap-3 border border-dashed border-border p-6">
          <RefUpload onFiles={onFiles} label="Upload documents" />
        </div>
      ) : (
        <>
          {attachments.map((a, i) => (
            <div key={i} className={BOX} data-testid="ref-row">
              <div className="flex flex-wrap items-baseline justify-between gap-2">
                <div className="grid min-w-0 gap-0.5">
                  <span className="break-all font-mono text-sm font-semibold" data-user-text>{a.name}</span>
                  <span className="font-mono text-xs text-muted-foreground">{a.format}, {formatCount(a.content.length)} characters</span>
                </div>
                <Button variant="quiet" className="text-err hover:text-err" aria-label={`Remove ${a.name}`}
                        onClick={() => remove(i)}>Remove</Button>
              </div>
              <div className="grid gap-2">
                {a.labels.map(([k, v], li) => {
                  const id = `${i}:${li}`;
                  const isEditing = editing.has(id) || !k.trim();
                  return isEditing ? (
                    <div key={li} className="grid items-end gap-3 sm:grid-cols-[minmax(0,1fr)_minmax(0,1fr)_auto_auto]" data-testid="ref-pair">
                      <Input label="Label" value={k} className="font-mono" spellCheck={false} autoFocus
                             onChange={(e) => setPair(i, li, [e.target.value, v])} />
                      <Input label="Value" value={v} className="font-mono" spellCheck={false}
                             onChange={(e) => setPair(i, li, [k, e.target.value])} />
                      <Button variant="secondary" onClick={() => openEdit(id, false)}>Done</Button>
                      <Button variant="quiet" className="text-err hover:text-err" aria-label={`Remove the label ${k || li + 1}`}
                              onClick={() => delPair(i, li)}>Remove</Button>
                    </div>
                  ) : (
                    <span key={li} className="flex flex-wrap items-center gap-1">
                      <span className={CHIP} data-user-text>{k}={v || <span className="text-muted-foreground">(empty)</span>}</span>
                      <Button variant="quiet" aria-label={`Edit the label ${k}`} onClick={() => openEdit(id, true)}>Edit</Button>
                    </span>
                  );
                })}
                <div><Button variant="secondary" onClick={() => addPair(i)}>Add label</Button></div>
              </div>
            </div>
          ))}
          <RefUpload onFiles={onFiles} label="Upload more" />
        </>
      )}
    </div>
  );
}

/** A JSON value as a tree: each field of an object folds on its own, scalars inline. */
export function JsonTree({ value, depth = 0 }: { value: unknown; depth?: number }) {
  if (value === null || typeof value !== "object") {
    return <span className="json-scalar font-mono">{JSON.stringify(value)}</span>;
  }
  const entries: [string, unknown][] = Array.isArray(value)
    ? value.map((v, i) => [String(i), v] as [string, unknown])
    : Object.entries(value as Record<string, unknown>);
  if (entries.length === 0) return <span className="font-mono">{Array.isArray(value) ? "[]" : "{}"}</span>;
  return (
    <div className={"grid gap-0.5 font-mono text-xs" + (depth ? " border-l border-border pl-3" : "")}>
      {entries.map(([k, v]) => {
        const nested = v !== null && typeof v === "object" && Object.keys(v as object).length > 0;
        return nested ? (
          <details key={k} open={depth === 0}>
            <summary className="cursor-pointer">
              <span className="font-semibold text-foreground">{k}</span>{" "}
              <span className="text-muted-foreground">{Array.isArray(v) ? `[${(v as unknown[]).length}]` : `{${Object.keys(v as object).length}}`}</span>
            </summary>
            <JsonTree value={v} depth={depth + 1} />
          </details>
        ) : (
          <div key={k} className="break-all"><span className="font-semibold text-foreground">{k}</span>: <JsonTree value={v} depth={depth + 1} /></div>
        );
      })}
    </div>
  );
}
