import * as React from "react";

import { Checkbox as BaseCheckbox } from "@/components/ui/checkbox";
import { cn } from "@/lib/utils";
import { ERROR_BORDER, fieldAria, InlineField, useFieldIds, type FieldProps } from "@/components/edition/Field";

export interface CheckboxProps extends React.ComponentProps<typeof BaseCheckbox>, FieldProps {}

/** `data-testid` lands on the checkbox itself. */
export const Checkbox = React.forwardRef<React.ElementRef<typeof BaseCheckbox>, CheckboxProps>(
  ({ label, hint, error, id, className, ...props }, ref) => {
    const ids = useFieldIds(id);
    return (
      <InlineField ids={ids} label={label} hint={hint} error={error}>
        <BaseCheckbox
          ref={ref}
          {...props}
          {...fieldAria(ids, { hint, error }, props["aria-describedby"])}
          className={cn("mt-0.5", error && ERROR_BORDER, className)}
        />
      </InlineField>
    );
  },
);
Checkbox.displayName = "Checkbox";
