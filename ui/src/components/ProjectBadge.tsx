import { useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { api } from "../api";
import type { Project } from "../types";

// "Part of project <name>": the chip an object wears when a project created it. Ownership is a
// badge, not a lock: the object stays editable and deletable on its own page. `customized` means
// it was edited by hand since, so the project keeps that version on its next re-plan.
//
// The object rows only carry the project id; the name comes from /api/projects, fetched once per
// page and shared through a small module cache so five badges on one page cost one request.

let cache: { at: number; list: Project[] } | undefined;
let inflight: Promise<Project[]> | undefined;

async function projectList(): Promise<Project[]> {
  if (cache && Date.now() - cache.at < 15_000) return cache.list;
  if (!inflight) {
    inflight = api.projects().then((r) => {
      cache = { at: Date.now(), list: r.projects };
      inflight = undefined;
      return r.projects;
    }).catch(() => { inflight = undefined; return cache?.list ?? []; });
  }
  return inflight;
}

export function useProjectName(id: string | null | undefined): Project | undefined {
  const [uc, setUc] = useState<Project>();
  useEffect(() => {
    if (!id) { setUc(undefined); return; }
    let live = true;
    projectList().then((list) => { if (live) setUc(list.find((u) => u.id === id)); });
    return () => { live = false; };
  }, [id]);
  return uc;
}

export default function ProjectBadge({ ownedBy, customized, compact }: {
  ownedBy: string | null | undefined;
  customized?: boolean;
  compact?: boolean;   // table cells: just the chip, no lead-in
}) {
  const uc = useProjectName(ownedBy);
  if (!ownedBy) return null;
  const label = uc?.name ?? "a project";
  return (
    <span className="uc-badge" title={customized
      ? "created by a project, then edited here; the project keeps your version"
      : "created by a project; edit or delete it here like any other object"}>
      <Link to={`/projects/${encodeURIComponent(ownedBy)}`} className="chip">
        {compact ? label : `Part of project ${label}`}
      </Link>
      {customized && <span className="help">customized</span>}
    </span>
  );
}

/** A project's name as a link to its page: the Project column of the global lists. */
export function ProjectLink({ id }: { id: string | null | undefined }) {
  const uc = useProjectName(id);
  if (!id) return <span className="dim">none</span>;
  return <Link to={`/projects/${encodeURIComponent(id)}`}>{uc?.name ?? id}</Link>;
}
