import * as React from "react";
import * as CheckboxPrimitive from "@radix-ui/react-checkbox";

import { Check } from "@/components/icons";
import { cn } from "@/lib/utils";

const Checkbox = React.forwardRef<
  React.ElementRef<typeof CheckboxPrimitive.Root>,
  React.ComponentPropsWithoutRef<typeof CheckboxPrimitive.Root>
>(({ className, ...props }, ref) => (
  <CheckboxPrimitive.Root
    ref={ref}
    className={cn(
      "peer inline-flex! size-4 shrink-0 items-center justify-center border! border-input! bg-background! p-0! text-primary-foreground! hover:border-input! disabled:cursor-not-allowed disabled:opacity-50 data-[state=checked]:border-primary! data-[state=checked]:bg-primary! data-[state=checked]:hover:border-primary!",
      className,
    )}
    {...props}
  >
    <CheckboxPrimitive.Indicator className="flex items-center justify-center text-current">
      <Check className="size-3.5" />
    </CheckboxPrimitive.Indicator>
  </CheckboxPrimitive.Root>
));
Checkbox.displayName = CheckboxPrimitive.Root.displayName;

export { Checkbox };
