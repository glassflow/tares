// Tares enterprise licence, see ee/LICENSE
import type { RegisterSlot } from "../eeSlots";
import { ChangeLead, ProjectAccess, ProjectMembers } from "./project/access";
import AreasPanel from "./settings/AreasPanel";

/** Called once by the console after the cell says its licence allows enterprise. Each ee screen
 *  registers itself under a slot name here. */
export function register(registerSlot: RegisterSlot): void {
  registerSlot("project.access", ProjectAccess);
  registerSlot("page.project.members", ProjectMembers);
  registerSlot("projects.changeLead", ChangeLead);
  registerSlot("page.settings.areas", AreasPanel);
}
