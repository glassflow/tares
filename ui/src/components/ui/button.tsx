import * as React from "react";
import { Slot } from "@radix-ui/react-slot";
import { cva, type VariantProps } from "class-variance-authority";

import { cn } from "@/lib/utils";

// The console's own `button` element rules are unlayered and would beat these utilities, so the
// ones they also set (display, size, padding, border, fill, colour) are marked important.
const buttonVariants = cva(
  "inline-flex! items-center justify-center gap-2 whitespace-nowrap text-sm! font-medium! transition-colors disabled:pointer-events-none disabled:opacity-50 [&_svg]:pointer-events-none [&_svg]:shrink-0",
  {
    variants: {
      variant: {
        default:
          "border! border-primary! bg-primary! text-primary-foreground! hover:border-primary/90! hover:bg-primary/90! hover:text-primary-foreground!",
        destructive:
          "border! border-destructive! bg-card! text-destructive! hover:bg-destructive/10! hover:text-destructive! hover:border-destructive!",
        outline:
          "border! border-border! bg-card! text-foreground! hover:border-border! hover:bg-accent! hover:text-accent-foreground!",
        secondary:
          "border! border-border! bg-secondary! text-secondary-foreground! hover:border-border! hover:bg-secondary/70! hover:text-secondary-foreground!",
        ghost:
          "border! border-transparent! bg-transparent! text-foreground! hover:border-transparent! hover:bg-accent! hover:text-accent-foreground!",
        link: "border-0! bg-transparent! p-0! text-foreground! underline-offset-4 hover:text-foreground! hover:underline",
      },
      size: {
        default: "h-9 px-4! py-2!",
        sm: "h-8 px-3! py-1! text-xs!",
        lg: "h-10 px-6! py-2!",
        icon: "size-9 p-0!",
      },
    },
    defaultVariants: { variant: "default", size: "default" },
  },
);

export interface ButtonProps
  extends React.ButtonHTMLAttributes<HTMLButtonElement>,
    VariantProps<typeof buttonVariants> {
  asChild?: boolean;
}

const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className, variant, size, asChild = false, ...props }, ref) => {
    const Comp = asChild ? Slot : "button";
    return (
      <Comp
        ref={ref}
        data-slot="button"
        className={cn(buttonVariants({ variant, size }), className)}
        {...props}
      />
    );
  },
);
Button.displayName = "Button";

export { Button, buttonVariants };
