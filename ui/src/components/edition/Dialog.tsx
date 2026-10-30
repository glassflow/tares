import * as React from "react";

import {
  Dialog as BaseDialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { cn } from "@/lib/utils";

export interface DialogProps {
  /** The element that opens it, usually a Button. Leave out when `open` is controlled. */
  trigger?: React.ReactNode;
  /** Both are required: the title names the dialog and the description says what it does. */
  title: string;
  description: string;
  children?: React.ReactNode;
  /** Buttons along the bottom edge. */
  footer?: React.ReactNode;
  open?: boolean;
  onOpenChange?: (open: boolean) => void;
  className?: string;
  "data-testid"?: string;
}

export function Dialog({
  trigger,
  title,
  description,
  children,
  footer,
  open,
  onOpenChange,
  className,
  "data-testid": testId,
}: DialogProps) {
  return (
    <BaseDialog open={open} onOpenChange={onOpenChange}>
      {trigger && <DialogTrigger asChild>{trigger}</DialogTrigger>}
      <DialogContent data-testid={testId} className={cn("max-h-[85vh] overflow-y-auto bg-card", className)}>
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          <DialogDescription>{description}</DialogDescription>
        </DialogHeader>
        {children}
        {footer && <DialogFooter>{footer}</DialogFooter>}
      </DialogContent>
    </BaseDialog>
  );
}
