/* vendored from glassflow/arche-design paper-kit-v2 b0f2a89c9b746ebbc4a4bd6968c128ccd414afb2: do not edit, run npm run paper:sync */
import * as React from "react";

import { Switch as BaseSwitch } from "./ui/switch";
import { cn } from "./lib/cn";
import { ERROR_BORDER, fieldAria, InlineField, useFieldIds, type FieldProps } from "./Field";

export interface SwitchProps extends React.ComponentProps<typeof BaseSwitch>, FieldProps {}

/** `data-testid` lands on the switch itself. */
export const Switch = React.forwardRef<React.ElementRef<typeof BaseSwitch>, SwitchProps>(
  ({ label, hint, error, id, className, ...props }, ref) => {
    const ids = useFieldIds(id);
    return (
      <InlineField ids={ids} label={label} hint={hint} error={error}>
        <BaseSwitch ref={ref} {...props} {...fieldAria(ids, { hint, error }, props)} className={cn(error && ERROR_BORDER, className)} />
      </InlineField>
    );
  },
);
Switch.displayName = "Switch";
