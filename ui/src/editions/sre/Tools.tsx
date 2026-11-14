import { ListSection, StatusDot } from "../../components/edition";
import type { HomeTool } from "./Home.api";

/** "Connected": the kinds of data the investigator can read, each connected or not. */
export function Tools({ items, paper }: { items: HomeTool[]; paper?: boolean }) {
  if (items.length === 0) return null;
  return (
    <ListSection paper={paper} title="Connected" data-testid="tools">
      <div className="px-[var(--row-x,18px)] py-[18px]">
      <ul className="m-0! flex list-none! flex-wrap gap-2 p-0!">
        {items.map((t) => (
          <li key={t.name} className="flex items-center gap-1.5 border border-border px-2.5 py-1 text-[13px]" data-testid="tool" data-ok={t.ok}>
            <StatusDot tone={t.ok ? "ready" : "quiet"} />
            <span className={t.ok ? undefined : "text-muted-foreground"}>{t.name}</span>
            {!t.ok && <span className="text-xs text-muted-foreground">not connected</span>}
          </li>
        ))}
      </ul>
      </div>
    </ListSection>
  );
}
