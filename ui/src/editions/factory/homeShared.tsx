import * as React from "react";

import { ApiError, isLicenceRequired } from "../../api";
import { licenceSentence } from "@/components/LicenceNeeded";
import type { HomeData } from "./home.types";

// What Home and the view for a person joining (JoinerStart) both draw with: the page's columns, the
// heading of a section, and the sentence of a refusal.

export const PAGE = "flex min-w-0 flex-col gap-6 md:gap-8";
export const COLS = "flex flex-wrap items-start gap-8";
export const LEFT = "flex min-w-0 flex-[2_1_560px] flex-col gap-8";
export const RIGHT = "flex min-w-0 flex-[1_1_300px] flex-col gap-6";
export const MUTED = "text-xs text-muted-foreground";

/** A cell with more than one person sends these keys; a cell of one never does. */
export const isTeam = (d: HomeData): boolean => d.summary !== undefined;

export function SectionHead({ title, aside }: { title: string; aside?: React.ReactNode }) {
  return (
    <div className="mb-2.5 flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1 border-t border-border pt-3">
      <h2 className="m-0 text-base">{title}</h2>
      {aside}
    </div>
  );
}

/** The sentence of a refusal, or a plain one when the cell could not be reached. */
export function reason(e: unknown): string {
  if (isLicenceRequired(e)) return licenceSentence();   // plain words under the row, never the raw answer
  return e instanceof ApiError && e.message ? e.message : "Could not reach the cell. Try again in a moment.";
}
