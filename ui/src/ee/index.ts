// Tares enterprise licence, see ee/LICENSE
import type { RegisterSlot } from "../eeSlots";
import TeamActions from "./home/actions";
import { WaitingForYou, YouAreWaitingOn } from "./home/Waiting";

/** Called once by the console after the cell says its licence allows enterprise. Each ee screen
 *  registers itself under a slot name here. */
export function register(registerSlot: RegisterSlot): void {
  registerSlot("home.waiting", WaitingForYou);
  registerSlot("home.waitingOn", YouAreWaitingOn);
  registerSlot("home.teamActions", TeamActions);
}
