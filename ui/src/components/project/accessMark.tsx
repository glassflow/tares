import { Lock } from "../icons";
import type { Ctx } from "./common";

/** The read-only side of project access: a private project says so. The lead, scope and members
 *  controls are enterprise (the `project.access` slot); without them this mark is all there is. */
export function AccessMark({ ctx }: { ctx: Ctx }) {
  if (ctx.s.scope !== "private") return null;
  return (
    <div className="acc">
      <div className="acc-row">
        <span className="acc-private" title="Only its lead and members can open it">
          <Lock /> Private
        </span>
      </div>
    </div>
  );
}
