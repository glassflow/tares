import { createContext, useContext, useEffect, useState, type ReactNode } from "react";

import { api } from "../api";
import type { Edition } from "../types";

export type EditionInfo = {
  /** undefined until the cell has said which edition it is */
  edition?: Edition;
  /** the edition's display name, as the cell gives it */
  name?: string;
  /** what the key this console signs in with is called; unset on a cell with no sign-in */
  credentialName?: string;
  /** the cell has no sign-in at all (whoami answers "open"), so there is no one to name */
  open?: boolean;
};

const EditionContext = createContext<EditionInfo>({});

/** Asks the cell for its edition once. If that fails the console still has to work, so it falls
 *  back to the platform edition, which is today's console. */
export function EditionProvider({ children }: { children: ReactNode }) {
  const [info, setInfo] = useState<EditionInfo>({});
  useEffect(() => {
    let live = true;
    api.whoami()
      .then((w) => { if (live) setInfo({ edition: w.edition ?? "platform", name: w.edition_name, open: w.id === "open", credentialName: w.id === "open" ? undefined : w.name }); })
      .catch(() => { if (live) setInfo({ edition: "platform" }); });
    return () => { live = false; };
  }, []);
  return <EditionContext.Provider value={info}>{children}</EditionContext.Provider>;
}

export function useEdition(): EditionInfo {
  return useContext(EditionContext);
}
