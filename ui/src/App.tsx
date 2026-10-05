import { useEffect, useState } from "react";
import { useProjectName } from "./components/ProjectBadge";
import { Link, NavLink, Outlet, useLocation } from "react-router-dom";

import { api, auth } from "./api";
import { useCloud, useWorkspaceOverview } from "./cloud";
import CommandPalette from "./components/CommandPalette";
import WorkspaceSwitcher from "./components/WorkspaceSwitcher";
import {
  Activity, Chat, ChevronRight, Database, GitHub, Grid, Lock, Moon,
  Settings, SignOut, Sun, Terminal, Zap,
} from "./components/icons";
import { applyTheme, currentTheme, type Theme } from "./theme";

const link = ({ isActive }: { isActive: boolean }) => "navlink" + (isActive ? " active" : "");

type NavItem = {
  to: string;
  end?: boolean;
  label: string;
  icon: (p: { className?: string }) => JSX.Element;
  badge?: string;   // small uppercase tag, e.g. "beta"
  locked?: boolean; // shows a lock glyph for gated features
  kbd?: string;     // keyboard-shortcut hint, e.g. "⌘K"
};

// Two named groups. Projects are the product, so they sit at the top with Overview and Ask;
// everything a project is made of is one flat Catalog group ordered along the pipeline
// (sources feed triggers, triggers run agents, agents leave firings).
// The old Data / Automate split was mechanism vocabulary and is gone.
const NAV_GROUPS: { section: string; items: NavItem[] }[] = [
  { section: "", items: [
    { to: "/", end: true, label: "Overview", icon: Grid },
    { to: "/projects", label: "Projects", icon: Zap },
    { to: "/ask", label: "Ask", icon: Chat, kbd: "⌘K" },
  ] },
  { section: "Catalog", items: [
    { to: "/sources", label: "Sources", icon: Database },
    { to: "/explore", label: "Explore", icon: Activity },
    { to: "/resources", label: "All resources", icon: Grid },
  ] },
  { section: "Agent access", items: [
    { to: "/connect", label: "Connect", icon: Terminal },
    { to: "/reads", label: "Reads", icon: Activity },
  ] },
];

const SECTION_LABEL: Record<string, string> = {
  sources: "Sources",
  explore: "Explore",
  triggers: "Triggers",
  agents: "Tares agents",
  firings: "Firings",
  connect: "Connect",
  reads: "Reads",
  catalog: "Catalog",
  "mcp-servers": "MCP servers",
  ask: "Ask",
  settings: "Settings",
  projects: "Projects",
  resources: "All resources",
};

type Crumb = { label: string; to?: string; mono?: boolean };

/** Derive breadcrumbs from the current path. `/` is Overview; every section is a sibling of it,
 *  so second-level pages link back to their own list (/sources/:name → Sources) rather than to
 *  the root the way they did when Sources *was* the root. */
function useCrumbs(): Crumb[] {
  const { pathname, search } = useLocation();
  const parts = pathname.replace(/^\/+|\/+$/g, "").split("/").filter(Boolean);
  const projectId = parts[0] === "projects" && parts.length > 1 && parts[1] !== "new" ? decodeURIComponent(parts[1]) : undefined;
  const projectName = useProjectName(projectId);

  if (parts.length === 0) return [{ label: "Overview" }];

  if (parts[0] === "sources") {
    if (parts.length === 1) return [{ label: "Sources" }];
    const sub = decodeURIComponent(parts[1]);
    const last: Crumb =
      sub === "discover" ? { label: "Auto-discover" }
      : sub === "new" ? { label: "Add source" }
      : sub === "claude-code" ? { label: "Claude Code" }
      : { label: sub, mono: true };
    return [{ label: "Sources", to: "/sources" }, last];
  }

  if (parts[0] === "projects" && parts.length > 1) {
    const sub = decodeURIComponent(parts[1]);
    // the path carries the instance id; show its name once /api/projects has answered
    const last: Crumb = sub === "new" ? { label: "New project" } : { label: projectName?.name ?? "\u2026" };
    // the guided setup of a project that exists: Projects > name > Set up
    if (sub !== "new" && parts[2] === "setup") {
      return [{ label: "Projects", to: "/projects" }, { ...last, to: `/projects/${encodeURIComponent(sub)}` },
              { label: "Set up" }];
    }
    // past the project's Overview: a result, How it works, or the full setup
    const q = new URLSearchParams(search);
    // old ?tab= and ?session= links land in the full setup too
    const kind = sub === "new" ? undefined
      : q.get("view")?.split(":")[0] || (q.get("tab") || q.get("session") ? "setup" : undefined);
    if (kind && kind !== "overview") {
      const here = kind === "result" ? "Result" : kind === "how" ? "Setup" : "Advanced setup";
      return [{ label: "Projects", to: "/projects" }, { ...last, to: `/projects/${encodeURIComponent(sub)}` }, { label: here }];
    }
    return [{ label: "Projects", to: "/projects" }, last];
  }

  if (parts[0] === "agents" && parts.length > 1) {
    const sub = decodeURIComponent(parts[1]);
    const last: Crumb = sub === "new" ? { label: "Create agent" } : { label: sub, mono: true };
    return [{ label: "Tares agents", to: "/agents" }, last];
  }

  return [{ label: SECTION_LABEL[parts[0]] ?? parts[0] }];
}

function Breadcrumbs() {
  const crumbs = useCrumbs();
  return (
    <nav className="crumbs" aria-label="Breadcrumb">
      {crumbs.map((c, i) => {
        const last = i === crumbs.length - 1;
        return (
          <span className="crumb" key={i}>
            {i > 0 && <span className="sep"><ChevronRight className="ico" /></span>}
            {c.to && !last
              ? <Link to={c.to}>{c.label}</Link>
              : <span className={"cur" + (c.mono ? " mono" : "")}>{c.label}</span>}
          </span>
        );
      })}
    </nav>
  );
}

/** Forget this console's key. On Tares Cloud (logout_url set, TR-377) then end the Tares Cloud
 *  session too, which lands on its sign-in page; self-hosted, back to the login form. */
function signOut(logoutUrl?: string) {
  auth.clear();
  if (logoutUrl) window.location.assign(logoutUrl);
  else window.location.reload();
}

function ThemeToggle() {
  const [theme, setTheme] = useState<Theme>(currentTheme);
  const toggle = () => {
    const next: Theme = theme === "dark" ? "light" : "dark";
    applyTheme(next);
    setTheme(next);
  };
  return (
    <button className="navbtn" onClick={toggle} title="Toggle light / dark">
      {theme === "dark" ? <Sun /> : <Moon />}
      <span className="nav-label">{theme === "dark" ? "Light mode" : "Dark mode"}</span>
    </button>
  );
}

export default function App() {
  const [version, setVersion] = useState<string | null>(null);
  // Cloud only: the control-plane workspace this cell belongs to. Users, plan, storage and the
  // Slack app are managed there; the link is the missing half of Settings (TR-142). Self-host
  // sets no TARES_WORKSPACE_URL and never sees it.
  // With TARES_WORKSPACES_URL as well (TR-370), the top left is a workspace switcher and this
  // link moves into it; an older control plane that only sets the workspace URL keeps it here.
  // With TARES_WORKSPACE_API_URL (TR-375) the workspace is managed in Settings > Workspace, so the
  // link out goes away whatever else is set.
  const cloud = useCloud();
  const workspaceUrl = cloud.switcher || cloud.health?.workspace_api_url
    ? undefined : cloud.health?.workspace_url || undefined;
  // Tares Cloud: who is signed in, from the control plane's view of this workspace (shared with
  // Settings, loaded once). Nothing shows while it is unknown.
  const logoutUrl = cloud.health?.logout_url || undefined;
  const overview = useWorkspaceOverview(logoutUrl ? cloud.health?.workspace_api_url || undefined : undefined);
  const email = overview.result?.status === "ok" ? overview.result.data.you?.email : undefined;
  useEffect(() => {
    api.capabilities().then((c) => setVersion(c.version ?? null)).catch(() => {});
  }, []);
  return (
    <>
      <nav className="sidebar">
        <div className={"brand" + (cloud.switcher ? " with-switcher" : "")}>
          <img className="brand-mark" src="/tares-mark.svg" alt="Tares" />
          <span className="brand-word">tares</span>
        </div>
        {cloud.switcher && <WorkspaceSwitcher cloud={cloud} />}

        {NAV_GROUPS.map(({ section, items }) => (
          <div className="nav-group" key={section}>
            {/* Overview and Ask have no section heading: they sit above the groups. */}
            {section && <div className="nav-section">{section}</div>}
            {items.map(({ to, end, label, icon: Icon, badge, locked, kbd }) => (
              <NavLink key={to} to={to} end={end} className={link}>
                <Icon className="ico" />
                <span className="nav-label">{label}</span>
                {badge && <span className="nav-badge">{badge}</span>}
                {locked && <Lock className="nav-lock" />}
                {kbd && <kbd className="nav-kbd" title="Ask from anywhere">{kbd}</kbd>}
              </NavLink>
            ))}
          </div>
        ))}

        <div className="nav-spacer" />
        <div className="sep" />

        <NavLink to="/settings" className={link}>
          <Lock className="ico" />
          <span className="nav-label">Settings</span>
        </NavLink>
        {workspaceUrl && (
          <a className="navlink" href={workspaceUrl} title="users, plan, storage and the Slack app: managed in your workspace">
            <Settings className="ico" />
            <span className="nav-label">Workspace</span>
            <span className="nav-badge">↗</span>
          </a>
        )}
        <ThemeToggle />
        {logoutUrl && email && <div className="who" title={`Signed in as ${email}`}>{email}</div>}
        {auth.get() && (
          <button className="navbtn" onClick={() => signOut(logoutUrl)}>
            <SignOut className="ico" />
            <span className="nav-label">Sign out</span>
          </button>
        )}
        <div className="foot">
          {version && <span>v{version}</span>}
          <a href="https://github.com/glassflow/tares" target="_blank" rel="noreferrer"
             title="Tares on GitHub" aria-label="Tares on GitHub">
            <GitHub />
          </a>
        </div>
      </nav>

      <div className="content">
        <header className="topbar">
          <Breadcrumbs />
        </header>
        <main className="main">
          <Outlet />
        </main>
      </div>

      <CommandPalette />
    </>
  );
}
