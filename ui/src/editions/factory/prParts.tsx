import * as React from "react";

import type { PrState, ProjectPr } from "./FactoryProject.api";

// What a pull request row shows on the project page and its GitHub page: the state chip and the
// size of the change.

export const PR_CHIP: Record<PrState, string> = {
  open: "border-[#e3c777] bg-[var(--warn-soft)] text-[color:var(--accent-deep)]",
  draft: "border-border bg-muted text-muted-foreground",
  merged: "border-[color:var(--ok-border)] bg-[var(--ok-soft)] text-[color:var(--ok)]",
  closed: "border-border bg-muted text-muted-foreground",
};

export const n = (x: number) => x.toLocaleString("en-US");

function count(k: number, one: string, many = one + "s") {
  return `${k} ${k === 1 ? one : many}`;
}

export function PrSize({ s }: { s: NonNullable<ProjectPr["size"]> }) {
  return (
    <span data-testid="pr-size">
      {count(s.files, "file")}{" "}
      <span className="text-[color:var(--ok)]">+{n(s.additions)}</span>{" "}
      <span className="text-[color:var(--err)]">{"−"}{n(s.deletions)}</span>
    </span>
  );
}
