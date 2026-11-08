// Tares enterprise licence, see ee/LICENSE
import type { RegisterSamples, RegisterSlot } from "../eeSlots";
import { ChangeLead, ProjectAccess, ProjectMembers } from "./project/access";
import AreasPanel from "./settings/AreasPanel";
import TeamActions from "./home/actions";
import { HiddenRows, LeadLine, PrivateLock, ProjectsSwitch } from "./home/Projects";
import { HOME_SAMPLES_EE } from "./home/samples";
import { CrewLimit, ReposYouOwn, TeamCrews } from "./home/Side";
import { WaitingForYou, YouAreWaitingOn } from "./home/Waiting";

/** Called once by the console after the cell says its licence allows enterprise. Each ee screen
 *  registers itself under a slot name here. */
export function register(registerSlot: RegisterSlot, registerSamples: RegisterSamples): void {
  registerSlot("project.access", ProjectAccess);
  registerSlot("page.project.members", ProjectMembers);
  registerSlot("projects.changeLead", ChangeLead);
  registerSlot("page.settings.areas", AreasPanel);
  registerSlot("home.waiting", WaitingForYou);
  registerSlot("home.waitingOn", YouAreWaitingOn);
  registerSlot("home.teamActions", TeamActions);
  registerSlot("home.projectsSwitch", ProjectsSwitch);
  registerSlot("home.projectLock", PrivateLock);
  registerSlot("home.projectLead", LeadLine);
  registerSlot("home.hiddenRows", HiddenRows);
  registerSlot("home.crewLimit", CrewLimit);
  registerSlot("home.teamCrews", TeamCrews);
  registerSlot("home.reposYouOwn", ReposYouOwn);
  registerSamples("home", HOME_SAMPLES_EE);
}
