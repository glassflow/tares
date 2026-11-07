import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";

import { api, type LicenceState } from "./api";

export type LicenceInfo = {
  /** undefined until the cell has answered */
  state?: LicenceState;
  /** the cell's own answer: a valid licence with the enterprise feature. The console never works it out. */
  eeAllowed: boolean;
  /** the first answer is still on its way */
  loading: boolean;
  /** reads the licence again, after a change; false when the cell did not answer */
  reload: () => Promise<boolean>;
};

const REFRESH_MS = 60_000;

const LicenceContext = createContext<LicenceInfo>({ eeAllowed: false, loading: false, reload: async () => true });

/** Reads GET /api/licence on mount and every 60 seconds. A failed read keeps what is shown. */
export function LicenceProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<LicenceState>();
  const [loading, setLoading] = useState(true);
  const live = useRef(true);
  useEffect(() => () => { live.current = false; }, []);

  const reload = useCallback(
    () => api.licence().then(
      (s) => { if (live.current) { setState(s); setLoading(false); } return true; },
      () => { if (live.current) setLoading(false); return false; },
    ),
    [],
  );
  useEffect(() => {
    void reload();
    const t = window.setInterval(() => { void reload(); }, REFRESH_MS);
    return () => window.clearInterval(t);
  }, [reload]);

  const value = useMemo<LicenceInfo>(
    () => ({ state, eeAllowed: state?.ee_allowed === true, loading, reload }),
    [state, loading, reload],
  );
  return <LicenceContext.Provider value={value}>{children}</LicenceContext.Provider>;
}

export function useLicence(): LicenceInfo {
  return useContext(LicenceContext);
}
