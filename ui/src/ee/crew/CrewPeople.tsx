// Tares enterprise licence, see ee/LICENSE
import { useEffect, useState } from "react";

import { TimeAgo } from "../../components/bits";
import { isMine, MemberRow, skey } from "../../pages/Crew";
import type { SlotProps } from "../../slotProps";
import type { CrewGroup, CrewStation } from "../../types";

const STALE_MS = 24 * 3600 * 1000;
/** A crew nobody heard from for more than a day, by the daemon's clock. A crew with no id is the
 *  flat fallback and is current. */
const isStale = (g: CrewGroup, now: string) =>
  g.id !== null && (!g.last_seen || new Date(now).getTime() - new Date(g.last_seen).getTime() > STALE_MS);

/** One crew: its name, machine and when it was last heard, then its stations with what each is on. */
function CrewBlock({ g, stations, selected }: { g: CrewGroup; stations: CrewStation[]; selected: string }) {
  // helpers show under the station that started them
  const top = g.stations.filter((s) => !(s.role === "helper" && g.stations.some((x) => x.name === s.parent)));
  return (
    <div className="crew-block">
      {(g.name || g.machine) && (
        <div className="crew-block-head">
          <b>{g.name ?? "crew"}</b>
          {g.machine && <span className="dim">{g.machine}</span>}
          {g.last_seen && <small className="dim">last seen <TimeAgo ts={g.last_seen} /></small>}
        </div>
      )}
      <ul className="crew-list">
        {top.map((s) => <MemberRow key={skey(s)} s={s} all={stations} selected={selected} withProject />)}
      </ul>
    </div>
  );
}

/** Crews not seen for a day, shut. Opens by itself when the member picked is in one of them. */
function StaleCrews({ crews, stations, selected }: { crews: CrewGroup[]; stations: CrewStation[]; selected: string }) {
  const holds = crews.some((g) => g.stations.some((s) => skey(s) === selected));
  const [open, setOpen] = useState(holds);
  useEffect(() => { if (holds) setOpen(true); }, [holds]);
  return (
    <details className="crew-stale" open={open}
      onToggle={(e) => { if (e.target === e.currentTarget) setOpen(e.currentTarget.open); }}>
      <summary>Not seen recently <span className="dim">· {crews.length}</span></summary>
      {crews.map((g) => <CrewBlock key={g.id ?? "none"} g={g} stations={stations} selected={selected} />)}
    </details>
  );
}

/** Every person's crews: you first, then the others by name. */
export default function CrewPeople({ groups, myId, now, stations, selected }: SlotProps["crew.groups"]) {
  const people = new Map<string, { id: string; name: string; mine: boolean; crews: CrewGroup[] }>();
  for (const g of groups) {
    const mine = isMine(g, myId);
    const id = mine ? myId : g.person!.id;
    if (!people.has(id)) people.set(id, { id, name: mine ? "You" : g.person!.name, mine, crews: [] });
    people.get(id)!.crews.push(g);
  }
  const ordered = [...people.values()].sort((a, b) => Number(b.mine) - Number(a.mine) || a.name.localeCompare(b.name));
  return (
    <>
      {!people.has(myId) && (
        <p className="dim" style={{ padding: "10px 16px", margin: 0 }}>
          You have no crew on this cell yet: <span className="mono">tares factory crew up</span>.
        </p>
      )}
      {ordered.map((p) => {
        const fresh = p.crews.filter((g) => !isStale(g, now));
        const stale = p.crews.filter((g) => isStale(g, now));
        return (
          <section key={p.id} className="crew-person">
            <h4 className="crew-person-h">{p.name}</h4>
            {fresh.map((g) => <CrewBlock key={g.id ?? "none"} g={g} stations={stations} selected={selected} />)}
            {stale.length > 0 && <StaleCrews crews={stale} stations={stations} selected={selected} />}
          </section>
        );
      })}
    </>
  );
}
