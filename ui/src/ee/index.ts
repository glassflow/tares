// Tares enterprise licence, see ee/LICENSE
import type { RegisterSamples, RegisterSlot } from "../eeSlots";
import { ChangeLead, ProjectAccess, ProjectMembers } from "./project/access";
import { GrantAgreedBy, GrantConsent, GrantRepos, GrantsWaitingElsewhere, GrantWaitingFor } from "./project/grants";
import { SignOffs, TeamQuestions } from "./project/signoffs";
import CrewPeople from "./crew/CrewPeople";
import DeskForPeople from "./crew/DeskGroups";
import AreasPanel from "./settings/AreasPanel";
import { PeopleAdd, PersonTeamLines } from "./people/FactoryPeopleTeam";
import { KeyPersonCell, KeyPersonPick, PeoplePanel } from "./people/SettingsPeople";
import TeamActions from "./home/actions";
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
  registerSlot("settings.people", PeoplePanel);
  registerSlot("settings.keyPerson", KeyPersonCell);
  registerSlot("settings.keyPersonPick", KeyPersonPick);
  registerSlot("people.teamLines", PersonTeamLines);
  registerSlot("people.add", PeopleAdd);
  registerSlot("grants.agreedBy", GrantAgreedBy);
  registerSlot("grants.waitingFor", GrantWaitingFor);
  registerSlot("grants.consent", GrantConsent);
  registerSlot("grants.waitingElsewhere", GrantsWaitingElsewhere);
  registerSlot("grants.repos", GrantRepos);
  registerSlot("project.teamQuestions", TeamQuestions);
  registerSlot("project.signoffs", SignOffs);
  registerSlot("home.waiting", WaitingForYou);
  registerSlot("home.waitingOn", YouAreWaitingOn);
  registerSlot("home.teamActions", TeamActions);
  registerSlot("home.crewLimit", CrewLimit);
  registerSlot("home.teamCrews", TeamCrews);
  registerSlot("home.reposYouOwn", ReposYouOwn);
  registerSlot("crew.groups", CrewPeople);
  registerSlot("desk.groups", DeskForPeople);
  registerSamples("home", HOME_SAMPLES_EE);
}
