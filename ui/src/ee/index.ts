// Tares enterprise licence, see ee/LICENSE
import type { RegisterSamples, RegisterSlot } from "../eeSlots";
import AreasPanel from "./settings/AreasPanel";
import { PeopleAdd, PersonTeamLines } from "./people/FactoryPeopleTeam";
import { KeyPersonCell, KeyPersonPick, PeoplePanel } from "./people/SettingsPeople";
import TeamActions from "./home/actions";
import { HOME_SAMPLES_EE } from "./home/samples";
import { CrewLimit, ReposYouOwn, TeamCrews } from "./home/Side";
import { WaitingForYou, YouAreWaitingOn } from "./home/Waiting";
import { HiddenRows, LeadLine, PrivateLock, ProjectsSwitch } from "./projects/Projects";
import { PROJECTS_SAMPLES_EE } from "./projects/samples";

/** Called once by the console after the cell says its licence allows enterprise. Each ee screen
 *  registers itself under a slot name here. */
export function register(registerSlot: RegisterSlot, registerSamples: RegisterSamples): void {
  registerSlot("projects.switch", ProjectsSwitch);
  registerSlot("projects.lock", PrivateLock);
  registerSlot("projects.lead", LeadLine);
  registerSlot("projects.hidden", HiddenRows);
  registerSlot("page.settings.areas", AreasPanel);
  registerSlot("settings.people", PeoplePanel);
  registerSlot("settings.keyPerson", KeyPersonCell);
  registerSlot("settings.keyPersonPick", KeyPersonPick);
  registerSlot("people.teamLines", PersonTeamLines);
  registerSlot("people.add", PeopleAdd);
  registerSlot("home.waiting", WaitingForYou);
  registerSlot("home.waitingOn", YouAreWaitingOn);
  registerSlot("home.teamActions", TeamActions);
  registerSlot("home.crewLimit", CrewLimit);
  registerSlot("home.teamCrews", TeamCrews);
  registerSlot("home.reposYouOwn", ReposYouOwn);
  registerSamples("home", HOME_SAMPLES_EE);
  registerSamples("projects", PROJECTS_SAMPLES_EE);
}
