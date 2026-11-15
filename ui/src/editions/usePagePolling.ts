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
  const readFor = useRef(-1);   // the tick the last read answered: a new interval alone does not read again
  const mounted = useRef(false);
  const latest = useRef(0);     // the newest read: an older answer that arrives after it is dropped

  // A read answers as long as the page is mounted and it is the newest one. It does not depend on
  // the effect below, which restarts when `ms` changes (and runs twice under StrictMode): an answer
  // in flight at that moment still lands.
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);

  useEffect(() => {
    let timer: ReturnType<typeof setInterval> | undefined;
    const load = () => {
      const mine = ++latest.current;
      const current = () => mounted.current && mine === latest.current;
      fnRef.current()
        .then((d) => { if (current()) { setData(d); setError(undefined); } })
        .catch((e) => { if (current()) setError(String(e?.message ?? e)); });
    };
    const stop = () => { if (timer !== undefined) { clearInterval(timer); timer = undefined; } };
    const start = () => { stop(); timer = setInterval(load, ms); };
    const onChange = () => {
      if (document.hidden) { stop(); return; }
      load();
      start();
    };
    if (readFor.current !== tick) { readFor.current = tick; load(); }
    if (ms <= 0) return undefined;
    if (!document.hidden) start();
    document.addEventListener("visibilitychange", onChange);
    return () => { stop(); document.removeEventListener("visibilitychange", onChange); };
  }, [ms, tick]);

  return { data, error, reload: () => setTick((t) => t + 1) };
}
