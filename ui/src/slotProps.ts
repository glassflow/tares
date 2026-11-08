import type { Ctx } from "./components/project/common";
import type {
  HomeCrewLimitProps, HomeHiddenRowsProps, HomeProjectLeadProps, HomeProjectsSwitchProps, HomeReposProps,
  HomeSample, HomeTeamActionsProps, HomeTeamCrewsProps, HomeWaitingOnProps, HomeWaitingProps,
} from "./editions/factory/home.types";
import type { Person } from "./types";

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
  // settings
  "page.settings.areas": NoProps;
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
}

export type SlotName = keyof SlotProps;
/** The slots that are a whole page, shown with EePage. */
export type PageSlotName = Extract<SlotName, `page.${string}`>;

/** The samples a page shows, by page; ee adds its own through registerSamples. */
export interface SampleSets {
  home: Record<string, HomeSample>;
}
