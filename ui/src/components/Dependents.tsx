import { useEffect, useState } from "react";

import { api } from "../api";
import type { ProjectObjectKind } from "../types";

export type Dependent = { kind: ProjectObjectKind; name: string };

// Inside a delete dialog: what else stops working if this object goes, and the choice to take
// it along. Deleting a source used to be refused with "remove it from those views first", which
// sent the user bottom-up through three pages; now the dialog lists the views, triggers and
// agents that depend on it and, on yes, the delete cascades in the right order.
export default function Dependents({ kind, name, cascade, onChange }: {
  kind: "source" | "view" | "trigger"; name: string;
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

  if (err) return <div className="alert error">could not check what depends on it: {err}. Deleting may break more than is shown here.</div>;
  if (!deps) return <div className="dim">checking what depends on it…</div>;
  if (deps.length === 0) return <p className="help" style={{ margin: "8px 0 0" }}>Nothing else depends on it.</p>;
  return (
    <div style={{ marginTop: 10 }}>
      <p className="help" style={{ whiteSpace: "normal", margin: "0 0 6px" }}>
        These depend on it and stop working without it:
      </p>
      <ul style={{ margin: "0 0 8px", paddingLeft: 18 }}>
        {deps.map((d) => <li key={`${d.kind}:${d.name}`}><span className="help">{d.kind}</span> <span className="mono">{d.name}</span></li>)}
      </ul>
      <label style={{ display: "flex", gap: 8, alignItems: "center" }}>
        <input type="checkbox" checked={cascade} onChange={(e) => onChange(e.target.checked, deps)} />
        <span>delete {deps.length === 1 ? "it" : `these ${deps.length}`} as well</span>
      </label>
      {!cascade && <p className="help" style={{ margin: "6px 0 0" }}>Without that, the delete is refused: they would point at nothing.</p>}
    </div>
  );
}
