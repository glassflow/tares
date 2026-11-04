import * as React from "react";
import * as SwitchPrimitive from "@radix-ui/react-switch";

import { cn } from "@/lib/utils";

const Switch = React.forwardRef<
  React.ElementRef<typeof SwitchPrimitive.Root>,
  React.ComponentPropsWithoutRef<typeof SwitchPrimitive.Root>
>(({ className, ...props }, ref) => (
  <SwitchPrimitive.Root
    ref={ref}
    className={cn(
      "peer inline-flex! h-5 w-9 shrink-0 cursor-pointer items-center border! border-border! bg-muted! p-0! transition-colors motion-reduce:transition-none hover:border-border! disabled:cursor-not-allowed disabled:opacity-45! data-[state=checked]:border-primary! data-[state=checked]:bg-primary! data-[state=checked]:hover:border-primary!",
      className,
    )}
    {...props}
  >
    <SwitchPrimitive.Thumb className="pointer-events-none block size-3.5 translate-x-0.5 bg-muted-foreground transition-transform motion-reduce:transition-none data-[state=checked]:translate-x-[18px] data-[state=checked]:bg-primary-foreground" />
  </SwitchPrimitive.Root>
));
Switch.displayName = SwitchPrimitive.Root.displayName;

export { Switch };
