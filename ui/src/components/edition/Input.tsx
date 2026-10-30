import * as React from "react";

import { Input as BaseInput } from "@/components/ui/input";
import { cn } from "@/lib/utils";
import { ERROR_BORDER, fieldAria, StackedField, useFieldIds, type FieldProps } from "@/components/edition/Field";

export interface InputProps extends React.ComponentProps<"input">, FieldProps {}

/** `data-testid` lands on the input itself. */
export const Input = React.forwardRef<HTMLInputElement, InputProps>(({ label, hint, error, id, className, ...props }, ref) => {
  const ids = useFieldIds(id);
  return (
    <StackedField ids={ids} label={label} hint={hint} error={error}>
      <BaseInput ref={ref} {...props} {...fieldAria(ids, { hint, error }, props["aria-describedby"])} className={cn(error && ERROR_BORDER, className)} />
    </StackedField>
  );
});
Input.displayName = "Input";
