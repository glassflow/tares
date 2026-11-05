import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";

import { api } from "../api";
import type { Edition, Whoami } from "../types";

export type EditionInfo = {
  /** undefined until the cell has said which edition it is */
  edition?: Edition;
  /** the edition's display name, as the cell gives it */
  name?: string;
  /** what the key this console signs in with is called; unset on a cell with no sign-in */
  credentialName?: string;
  /** the person behind the sign-in, when it has one; the cell records changes under this name */
  personName?: string;
  /** the picture of that person (an https address), when they have one (TR-1016) */
  personPicture?: string;
  /** the cell has no sign-in at all (whoami answers "open"), so there is no one to name */
  open?: boolean;
  /** the cell has no edition stored yet, so the first screen asks which one (whoami edition_needed) */
  needed?: boolean;
  /** the key this console signs in with may change settings; a cell with no sign-in counts as admin */
  admin?: boolean;
  /** reads whoami again, after the edition was chosen; false when the cell did not answer */
  reload: () => Promise<boolean>;
};

const EditionContext = createContext<EditionInfo>({ reload: async () => true });

/** Asks the cell for its edition once. If that fails the console still has to work, so it falls
 *  back to the platform edition, which is today's console. */
export function EditionProvider({ children }: { children: ReactNode }) {
  const [info, setInfo] = useState<Omit<EditionInfo, "reload">>({});
  useEffect(() => {
    let live = true;
    api.whoami()
      .then((w) => { if (live) setInfo(fromWhoami(w)); })
      .catch(() => { if (live) setInfo({ edition: "platform" }); });
    return () => { live = false; };
  }, []);
  // After a choice is saved; a failed read keeps what is shown and says so (false)
  const reload = useCallback(
    () => api.whoami().then((w) => { setInfo(fromWhoami(w)); return true; }, () => false),
    [],
  );
  const value = useMemo(() => ({ ...info, reload }), [info, reload]);
  return <EditionContext.Provider value={value}>{children}</EditionContext.Provider>;
}

function fromWhoami(w: Whoami): Omit<EditionInfo, "reload"> {
  const open = w.id === "open";
  return {
    edition: w.edition ?? "platform", name: w.edition_name, open, credentialName: open ? undefined : w.name,
    personName: w.person?.name || undefined, personPicture: w.person?.picture || undefined,
    needed: w.edition_needed === true, admin: open || (w.scopes ?? []).includes("admin"),
  };
}

export function useEdition(): EditionInfo {
  return useContext(EditionContext);
}
