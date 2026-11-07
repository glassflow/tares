// Tares enterprise licence, see ee/LICENSE
import type { RegisterSlot } from "../eeSlots";

/** Called once by the console after the cell says its licence allows enterprise. Each ee screen
 *  registers itself under a slot name here. Nothing is registered yet. */
export function register(registerSlot: RegisterSlot): void {
  void registerSlot;
}
