import { useEffect, useRef, useState } from "react";

/** Loads on mount and again every `ms`, like `usePolling`, and returns the same thing. While the
 *  tab is hidden the timer is stopped, so a background tab makes no calls; when the tab is visible
 *  again it loads once straight away and starts the timer again. The first load always runs, even
 *  in a hidden tab, so a page never shows its empty state for data it has not asked for. With
 *  `ms` of 0 it loads once and never again by itself (only `reload` reads again). */
export function usePagePolling<T>(fn: () => Promise<T>, ms: number): {
  data: T | undefined;
  error: string | undefined;
  reload: () => void;
} {
  const [data, setData] = useState<T>();
  const [error, setError] = useState<string>();
  const [tick, setTick] = useState(0);
  const fnRef = useRef(fn);
  fnRef.current = fn;

  useEffect(() => {
    let live = true;
    let timer: ReturnType<typeof setInterval> | undefined;
    const load = () => {
      fnRef.current()
        .then((d) => { if (live) { setData(d); setError(undefined); } })
        .catch((e) => { if (live) setError(String(e?.message ?? e)); });
    };
    const stop = () => { if (timer !== undefined) { clearInterval(timer); timer = undefined; } };
    const start = () => { stop(); timer = setInterval(load, ms); };
    const onChange = () => {
      if (document.hidden) { stop(); return; }
      load();
      start();
    };
    load();
    if (ms <= 0) return () => { live = false; };
    if (!document.hidden) start();
    document.addEventListener("visibilitychange", onChange);
    return () => { live = false; stop(); document.removeEventListener("visibilitychange", onChange); };
  }, [ms, tick]);

  return { data, error, reload: () => setTick((t) => t + 1) };
}
