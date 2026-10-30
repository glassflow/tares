import * as React from "react";

import { Button as BaseButton } from "@/components/ui/button";
import { cn } from "@/lib/utils";

export type ButtonVariant = "primary" | "secondary" | "quiet";
export type ButtonSize = "md" | "lg";

// The edition names on top of the primitive's variants. Utilities that must beat the console's
// unlayered `button` rules carry the important suffix, as in the primitive.
const VARIANTS = {
  primary: { base: "default", extra: "border-foreground! font-semibold! hover:border-foreground!" },
  secondary: { base: "outline", extra: "border-border! bg-transparent! hover:border-border! hover:bg-muted!" },
  quiet: { base: "ghost", extra: "text-[color:var(--ink-soft)]! hover:text-foreground!" },
} as const;

export interface ButtonProps extends Omit<React.ComponentProps<typeof BaseButton>, "variant" | "size"> {
  variant?: ButtonVariant;
  size?: ButtonSize;
  /** Waiting for an answer: the button is inert and says so to assistive tech. */
  busy?: boolean;
}

export const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(
  ({ variant = "primary", size = "md", busy = false, disabled, asChild, className, children, ...props }, ref) => {
    const v = VARIANTS[variant];
    const inert = Boolean(disabled || busy);
    return (
      <BaseButton
        ref={ref}
        asChild={asChild}
        variant={v.base}
        size={size === "lg" ? "lg" : "default"}
        // A link cannot be disabled, so it gets aria-disabled and no pointer events instead.
        disabled={asChild ? undefined : inert}
        aria-disabled={asChild && inert ? true : undefined}
        aria-busy={busy || undefined}
        type={asChild ? undefined : (props.type ?? "button")}
        className={cn(v.extra, asChild && inert && "pointer-events-none opacity-50", busy && "cursor-progress", className)}
        {...props}
        tabIndex={asChild && inert ? -1 : props.tabIndex}
        onClick={(e) => {
          // A link has no disabled state: stop Enter and clicks from following it.
          if (asChild && inert) e.preventDefault();
          else props.onClick?.(e);
        }}
      >
        {asChild ? (
          children
        ) : (
          <>
            {busy && <span aria-hidden className="size-2 shrink-0 animate-pulse bg-current motion-reduce:animate-none" />}
            {children}
          </>
        )}
      </BaseButton>
    );
  },
);
Button.displayName = "Button";
