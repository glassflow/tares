import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";

import { api } from "../api";
import type { Whoami } from "../types";

export type Who = {
  /** false until the cell has answered whoami (or failed to) */
  loaded: boolean;
  /** what the key this console signs in with is called; unset on a cell with no sign-in */
  credentialName?: string;
  /** the person behind the sign-in, when it has one; the cell records changes under this name */
  personName?: string;
  /** the picture of that person (an https address), when they have one (TR-1016) */
  personPicture?: string;
  /** the cell has no sign-in at all (whoami answers "open"), so there is no one to name */
  open?: boolean;
  /** the key this console signs in with may change settings; a cell with no sign-in counts as admin */
  admin?: boolean;
  /** the id of the person behind the sign-in, when it has one */
  personId?: string;
  /** the key may write (an `ingest` or `admin` scope); false for a read-only key, true with no sign-in */
  canWrite?: boolean;
  /** reads whoami again (after a person changed); false when the cell did not answer */
  reload: () => Promise<boolean>;
};

const WhoContext = createContext<Who>({ loaded: false, reload: async () => true });

/** Asks the cell once who this console signs in as. If that fails the console still works, with
 *  nobody named. */
export function WhoProvider({ children }: { children: ReactNode }) {
  const [who, setWho] = useState<Omit<Who, "reload">>({ loaded: false });
  useEffect(() => {
    let live = true;
    api.whoami()
      .then((w) => { if (live) setWho(fromWhoami(w)); })
      .catch(() => { if (live) setWho({ loaded: true }); });
    return () => { live = false; };
  }, []);
  // a failed read keeps what is shown and says so (false)
  const reload = useCallback(
    () => api.whoami().then((w) => { setWho(fromWhoami(w)); return true; }, () => false),
    [],
  );
  const value = useMemo(() => ({ ...who, reload }), [who, reload]);
  return <WhoContext.Provider value={value}>{children}</WhoContext.Provider>;
}

function fromWhoami(w: Whoami): Omit<Who, "reload"> {
  const open = w.id === "open";
  return {
    loaded: true, open, credentialName: open ? undefined : w.name,
    personName: w.person?.name || undefined, personPicture: w.person?.picture || undefined,
    admin: open || (w.scopes ?? []).includes("admin"),
    personId: w.person?.id || undefined,
    canWrite: open || (w.scopes ?? []).some((s) => s === "ingest" || s === "admin"), // words-ok
  };
}

export function useWho(): Who {
  return useContext(WhoContext);
}
