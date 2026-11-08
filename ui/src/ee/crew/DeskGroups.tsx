// Tares enterprise licence, see ee/LICENSE
import { DeskGroupView, type DeskGroup } from "../../pages/Crew";
import type { SlotProps } from "../../slotProps";
import type { DeskItem } from "../../types";

/** The desk items grouped by the person each is for: you first, then the others by name, then
 *  the ones nobody is named for (yours to answer, as before TR-634). */
function deskGroups(items: DeskItem[], myId: string): DeskGroup[] {
  const by = new Map<string, DeskGroup>();
  for (const d of items) {
    const p = d.to_person;
    const key = !p ? "anyone" : p.id === myId ? "you" : p.id;
    if (!by.has(key)) by.set(key, { key, label: key === "you" ? "For you" : key === "anyone" ? "For anyone" : `For ${p!.name}`, name: p?.name ?? "", mine: key !== p?.id, items: [] });
    by.get(key)!.items.push(d);
  }
  const rank = (g: { key: string }) => (g.key === "you" ? 0 : g.key === "anyone" ? 2 : 1);
  return [...by.values()].sort((a, b) => rank(a) - rank(b) || a.label.localeCompare(b.label));
}

/** The open items under "For <name>" headings once they are for more than one person; an item for
 *  someone else is read-only here. */
export default function DeskForPeople({ items, allowed, onAllowed, myId }: SlotProps["desk.groups"]) {
  const groups = deskGroups(items, myId);
  // headed once there is more than one group, or the one group is not yours
  const headed = groups.length > 1 || groups.some((g) => !g.mine);
  return (
    <>
      {groups.map((g) => <DeskGroupView key={g.key} g={g} headed={headed} allowed={allowed} onAllowed={onAllowed} />)}
    </>
  );
}
