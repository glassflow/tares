import * as React from "react";

import { Button, Checkbox, Input } from "../../components/edition";
import { cn } from "../../lib/utils";
import { ago } from "./brainText";
import type { FolderScan } from "./BrainFolders.api";
import { ADD_FOLDER_LABEL, checkFolder, FOLDERS_LEGEND, filesWords, foundWords, LOOKING_WORDS, NONE_FOUND_WORDS, PARTIAL_WORDS } from "./brainFoldersText";

// The decision folders of one repo (TR-1454), one list used in two places: under a ticked repo on
// Connect a repo, before it is connected, and in the Decision folders dialog of a Ready card, after. It
// shows the folders already chosen (with who chose them and when), the ones Tares found in the repo's git
// tree, and the ones typed in; `selected` is the ticked paths, and the caller sends that list. Paths, names
// and the cell's words are a person's or the cell's own text and marked data-user-text.

const MUTED = "m-0 text-[13px] text-muted-foreground";

export interface FolderListProps {
  /** The cell's answer; null while it is on its way. */
  scan: FolderScan | null;
  /** The read failed: the cell's words. Typing a folder still works. */
  readError?: string;
  selected: readonly string[];
  onChange: (next: string[]) => void;
  disabled?: boolean;
  /** The clock the "3 days ago" words count from. */
  now: number;
  "data-testid"?: string;
}

interface Row { path: string; meta: string }

export function FolderList({ scan, readError, selected, onChange, disabled, now, "data-testid": testId }: FolderListProps) {
  const [typed, setTyped] = React.useState("");
  const [problem, setProblem] = React.useState("");
  const [extra, setExtra] = React.useState<string[]>([]);   // typed here, so they stay in the list when unticked

  const rows: Row[] = [];
  const seen = new Set<string>();
  const add = (path: string, meta: string) => { if (!seen.has(path)) { seen.add(path); rows.push({ path, meta }); } };
  for (const c of scan?.folders ?? []) {
    const who = c.chosen_by_name ? `Chosen by ${c.chosen_by_name}` : "Chosen";
    const when = ago(c.chosen_at, now);
    add(c.path, [filesWords(c.files), [who, when].filter(Boolean).join(" ")].filter(Boolean).join(", "));
  }
  for (const s of scan?.suggested ?? []) add(s.path, foundWords(s.files));
  for (const p of [...extra, ...selected]) add(p, "Added by you");

  const toggle = (path: string, on: boolean) => {
    const next = on ? [...selected.filter((p) => p !== path), path] : selected.filter((p) => p !== path);
    onChange(next);
  };
  const addTyped = () => {
    const r = checkFolder(typed, selected);
    if ("problem" in r) { setProblem(r.problem); return; }
    // a folder already in the list (found by Tares, say) is ticked; a new one joins the list ticked
    if (!seen.has(r.path)) setExtra((e) => [...e, r.path]);
    onChange([...selected, r.path]);
    setTyped(""); setProblem("");
  };

  return (
    <fieldset className="m-0 mt-2.5 min-w-0 border border-dotted border-border px-3 pb-3 pt-1" data-testid={testId}>
      <legend className="px-1 text-xs text-muted-foreground">{FOLDERS_LEGEND}</legend>
      {scan === null && !readError && <p className={MUTED} role="status" data-testid={testId && `${testId}-looking`}>{LOOKING_WORDS}</p>}
      {readError && <p className="m-0 text-[13px]" data-user-text data-testid={testId && `${testId}-read-error`}>{readError}</p>}
      {scan && !scan.scanned && scan.scan_error && (
        <p className="m-0 text-[13px]" data-user-text data-testid={testId && `${testId}-scan-error`}>{scan.scan_error}</p>
      )}
      {scan?.partial && <p className={MUTED} data-testid={testId && `${testId}-partial`}>{PARTIAL_WORDS}</p>}
      {scan && scan.scanned && rows.length === 0 && <p className={MUTED} data-testid={testId && `${testId}-none`}>{NONE_FOUND_WORDS}</p>}
      {rows.length > 0 && (
        <ul className="m-0 grid list-none gap-1.5 p-0 pt-1" data-testid={testId && `${testId}-rows`}>
          {rows.map((r) => (
            <li key={r.path} className="min-w-0 [overflow-wrap:anywhere]" data-testid={testId && `${testId}-row`} data-path={r.path}>
              <Checkbox label={r.path} hint={r.meta} checked={selected.includes(r.path)} disabled={disabled}
                        onCheckedChange={(v) => toggle(r.path, v === true)} data-testid={testId && `${testId}-check`} />
            </li>
          ))}
        </ul>
      )}
      <div className={cn("mt-2.5 flex min-w-0 flex-wrap items-end gap-2")}>
        <div className="min-w-0 flex-1 basis-48">
          <Input label={ADD_FOLDER_LABEL} value={typed} placeholder="docs/decisions" disabled={disabled} error={problem || undefined}
                 onChange={(e) => { setTyped(e.target.value); setProblem(""); }}
                 onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); addTyped(); } }}
                 className="font-mono text-[13px]" data-testid={testId && `${testId}-add`} />
        </div>
        <Button variant="secondary" disabled={disabled || !typed.trim()} onClick={addTyped} data-testid={testId && `${testId}-add-button`}>Add</Button>
      </div>
    </fieldset>
  );
}
