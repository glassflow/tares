import * as React from "react";
import { Link } from "react-router-dom";

import { cn } from "../../lib/utils";
import type { FactoryProjectData } from "./FactoryProject.api";
import { DecisionRow } from "./FactoryProject";
import { PageHead, ProjectShell } from "./ProjectShell";

// /projects/:id/decisions, Decisions and calls in the project's frame (TR-1245), Decisions tab:
// every decision of the project, newest first. The Calls tab is /projects/:id/calls.

/** The two tabs of Decisions and calls, each its own address. */
export function DecisionTabs({ d, on }: { d: FactoryProjectData; on: "decisions" | "calls" }) {
  const base = `/projects/${encodeURIComponent(d.id)}`;
  const tabs = [
    { key: "decisions", label: "Decisions", n: d.decisions.length, to: `${base}/decisions` },
    { key: "calls", label: "Calls", n: d.calls.length, to: `${base}/calls` },
  ] as const;
  return (
    <nav aria-label="Decisions and calls" className="flex min-w-0 gap-1 border-b border-border" data-testid="decision-tabs">
      {tabs.map((t) => (
        <Link key={t.key} to={t.to} aria-current={on === t.key ? "page" : undefined} data-testid={`tab-${t.key}`}
              className={cn("-mb-px flex items-center gap-2 border-b-2 border-transparent px-3 py-2 text-[14px] text-foreground! no-underline! hover:bg-[var(--row-hover)]",
                            on === t.key && "border-b-[color:var(--accent)] font-semibold")}>
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
          <PageHead title="Decisions and calls" />
          <DecisionTabs d={d} on="decisions" />
          <p className="m-0! text-xs text-muted-foreground">Your answers to the crew, newest first.</p>
          {d.decisions.length === 0 ? (
            <div className="border border-border bg-card px-4 py-5 text-muted-foreground" data-testid="decisions-empty">
              No decisions yet. When you answer the crew, it is kept here.
            </div>
          ) : (
            <div className="grid gap-3" data-testid="decisions-list">
              {d.decisions.map((x, i) => (
                <div key={i} className="border border-border bg-card shadow-[0_1px_3px_rgba(0,0,0,0.07)]">
                  <DecisionRow x={x} wide />
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </ProjectShell>
  );
}
