import { useEffect, useState } from "react";

import { api, type CloudWorkspace, type CloudWorkspacesResult } from "./api";

// Tares Cloud, as the console sees it (P-TR-222). Everything here is driven by public /health
// fields the control plane sets on its cells; on a self-hosted instance none are set and every
// caller behaves exactly as before.
//
// /health and the control plane's workspace list are each fetched once per page load and shared
// by every component that asks (the sidebar's switcher, Settings), so opening Settings does not
// ask the control plane again.

export type Health = Awaited<ReturnType<typeof api.health>>;

let healthP: Promise<Health> | null = null;
let listP: Promise<CloudWorkspacesResult> | null = null;

function loadHealth(): Promise<Health> {
  if (!healthP) healthP = api.health().catch((e) => { healthP = null; throw e; });
  return healthP;
}

function loadList(url: string): Promise<CloudWorkspacesResult> {
  if (!listP) listP = api.cloudWorkspaces(url);
  return listP;
}

export type Cloud = {
  /** undefined until /health has answered */
  health?: Health;
  /** /health has answered, or failed (then everything behaves as on a self-hosted instance) */
  ready: boolean;
  /** the control plane lists this person's workspaces (TARES_WORKSPACES_URL is set) */
  switcher: boolean;
  /** undefined while loading, or when there is no list to load */
  list?: CloudWorkspacesResult;
  /** this workspace's own entry in the list, found by the page's host */
  current?: CloudWorkspace;
  /** true: owner. false: a member who is not the owner. undefined: not known (signed out of the
   *  control plane, an error, self-host), so connect controls stay and the control plane checks. */
  isOwner?: boolean;
};

export function useCloud(): Cloud {
  const [health, setHealth] = useState<Health>();
  const [list, setList] = useState<CloudWorkspacesResult>();
  const [ready, setReady] = useState(false);
  useEffect(() => {
    let live = true;
    loadHealth().then((h) => {
      if (!live) return;
      setHealth(h); setReady(true);
      if (h.workspaces_url) loadList(h.workspaces_url).then((l) => { if (live) setList(l); });
    }).catch(() => { if (live) setReady(true); });
    return () => { live = false; };
  }, []);
  const current = list?.status === "ok"
    ? list.workspaces.find((w) => w.host === window.location.host) : undefined;
  return {
    health, ready, switcher: !!health?.workspaces_url, list, current,
    isOwner: current ? current.role === "owner" : undefined,
  };
}

/** The workspace's slug as the address says it, for before (or without) the list. */
export function slugFromHost(): string {
  return window.location.hostname.split(".")[0] || window.location.host;
}

/** A link to one of the control plane's connect pages: its URL plus `params`, plus `return`, the
 *  Settings tab to come back to. Built with the URL API, never by concatenation (contract §2). */
export function cloudLink(base: string, params: Record<string, string>,
                          tab: "github" | "slack"): string {
  const u = new URL(base, window.location.href);
  for (const [k, v] of Object.entries(params)) u.searchParams.set(k, v);
  u.searchParams.set("return", `${window.location.origin}/settings?tab=${tab}`);
  return u.toString();
}

/** Where the control plane signs the person in again and brings them back here (contract §6). */
export function signInLink(loginUrl: string): string {
  const u = new URL(loginUrl, window.location.href);
  u.searchParams.set("return", window.location.host);
  return u.toString();
}
