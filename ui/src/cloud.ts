import { useEffect, useState } from "react";

import {
  api, cloudApi, type CloudResult, type CloudWorkspace, type CloudWorkspaceOverview,
  type CloudWorkspacesResult,
} from "./api";

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
// /health once it has answered, so a component mounted later (Settings) starts with it and does
// not first render as self-hosted for a moment
let healthNow: Health | undefined;

function loadHealth(): Promise<Health> {
  if (!healthP) {
    healthP = api.health()
      .then((h) => { healthNow = h; return h; })
      .catch((e) => { healthP = null; throw e; });
  }
  return healthP;
}

// every mounted useCloud, so a list fetched again (reloadList) reaches the switcher
const listSubs = new Set<(l: CloudWorkspacesResult) => void>();

function loadList(url: string, fresh = false): Promise<CloudWorkspacesResult> {
  if (!listP || fresh) {
    const p: Promise<CloudWorkspacesResult> = api.cloudWorkspaces(url).then((l) => {
      if (listP === p) listSubs.forEach((f) => f(l));   // a newer fetch wins
      return l;
    });
    listP = p;
  }
  return listP;
}

/** Ask the control plane for the workspace list again, after a change it shows (the sign-in pin). */
export function reloadList(): void {
  const url = healthNow?.workspaces_url;
  if (url) loadList(url, true);
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
  const [health, setHealth] = useState<Health | undefined>(healthNow);
  const [list, setList] = useState<CloudWorkspacesResult>();
  const [ready, setReady] = useState(!!healthNow);
  useEffect(() => {
    let live = true;
    listSubs.add(setList);
    loadHealth().then((h) => {
      if (!live) return;
      setHealth(h); setReady(true);
      if (h.workspaces_url) loadList(h.workspaces_url).then((l) => { if (live) setList(l); });
    }).catch(() => { if (live) setReady(true); });
    return () => { live = false; listSubs.delete(setList); };
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
                          tab: "github" | "slack", back?: string): string {
  const u = new URL(base, window.location.href);
  for (const [k, v] of Object.entries(params)) u.searchParams.set(k, v);
  // where the person comes back to: the Settings tab by default, or `back` (a path on this
  // workspace, e.g. /start); the control plane accepts any URL on the workspace's own origin
  u.searchParams.set("return", `${window.location.origin}${back ?? `/settings?tab=${tab}`}`);
  return u.toString();
}

/** Where the control plane signs the person in again and brings them back here (contract §6). */
export function signInLink(loginUrl: string): string {
  const u = new URL(loginUrl, window.location.href);
  u.searchParams.set("return", window.location.host);
  return u.toString();
}

// This workspace's own record in the control plane (TR-375): fetched once per page load and shared
// by the sidebar (who is signed in) and Settings > Workspace. A change made in Settings (storage,
// the sign-in pin) reaches every component that shows it. A failed load is not kept, so opening
// Settings later asks again.
type OverviewResult = CloudResult<CloudWorkspaceOverview>;
let overviewP: Promise<OverviewResult> | null = null;
let overviewNow: OverviewResult | undefined;
const overviewSubs = new Set<(r: OverviewResult) => void>();

function publishOverview(r: OverviewResult) {
  overviewNow = r;
  overviewSubs.forEach((f) => f(r));
}

function loadOverview(url: string, fresh = false): Promise<OverviewResult> {
  if (!overviewP || fresh) {
    const p: Promise<OverviewResult> = cloudApi.overview(url).then((r) => {
      if (r.status !== "ok" && overviewP === p) overviewP = null;
      publishOverview(r);
      return r;
    });
    overviewP = p;
  }
  return overviewP;
}

export type WorkspaceOverview = {
  /** undefined while loading, or when there is nothing to load (self-host) */
  result?: OverviewResult;
  /** ask the control plane again */
  reload: () => void;
  /** merge what a write changed into the shared copy */
  update: (patch: Partial<CloudWorkspaceOverview>) => void;
};

/** The control plane's view of this workspace, for `url` = /health's workspace_api_url. */
export function useWorkspaceOverview(url?: string): WorkspaceOverview {
  const [result, setResult] = useState<OverviewResult | undefined>(
    url && overviewNow?.status === "ok" ? overviewNow : undefined);
  useEffect(() => {
    if (!url) return;
    overviewSubs.add(setResult);
    if (overviewNow?.status === "ok") setResult(overviewNow);
    else loadOverview(url);
    return () => { overviewSubs.delete(setResult); };
  }, [url]);
  return {
    result,
    reload: () => { if (url) loadOverview(url, true); },
    update: (patch) => {
      if (overviewNow?.status === "ok") {
        publishOverview({ status: "ok", data: { ...overviewNow.data, ...patch } });
      }
    },
  };
}
