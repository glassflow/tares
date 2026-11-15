// The page you were on before Settings, for its Back link. Settings hides the main menu, so Back is
// the way out. Remembered in memory: a fresh load of Settings goes back to Home. A factory project's
// pages hide the main menu too (TR-1245) and have their own Back: to the page before the project,
// Projects on a fresh load.

let before = "/";
let beforeProject = "/projects";

/** Somewhere in one project: its pages and its tickets' pages, never New project. */
const inProject = (path: string) => /^\/projects\/(?!new(?:\/|$))[^/]+/.test(path);

export const placeBeforeProject = () => beforeProject;

export const isSettingsPath = (path: string) => path === "/settings" || path.startsWith("/settings/");

export function rememberPlace(path: string, search: string) {
  if (!isSettingsPath(path)) before = path + search;
  if (!inProject(path) && !isSettingsPath(path)) beforeProject = path + search;
}

export const placeBeforeSettings = () => before;
