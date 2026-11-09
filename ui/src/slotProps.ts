import type { ReactNode } from "react";

import type { Ctx } from "./components/project/common";
import type { FactoryPeopleData, PersonRow } from "./editions/factory/FactoryPeople.api";
import type {
  HomeCrewLimitProps, HomeHiddenRowsProps, HomeProjectLeadProps, HomeProjectsSwitchProps, HomeReposProps,
  HomeSample, HomeTeamActionsProps, HomeTeamCrewsProps, HomeWaitingOnProps, HomeWaitingProps,
} from "./editions/factory/home.types";
import type {
  ApiKey, CrewGroup, CrewStation, DeskAllowed, DeskItem, Grant, GrantsView, Handover, Person, Whoami,
} from "./types";

/*
 * The props each slot of the console takes (see eeSlots.tsx). Core renders a slot with these props
 * and src/ee registers a component that takes them, so both sides are checked against one list.
 * Types only: core never imports src/ee.
 */

/** A slot that takes nothing. */
type NoProps = Record<never, never>;

export interface SlotProps {
  // projects
  "project.access": { ctx: Ctx };
  "page.project.members": { ctx: Ctx };
  "projects.changeLead": { id: string; lead?: { id: string } | null; people: Person[]; onDone: () => void };
  // the overview of a project, and its Grants view
  "project.teamQuestions": { project: string };
  "project.signoffs": { handover?: Handover | null };
  "grants.agreedBy": { part: "head" | "cell"; grant?: Grant };
  "grants.waitingFor": { grant: Grant };
  "grants.consent": {
    grant: Grant; list?: GrantsView; busy: boolean;
    onBusy: (id?: string) => void; reload: () => void; onError: (e: unknown) => void;
  };
  "grants.waitingElsewhere": { here: Grant[]; list?: GrantsView; row: (g: Grant) => ReactNode; head: ReactNode };
  "grants.repos": { repos: string[]; onChange: (repos: string[]) => void };
  // settings
  "page.settings.areas": NoProps;
  "settings.people": { people?: Person[]; admin: boolean; err?: string; onChanged: () => void; me?: Whoami };
  "settings.keyPerson": { k: ApiKey; people: Person[]; admin: boolean; onChanged: () => void; onError: (message: string) => void };
  "settings.keyPersonPick": { people: Person[]; value: string; onChange: (personId: string) => void };
  // the factory People page
  "people.teamLines": { p: PersonRow };
  "people.add": { data: FactoryPeopleData; onAdded: (words: string) => void };
  // the factory home
  "home.waiting": HomeWaitingProps;
  "home.waitingOn": HomeWaitingOnProps;
  "home.teamActions": HomeTeamActionsProps;
  "home.projectsSwitch": HomeProjectsSwitchProps;
  "home.projectLock": NoProps;
  "home.projectLead": HomeProjectLeadProps;
  "home.hiddenRows": HomeHiddenRowsProps;
  "home.crewLimit": HomeCrewLimitProps;
  "home.teamCrews": HomeTeamCrewsProps;
  "home.reposYouOwn": HomeReposProps;
  // crew
  "crew.groups": { groups: CrewGroup[]; myId: string; now: string; stations: CrewStation[]; selected: string };
  "desk.groups": { items: DeskItem[]; allowed: Record<string, DeskAllowed>; onAllowed: (a: DeskAllowed) => void; myId: string };
}

export type SlotName = keyof SlotProps;
/** The slots that are a whole page, shown with EePage. */
export type PageSlotName = Extract<SlotName, `page.${string}`>;

/** The samples a page shows, by page; ee adds its own through registerSamples. */
export interface SampleSets {
  home: Record<string, HomeSample>;
}
