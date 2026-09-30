import { useEffect, useState } from "react";
import { Navigate, useParams } from "react-router-dom";

import { api } from "../api";
import { ErrorState, usePolling } from "../components/bits";
import type { Template } from "../types";
import ProjectShell from "./ProjectShell";

// The route behind /projects/:id. Fetches the summary and the template's description (for its
// declared actions) and hands both to ProjectShell, the one page every project shares. key={id}
// remounts the shell between projects so no tab, form or dialog state leaks from one to the next,
// and the stale-id guard keeps project A's data from rendering under project B's URL.
export default function ProjectDetail() {
  const { id = "" } = useParams();
  const { data: s, error, reload } = usePolling(() => api.projectSummary(id), 10000);
  const [template, setTemplate] = useState<Template>();
  useEffect(() => { reload(); }, [id]);  // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (!s?.template) return;
    api.templates().then((r) => setTemplate(r.templates.find((x) => x.key === s.template))).catch(() => {});
  }, [s?.template]);

  if (error && !s) return <ErrorState error={error} what="this project" onRetry={reload} />;
  if (!s || s.id !== id) return <div className="dim">loading…</div>;
  // a draft has no page yet: it opens where its setup stopped
  if (s.status === "draft") return <Navigate to={`/projects/${encodeURIComponent(id)}/setup`} replace />;
  return (
    <>
      {error && <ErrorState error={error} what="the latest state" onRetry={reload} />}
      <ProjectShell key={id} s={s} id={id} reload={reload} template={template} />
    </>
  );
}
