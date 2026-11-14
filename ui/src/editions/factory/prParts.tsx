import * as React from "react";

import type { ProjectPr } from "./FactoryProject.api";

// What a pull request row shows on the project page and its GitHub page: the size of the change.
// Its state is a StatusLabel with the tone from STATUS_TONES.pr.

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
