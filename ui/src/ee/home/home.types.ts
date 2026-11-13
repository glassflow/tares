// Tares enterprise licence, see ee/LICENSE
// The props of each slot the team home fills, and the types its parts share with the console. The
// console (core) owns them, in src/slotProps.ts and editions/factory/home.types.ts, because core
// renders the slots and may not import this folder; the ee components take them from here.
export type {
  HomeAction, HomeArea, HomeCrewLimit, HomeData, HomeHiddenProject, HomeItem, HomeProject, HomeProjectRow, HomeRef,
  HomeSample, HomeTeamCrew, HomeWaitingOn, ProjectsView, TeamHomeHandler,
  HomeCrewLimitProps, HomeReposProps,
  HomeRun, HomeTeamActionsProps, HomeTeamCrewsProps, HomeWaitingOnProps, HomeWaitingProps,
} from "@/editions/factory/home.types";
export type { SlotName, SlotProps } from "@/slotProps";
