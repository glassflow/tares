import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";

import { api } from "../api";
import type { Project } from "../types";
import { Picker } from "./bits";
import ConfirmDialog from "./ConfirmDialog";

// Triggers and agents live inside a project, so the global lists' "New" buttons ask which one
// first. The Default project (the one GET /api/projects marks `default`) is preselected.
export default function NewInProject({ label, what, to }: {
  label: string;                        // the button, e.g. "Add trigger"
  what: string;                         // what is being made, for the dialog, e.g. "trigger"
  to: (project: string) => string;      // where to go once a project is picked
}) {
  const nav = useNavigate();
  const [open, setOpen] = useState(false);
  const [projects, setProjects] = useState<Project[]>();
  const [err, setErr] = useState<string>();
  const [pick, setPick] = useState("");

  useEffect(() => {
    if (!open) return;
    let live = true;
    api.projects().then((r) => {
      if (!live) return;
      setProjects(r.projects);
      setPick((cur) => cur || (r.projects.find((p) => p.default) ?? r.projects[0])?.id || "");
    }).catch((e) => { if (live) setErr(String((e as Error).message ?? e)); });
    return () => { live = false; };
  }, [open]);

  const list = projects ?? [];
  return (
    <>
      <button className="primary" onClick={() => setOpen(true)}>{label}</button>
      {open && (
        <ConfirmDialog
          title={`Which project is this ${what} for?`}
          message={`A ${what} belongs to one project and works with that project's sources.`}
          confirmLabel="Continue"
          onCancel={() => setOpen(false)}
          onConfirm={() => { if (pick) nav(to(pick)); }}>
          {err && <div className="alert error" style={{ marginTop: 10 }}>{err}</div>}
          {!projects && !err && <div className="dim" style={{ marginTop: 10 }}>loading projects…</div>}
          {projects && (
            <div className="field" style={{ marginTop: 10 }}>
              <span className="lbl">project</span>
              <Picker value={pick} onChange={setPick} ariaLabel="project"
                      options={list.map((p) => p.id)}
                      labels={Object.fromEntries(list.map((p) => [p.id, p.default ? `${p.name} (default)` : p.name]))} />
            </div>
          )}
        </ConfirmDialog>
      )}
    </>
  );
}
