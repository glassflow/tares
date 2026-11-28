
import type { FactoryPeopleData, PersonRow } from "./editions/factory/FactoryPeople.api";
import type { FactoryProjectRow, FactoryProjectsData, HiddenProject, ProjectsView } from "./editions/factory/FactoryProjects.api";
import type {
  HomeCrewLimitProps, HomeReposProps,
  HomeSample, HomeTeamActionsProps, HomeTeamCrewsProps, HomeWaitingOnProps, HomeWaitingProps,
} from "./editions/factory/home.types";
import type {
  ApiKey, Person, Whoami,
} from "./types";

/*
 * The props each slot of the console takes (see eeSlots.tsx). Core renders a slot with these props
 * and src/ee registers a component that takes them, so both sides are checked against one list.
 * Types only: core never imports src/ee.
 */

/** A slot that takes nothing. */
type NoProps = Record<never, never>;

export interface SlotProps {
  // the Projects page on a cell with more than one person (TR-1277)
  "projects.switch": { view: ProjectsView; counts: Record<ProjectsView, number>; onView: (v: ProjectsView) => void };
  "projects.lock": NoProps;
  "projects.lead": { p: FactoryProjectRow; me?: string };
  "projects.hidden": { stubs: HiddenProject[]; summary?: string; view: ProjectsView };
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
  "home.crewLimit": HomeCrewLimitProps;
  "home.teamCrews": HomeTeamCrewsProps;
  "home.reposYouOwn": HomeReposProps;
}

export type SlotName = keyof SlotProps;
/** The slots that are a whole page, shown with EePage. */
export type PageSlotName = Extract<SlotName, `page.${string}`>;

/** The samples a page shows, by page; ee adds its own through registerSamples. */
export interface SampleSets {
  home: Record<string, HomeSample>;
  projects: Record<string, FactoryProjectsData>;
}
