import * as React from "react";

import { cn } from "@/lib/utils";

// Border, padding, fill and size are important: the console's bare `input` rules are unlayered.
const Input = React.forwardRef<HTMLInputElement, React.ComponentProps<"input">>(
  ({ className, type, ...props }, ref) => (
    <input
      type={type}
      ref={ref}
      data-slot="input"
      className={cn(
        "flex h-9 w-full border! border-input! bg-background! px-3! py-1! text-sm! text-foreground! transition-colors motion-reduce:transition-none file:border-0 file:bg-transparent file:text-sm file:font-medium placeholder:text-muted-foreground disabled:cursor-not-allowed disabled:opacity-50",
        className,
      )}
      {...props}
    />
  ),
);
Input.displayName = "Input";

export { Input };
