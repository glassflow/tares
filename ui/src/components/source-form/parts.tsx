import * as React from "react";

import { Input, Select, type InputProps, type SelectProps } from "../edition";

/** The connection form's shared pieces: the class strings its sections reuse, a Select over plain string
 *  options, and a text field that suggests values. Kit parts only (TR-1394). */

export const TEXT = "m-0 text-[13px] leading-relaxed text-muted-foreground";
export const CODE = "font-mono text-[12px] text-foreground";
/** A group of controls set apart from the form around it: a sub-editor, a list of rows. */
export const BOX = "grid min-w-0 gap-3 border border-border bg-muted/40 p-4";
/** The label above a group of controls, set like the kit's own field labels. */
export const GROUP_LABEL = "m-0 text-xs font-semibold text-foreground";
export const CHIP = "inline-flex max-w-full items-center gap-1 border border-border bg-card px-1.5 py-0.5 font-mono text-xs text-foreground";
export const TABLE = "w-full border-collapse text-[13px] [&_td]:border-b [&_td]:border-border [&_td]:px-2 [&_td]:py-1.5 [&_th]:border-b [&_th]:border-border [&_th]:px-2 [&_th]:py-1.5 [&_th]:text-left [&_th]:text-xs [&_th]:font-semibold [&_th]:text-muted-foreground";

/** "dsn" reads as "Dsn"; a daemon label that is already a sentence keeps its own case. */
export const cap = (s: string) => (s ? s[0].toUpperCase() + s.slice(1) : s);

// Radix refuses "" as an item's value, and "" is how the form spells "nothing chosen".
const NONE = "__none";

export type PickProps = Omit<SelectProps, "options" | "value" | "defaultValue" | "onValueChange"> & {
  value: string;
  options: string[];
  /** What each option reads as; an option without one reads as itself, and "" as None. */
  labels?: Record<string, string>;
  onChange: (v: string) => void;
};

/** The kit Select over string options, where "" may be one of them. When "" is not offered, an empty value
 *  shows the placeholder. */
export function Pick({ value, options, labels, onChange, ...rest }: PickProps) {
  const offersNone = options.includes("");
  return (
    <Select
      {...rest}
      value={value === "" ? (offersNone ? NONE : "") : value}
      options={options.map((o) => ({ value: o === "" ? NONE : o, label: labels?.[o] ?? (o || "None") }))}
      onValueChange={(v) => onChange(v === NONE ? "" : v)}
    />
  );
}

/** A kit Input that suggests values from a list as you type (the browser's own datalist); anything typed
 *  is kept, so a value the list does not have is still allowed. A hint per option shows beside it. */
export function SuggestInput({ options, hints, ...props }: InputProps & { options: string[]; hints?: Record<string, string> }) {
  const listId = React.useId();
  return (
    <>
      <Input {...props} list={listId} autoComplete="off" data-1p-ignore data-lpignore="true" />
      <datalist id={listId}>
        {options.map((o) => <option key={o} value={o} label={hints?.[o]} />)}
      </datalist>
    </>
  );
}
