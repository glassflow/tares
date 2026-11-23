/* vendored from glassflow/arche-design paper-kit-v2 b0f2a89c9b746ebbc4a4bd6968c128ccd414afb2: do not edit, run npm run paper:sync */
import * as React from "react";

import { Input as BaseInput } from "./ui/input";
import { cn } from "./lib/cn";
import { ERROR_BORDER, fieldAria, StackedField, useFieldIds, type FieldProps } from "./Field";

export interface InputProps extends React.ComponentProps<"input">, FieldProps {}

/** `data-testid` lands on the input itself. */
export const Input = React.forwardRef<HTMLInputElement, InputProps>(({ label, hint, error, id, className, ...props }, ref) => {
  const ids = useFieldIds(id);
  return (
    <StackedField ids={ids} label={label} hint={hint} error={error}>
      <BaseInput ref={ref} {...props} {...fieldAria(ids, { hint, error }, props)} className={cn(error && ERROR_BORDER, className)} />
    </StackedField>
  );
});
Input.displayName = "Input";
