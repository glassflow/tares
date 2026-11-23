/* vendored from glassflow/arche-design paper-kit-v2 b0f2a89c9b746ebbc4a4bd6968c128ccd414afb2: do not edit, run npm run paper:sync */
import * as React from "react";

import {
  Select as BaseSelect,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "./ui/select";
import { cn } from "./lib/cn";
import { ERROR_BORDER, fieldAria, StackedField, useFieldIds, type FieldProps } from "./Field";

export interface SelectOption {
  value: string;
  label: string;
}

type TriggerProps = Omit<
  React.ComponentPropsWithoutRef<typeof SelectTrigger>,
  "id" | "children" | "className" | "value" | "defaultValue" | "dir" | "name" | "disabled" | "onChange"
>;

export interface SelectProps extends FieldProps, TriggerProps {
  options: SelectOption[];
  value?: string;
  defaultValue?: string;
  onValueChange?: (value: string) => void;
  placeholder?: string;
  disabled?: boolean;
  required?: boolean;
  name?: string;
  id?: string;
  className?: string;
  /** `data-testid` and other aria props land on the trigger button. */
}

export function Select({
  label,
  hint,
  error,
  id,
  options,
  value,
  defaultValue,
  onValueChange,
  placeholder,
  disabled,
  required,
  name,
  className,
  ...triggerProps
}: SelectProps) {
  const ids = useFieldIds(id);
  return (
    <StackedField ids={ids} label={label} hint={hint} error={error}>
      <BaseSelect value={value} defaultValue={defaultValue} onValueChange={onValueChange} disabled={disabled} required={required} name={name}>
        <SelectTrigger
          {...triggerProps}
          {...fieldAria(ids, { hint, error }, triggerProps)}
          className={cn(error && ERROR_BORDER, className)}
        >
          <SelectValue placeholder={placeholder} />
        </SelectTrigger>
        <SelectContent>
          {options.map((o) => (
            <SelectItem key={o.value} value={o.value}>
              {o.label}
            </SelectItem>
          ))}
        </SelectContent>
      </BaseSelect>
    </StackedField>
  );
}
