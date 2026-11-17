import * as React from "react";

import { cn } from "@/lib/utils";

// Leaving takes a moment (TR-1432, TR-1434). React removes an element the instant its condition goes
// false, so nothing could animate out. usePresence keeps it mounted with data-state="closed" until every
// animation on it and inside it has finished, then lets it go. The CSS decides what closing looks like
// (the motion-* classes in styles.css); with no animation, or under reduced motion, it goes at once.

export type PresenceState = "open" | "closed";

/** Mounted while `show`, and after it until the element's exit animation ends. Put `ref` on the element
 *  and `data-state={state}` on it, so the CSS can tell arriving from leaving. */
export function usePresence<T extends HTMLElement = HTMLElement>(show: boolean) {
  const [mounted, setMounted] = React.useState(show);
  const ref = React.useRef<T>(null);
  // shown again before the exit ended: mount at once, in this render
  if (show && !mounted) setMounted(true);

  React.useEffect(() => {
    if (show || !mounted) return;
    const el = ref.current;
    // getAnimations reads styles first, so the closed state's animation is already in the list
    const running = el?.getAnimations?.({ subtree: true }) ?? [];
    if (!running.length) { setMounted(false); return; }
    let live = true;
    Promise.allSettled(running.map((a) => a.finished)).then(() => { if (live) setMounted(false); });
    return () => { live = false; };
  }, [show, mounted]);

  const state: PresenceState = show ? "open" : "closed";
  return { mounted, state, ref };
}

/** True while any child would render something: not null, undefined, false or "". */
function shows(children: React.ReactNode): boolean {
  return React.Children.toArray(children).length > 0;
}

export interface CollapseProps extends React.HTMLAttributes<HTMLDivElement> {
  /** What to show. When it turns to nothing (`{problem && <Banner…>}` with no problem), the last thing
   *  shown stays while it shrinks shut. */
  children?: React.ReactNode;
}

/** Wrap something that comes and goes in the flow of the page, a Banner most often: it grows open and
 *  shrinks shut, so what is below moves with it instead of jumping. What is there when the Collapse
 *  first renders just shows; only a change afterwards moves. */
export function Collapse({ children, className, ...props }: CollapseProps) {
  const show = shows(children);
  const last = React.useRef<React.ReactNode>(children);
  if (show) last.current = children;
  // shown from the start: no grow. Once it has closed, any later opening grows.
  const [initial, setInitial] = React.useState(show);
  const { mounted, state, ref } = usePresence<HTMLDivElement>(show);
  if (!show && initial) setInitial(false);
  // The parent's gap beside it: a stack's gap-5 would otherwise appear whole as it mounts and vanish whole
  // as it unmounts. The CSS folds it in (styles.css, m-pull-*): the next sibling moves up over it, reading
  // --m-gap from the parent; with no next sibling, the element pulls itself (--m-gap-s). Set before paint.
  React.useLayoutEffect(() => {
    const el = ref.current;
    const parent = el?.parentElement;
    if (!el || !parent) return;
    const cs = getComputedStyle(parent);
    const flow = cs.display.includes("grid") || (cs.display.includes("flex") && cs.flexDirection.startsWith("column"));
    const gap = flow && cs.rowGap !== "normal" ? cs.rowGap : "0px";
    parent.style.setProperty("--m-gap", gap);
    el.style.setProperty("--m-gap-s", !el.nextElementSibling && el.previousElementSibling ? gap : "0px");
  }, [mounted, state, ref]);
  if (!mounted) return null;
  return (
    <div ref={ref} data-state={state} data-initial={initial ? "" : undefined}
         className={cn("motion-collapse min-w-0", className)} {...props}>
      {show ? children : last.current}
    </div>
  );
}
