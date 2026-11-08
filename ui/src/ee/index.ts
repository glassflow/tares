// Tares enterprise licence, see ee/LICENSE
import type { RegisterSlot } from "../eeSlots";
import { ChangeLead, ProjectAccess, ProjectMembers } from "./project/access";
import { GrantAgreedBy, GrantConsent, GrantRepos, GrantsWaitingElsewhere } from "./project/grants";
import { SignOffs, TeamQuestions } from "./project/signoffs";
import AreasPanel from "./settings/AreasPanel";
import TeamActions from "./home/actions";
import { WaitingForYou, YouAreWaitingOn } from "./home/Waiting";

/** Called once by the console after the cell says its licence allows enterprise. Each ee screen
 *  registers itself under a slot name here. */
export function register(registerSlot: RegisterSlot): void {
  registerSlot("project.access", ProjectAccess);
  registerSlot("page.project.members", ProjectMembers);
  registerSlot("projects.changeLead", ChangeLead);
  registerSlot("page.settings.areas", AreasPanel);
  registerSlot("grants.agreedBy", GrantAgreedBy);
  registerSlot("grants.consent", GrantConsent);
  registerSlot("grants.waitingElsewhere", GrantsWaitingElsewhere);
  registerSlot("grants.repos", GrantRepos);
  registerSlot("project.teamQuestions", TeamQuestions);
  registerSlot("project.signoffs", SignOffs);
  registerSlot("home.waiting", WaitingForYou);
  registerSlot("home.waitingOn", YouAreWaitingOn);
  registerSlot("home.teamActions", TeamActions);
}
