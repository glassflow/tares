import { useEffect, useState } from "react";

import { api } from "../api";
import { Banner, Checkbox } from "./edition";
import type { ProjectObjectKind } from "../types";

export type Dependent = { kind: ProjectObjectKind; name: string };

// What each kind is called on the page (editions/words.json). "rule" is both editions' word: the sre
// edition's "check rule" names the same object.
const KIND_WORDS: Record<ProjectObjectKind, string> = {
  source: "connection", // words-ok
  trigger: "rule", // words-ok
  agent: "agent",
  mcp_server: "MCP server",
};

// Inside a delete dialog: what else stops working if this object goes, and the choice to take
// it along. The dialog lists the rules and agents that depend on it and, on yes, the delete
// cascades in the right order.
export default function Dependents({ kind, name, cascade, onChange }: {
  kind: "source" | "trigger"; name: string; // words-ok
  cascade: boolean; onChange: (cascade: boolean, deps: Dependent[]) => void;
}) {
  const [deps, setDeps] = useState<Dependent[]>();
  const [err, setErr] = useState<string>();
  useEffect(() => {
    let live = true;
    api.dependents(kind, name)
      .then((r) => { if (live) { setDeps(r.dependents); onChange(true, r.dependents); } })
      .catch((e) => { if (live) setErr(String((e as Error).message ?? e)); });
    return () => { live = false; };
  }, [kind, name]);   // eslint-disable-line react-hooks/exhaustive-deps

  if (err) return <Banner tone="error">Tares could not check what depends on it: {err}. Deleting may break more than is shown here.</Banner>;
  if (!deps) return <p className="m-0 text-sm text-muted-foreground">Checking what depends on it…</p>;
  if (deps.length === 0) return <p className="m-0 text-sm text-muted-foreground">Nothing else depends on it.</p>;
  return (
    <div className="grid gap-2" data-testid="dependents">
      <p className="m-0 text-sm text-muted-foreground">These depend on it and stop working without it:</p>
      <ul className="m-0 grid list-none gap-1 p-0 text-sm">
        {deps.map((d) => (
          <li key={`${d.kind}:${d.name}`} className="flex min-w-0 flex-wrap items-baseline gap-x-2">
            <span className="text-muted-foreground">{KIND_WORDS[d.kind] ?? d.kind}</span>
            <span className="break-all font-mono" data-user-text>{d.name}</span>
          </li>
        ))}
      </ul>
      <Checkbox checked={cascade} onCheckedChange={(c) => onChange(c === true, deps)} data-testid="dependents-cascade"
                label={deps.length === 1 ? "Delete it as well" : `Delete these ${deps.length} as well`} />
      {!cascade && <p className="m-0 text-sm text-muted-foreground">Without that, the delete is refused: they would point at nothing.</p>}
    </div>
  );
}
