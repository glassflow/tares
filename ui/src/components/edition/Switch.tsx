import * as React from "react";

import { Switch as BaseSwitch } from "@/components/ui/switch";
import { cn } from "@/lib/utils";
import { ERROR_BORDER, fieldAria, InlineField, useFieldIds, type FieldProps } from "@/components/edition/Field";

export interface SwitchProps extends React.ComponentProps<typeof BaseSwitch>, FieldProps {}

/** `data-testid` lands on the switch itself. */
export const Switch = React.forwardRef<React.ElementRef<typeof BaseSwitch>, SwitchProps>(
  ({ label, hint, error, id, className, ...props }, ref) => {
    const ids = useFieldIds(id);
    return (
      <InlineField ids={ids} label={label} hint={hint} error={error}>
        <BaseSwitch ref={ref} {...fieldAria(ids, { hint, error })} className={cn(error && ERROR_BORDER, className)} {...props} />
      </InlineField>
    );
  },
);
Switch.displayName = "Switch";
