import * as React from "react";

/** The label, hint and error that every form control shares. */
export interface FieldProps {
  /** What the control is for. Required: every control has a visible label. */
  label: string;
  /** One line that helps fill it in. */
  hint?: string;
  /** One sentence saying what is wrong. Shown under the control in the error colour. */
  error?: string;
}

export function useFieldIds(id?: string) {
  const auto = React.useId();
  const base = id ?? auto;
  return { id: base, hintId: `${base}-hint`, errorId: `${base}-error` };
}

type Ids = ReturnType<typeof useFieldIds>;

/** The control's aria attributes: described by the hint and the error, invalid when there is an error. */
export function fieldAria(ids: Ids, { hint, error }: Pick<FieldProps, "hint" | "error">) {
  const describedBy = [hint && ids.hintId, error && ids.errorId].filter(Boolean).join(" ");
  return { id: ids.id, "aria-describedby": describedBy || undefined, "aria-invalid": error ? true : undefined };
}

export function FieldNote({ ids, hint, error }: { ids: Ids } & Pick<FieldProps, "hint" | "error">) {
  return (
    <>
      {hint && (
        <p id={ids.hintId} className="m-0 text-xs text-muted-foreground">
          {hint}
        </p>
      )}
      {error && (
        <p id={ids.errorId} className="m-0 text-xs text-[color:var(--err)]">
          {error}
        </p>
      )}
    </>
  );
}

/** Label above the control, hint and error below: Input, Textarea, Select. */
export function StackedField({ ids, label, hint, error, children }: { ids: Ids; children: React.ReactNode } & FieldProps) {
  return (
    <div className="grid gap-1.5">
      <label htmlFor={ids.id} className="text-xs font-semibold text-foreground">
        {label}
      </label>
      {children}
      <FieldNote ids={ids} hint={hint} error={error} />
    </div>
  );
}

/** Control then label, hint and error beside it: Switch, Checkbox. */
export function InlineField({ ids, label, hint, error, children }: { ids: Ids; children: React.ReactNode } & FieldProps) {
  return (
    <div className="grid grid-cols-[auto_1fr] items-start gap-x-3 gap-y-1">
      {children}
      <label htmlFor={ids.id} className="cursor-pointer text-sm font-medium leading-5 text-foreground">
        {label}
      </label>
      <div className="col-start-2 grid gap-1">
        <FieldNote ids={ids} hint={hint} error={error} />
      </div>
    </div>
  );
}

export const ERROR_BORDER = "border-[color:var(--err)]! hover:border-[color:var(--err)]!";
