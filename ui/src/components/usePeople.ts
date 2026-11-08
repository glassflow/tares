import { api } from "../api";
import type { Person } from "../types";
import { usePolling } from "./bits";

/** The people on the cell, who the caller is, and whether the caller's key has the admin scope
 *  (the project calls need it; the person's own admin flag does not count). Empty or false until
 *  they load. */
export function usePeople() {
  const { data: me } = usePolling(() => api.whoami(), 60000);
  const { data: people } = usePolling(() => api.people(), 60000);
  const meId = me?.person?.id ?? null;
  const list: Person[] = people?.people ?? [];
  return { list, meId, isAdmin: !!me?.scopes?.includes("admin"), kind: me?.kind };
}
