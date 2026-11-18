import * as React from "react";
import * as TabsPrimitive from "@radix-ui/react-tabs";

import { cn } from "@/lib/utils";

const Tabs = TabsPrimitive.Root;

// The underline slides to the tab chosen (TR-1436). One line for the list, placed under the active tab
// and moved when the active tab, the tabs or the list's size change. It is placed once without moving,
// then data-line="on" turns its transition on and hides the tabs' own borders (styles.css, motion-tabs).
// Until script places it, or without script, each tab's own border shows the choice as before.
function useTabLine(list: React.RefObject<HTMLDivElement>) {
  React.useLayoutEffect(() => {
    const el = list.current;
    const line = el?.querySelector<HTMLElement>(":scope > [data-tab-line]");
    if (!el || !line) return;
    let frame = 0;
    const place = () => {
      const tab = el.querySelector<HTMLElement>(':scope > [role="tab"][data-state="active"]');
      line.style.opacity = tab ? "1" : "0";
      if (!tab) return;
      line.style.transform = `translateX(${tab.offsetLeft}px)`;
      line.style.inlineSize = `${tab.offsetWidth}px`;
    };
    place();
    frame = requestAnimationFrame(() => { el.dataset.line = "on"; });
    const mo = new MutationObserver(place);
    mo.observe(el, { subtree: true, childList: true, attributes: true, attributeFilter: ["data-state"] });
    const ro = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(place);
    ro?.observe(el);
    return () => { cancelAnimationFrame(frame); mo.disconnect(); ro?.disconnect(); delete el.dataset.line; };
  }, [list]);
}

const TabsList = React.forwardRef<
  React.ElementRef<typeof TabsPrimitive.List>,
  React.ComponentPropsWithoutRef<typeof TabsPrimitive.List>
>(({ className, children, ...props }, ref) => {
  const own = React.useRef<HTMLDivElement>(null);
  React.useImperativeHandle(ref, () => own.current as HTMLDivElement);
  useTabLine(own);
  return (
    <TabsPrimitive.List
      ref={own}
      className={cn("motion-tabs relative inline-flex w-full items-center gap-1 border-b border-border", className)}
      {...props}
    >
      {children}
      <span aria-hidden data-tab-line className="bg-primary opacity-0" />
    </TabsPrimitive.List>
  );
});
TabsList.displayName = TabsPrimitive.List.displayName;

const TabsTrigger = React.forwardRef<
  React.ElementRef<typeof TabsPrimitive.Trigger>,
  React.ComponentPropsWithoutRef<typeof TabsPrimitive.Trigger>
>(({ className, ...props }, ref) => (
  <TabsPrimitive.Trigger
    ref={ref}
    className={cn(
      "inline-flex items-center justify-center whitespace-nowrap border-0 border-b-2 border-b-transparent bg-transparent px-3 py-1.5 text-sm font-medium text-muted-foreground transition-[color,background-color] motion-reduce:transition-none hover:border-b-transparent hover:text-foreground disabled:pointer-events-none disabled:opacity-45 data-[state=active]:border-b-primary data-[state=active]:text-foreground data-[state=active]:hover:border-b-primary",
      className,
    )}
    {...props}
  />
));
TabsTrigger.displayName = TabsPrimitive.Trigger.displayName;

const TabsContent = React.forwardRef<
  React.ElementRef<typeof TabsPrimitive.Content>,
  React.ComponentPropsWithoutRef<typeof TabsPrimitive.Content>
>(({ className, ...props }, ref) => (
  <TabsPrimitive.Content ref={ref} className={cn("mt-3 outline-none focus-visible:outline-2 focus-visible:outline-solid focus-visible:outline-brand", className)} {...props} />
));
TabsContent.displayName = TabsPrimitive.Content.displayName;

export { Tabs, TabsList, TabsTrigger, TabsContent };
