import { auth } from "../api";

/** Forget this console's key. On Tares Cloud (logout_url set, TR-377) then end the Tares Cloud
 *  session too, which lands on its sign-in page; self-hosted, back to the login form. */
export function signOut(logoutUrl?: string) {
  auth.clear();
  if (logoutUrl) window.location.assign(logoutUrl);
  else window.location.reload();
}
