import * as React from "react";
import { Link, useParams } from "react-router-dom";

import { Banner, Button, EmptyState, Skeleton } from "../../components/edition";
import { cn } from "../../lib/utils";
import { usePagePolling } from "../usePagePolling";
import { factoryProject, type FactoryProjectData } from "./FactoryProject.api";
import { DecisionRow } from "./FactoryProject";
import { InlineIcon } from "./ProjectIcon";

// /projects/:id/decisions: every decision of one project on its own page, newest first. The
// project page shows the newest few and links here.

const POLL_MS = 15000;
const MUTED = "text-xs text-muted-foreground";
const LINK = "text-foreground! underline!";

export default function FactoryDecisions() {
  const { id = "" } = useParams();
  const { data, error, reload } = usePagePolling<FactoryProjectData>(() => factoryProject(id), POLL_MS);
  const crumb = (
    <div className={MUTED} data-testid="crumb">
      <Link to="/projects" className={LINK}>Projects</Link>
      {data && <> / <Link to={`/projects/${encodeURIComponent(id)}`} className={LINK} data-user-text><InlineIcon icon={data.icon} className="mr-1" />{data.name}</Link></>}
      {" / "}Decisions
    </div>
  );
  if (!data) {
    return (
      <div className="grid min-w-0 gap-3" data-testid="decisions-page-wait">
        {crumb}
        {error ? (
          <Banner tone="error" action={<Button variant="secondary" onClick={reload}>Retry</Button>}>
            <b>We could not load the decisions.</b> <span className="text-foreground">Check your connection, then try again.</span>
          </Banner>
        ) : <Skeleton className="h-40" />}
      </div>
    );
  }
  const items = data.decisions;
  return (
    <div className="grid min-w-0 gap-4" data-testid="decisions-page">
      {crumb}
      <div>
        <h1 className="m-0! text-[22px]! md:text-[28px]!">Decisions</h1>
        <p className={cn(MUTED, "m-0! mt-1!")}>Your answers to the crew on <span data-user-text>{data.name}</span>, newest first.</p>
      </div>
      {items.length === 0 ? (
        <EmptyState className="border-t border-border py-4" data-testid="decisions-empty">
          No decisions yet. When you answer the crew, it is kept here.
        </EmptyState>
      ) : (
        <div className="border-t border-border" data-testid="decisions-list">
          {items.map((x, i) => <DecisionRow key={i} x={x} wide />)}
        </div>
      )}
    </div>
  );
}
