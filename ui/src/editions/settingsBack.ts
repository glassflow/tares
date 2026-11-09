// The page you were on before Settings, for its Back link. Settings hides the main menu, so Back is
// the way out. Remembered in memory: a fresh load of Settings goes back to Home.

let before = "/";

export const isSettingsPath = (path: string) => path === "/settings" || path.startsWith("/settings/");

export function rememberPlace(path: string, search: string) {
  if (!isSettingsPath(path)) before = path + search;
}

export const placeBeforeSettings = () => before;
