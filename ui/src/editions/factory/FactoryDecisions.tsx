import * as React from "react";
import { Link } from "react-router-dom";

import { EmptyState } from "../../components/edition";
import { cn } from "../../lib/utils";
import type { FactoryProjectData } from "./FactoryProject.api";
import { DecisionRow } from "./FactoryProject";
import { PageHead, ProjectShell } from "./ProjectShell";

// /projects/:id/decisions, Crew questions in the project's frame (TR-1245, named in TR-1444), the
// Answered by you tab: every answer you gave the crew on the project, newest first. The Answered by the
// crew tab is /projects/:id/calls.

/** The two tabs of Crew questions, each its own address. */
export function DecisionTabs({ d, on }: { d: FactoryProjectData; on: "decisions" | "calls" }) {
  const base = `/projects/${encodeURIComponent(d.id)}`;
  const tabs = [
    { key: "calls", label: "Answered by the crew", n: d.calls.length, to: `${base}/calls` },
    { key: "decisions", label: "Answered by you", n: d.decisions.length, to: `${base}/decisions` },
  ] as const;
  return (
    <nav aria-label="Crew questions" className="flex min-w-0 gap-1 border-b border-border" data-testid="decision-tabs">
      {tabs.map((t) => (
        <Link key={t.key} to={t.to} aria-current={on === t.key ? "page" : undefined} data-testid={`tab-${t.key}`}
              className={cn("-mb-px flex items-center gap-2 border-b-2 border-transparent px-3 py-2 text-[14px] text-foreground no-underline hover:bg-row-hover",
                            on === t.key && "border-b-brand font-semibold")}>
          {t.label}
          {t.n > 0 && <span className="font-mono text-xs font-normal text-muted-foreground">{t.key === "calls" ? `${t.n} to confirm` : t.n}</span>}
        </Link>
      ))}
    </nav>
  );
}

export default function FactoryDecisions() {
  return (
    <ProjectShell page="decisions">
      {({ d }) => (
        <div className="grid min-w-0 gap-4" data-testid="decisions-page">
          <PageHead title="Crew questions" />
          <DecisionTabs d={d} on="decisions" />
          <p className="m-0 text-xs text-muted-foreground">What you told the crew when it asked, newest first.</p>
          {d.decisions.length === 0 ? (
            <EmptyState className="border-t border-border py-4" data-testid="decisions-empty">
              No answers yet. When you answer the crew, it is kept here.
            </EmptyState>
          ) : (
            <div className="border-t border-border" data-testid="decisions-list">
              {d.decisions.map((x, i) => <DecisionRow key={i} x={x} wide />)}
            </div>
          )}
        </div>
      )}
    </ProjectShell>
  );
}
