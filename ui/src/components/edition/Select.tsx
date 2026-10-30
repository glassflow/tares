import * as React from "react";

import {
  Select as BaseSelect,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { cn } from "@/lib/utils";
import { ERROR_BORDER, fieldAria, StackedField, useFieldIds, type FieldProps } from "@/components/edition/Field";

export interface SelectOption {
  value: string;
  label: string;
}

export interface SelectProps extends FieldProps {
  options: SelectOption[];
  value?: string;
  defaultValue?: string;
  onValueChange?: (value: string) => void;
  placeholder?: string;
  disabled?: boolean;
  name?: string;
  id?: string;
  className?: string;
  /** Lands on the trigger button. */
  "data-testid"?: string;
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
  name,
  className,
  "data-testid": testId,
}: SelectProps) {
  const ids = useFieldIds(id);
  return (
    <StackedField ids={ids} label={label} hint={hint} error={error}>
      <BaseSelect value={value} defaultValue={defaultValue} onValueChange={onValueChange} disabled={disabled} name={name}>
        <SelectTrigger {...fieldAria(ids, { hint, error })} data-testid={testId} className={cn(error && ERROR_BORDER, className)}>
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
