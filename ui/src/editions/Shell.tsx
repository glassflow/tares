import { useEffect, useRef, useState } from "react";
import { NavLink, Outlet, useLocation } from "react-router-dom";

import { api, auth } from "../api";
import { useCloud, useWorkspaceOverview } from "../cloud";
import CommandPalette from "../components/CommandPalette";
import WorkspaceSwitcher from "../components/WorkspaceSwitcher";
import { Moon, Search, Sun } from "../components/icons";
import { Button } from "../components/edition";
import { Initials } from "../components/edition/Initials";
import { Input } from "../components/ui/input";
import { applyTheme, currentTheme, type Theme } from "../theme";
import { signOut } from "./account";
import { useEdition } from "./edition";
import { SETTINGS_ENTRY, visibleEntries, type MenuEntry } from "./menus";
import { hasPage } from "./routes";

// The shell of the factory and sre editions: a header, a left menu, and the page in between.
// The page keeps its own heading; the shell adds no breadcrumbs.

const NARROW = "(max-width: 767px)";

/** True under 768px wide. The menu then sits behind a button and search becomes an icon. */
function useNarrow(): boolean {
  const [narrow, setNarrow] = useState(() => window.matchMedia(NARROW).matches);
  useEffect(() => {
    const mq = window.matchMedia(NARROW);
    const on = () => setNarrow(mq.matches);
    mq.addEventListener("change", on);
    return () => mq.removeEventListener("change", on);
  }, []);
  return narrow;
}

// The console's own `a` and `button` rules are unlayered and beat plain utilities, so the
// properties they also set are marked important.
const LINK_BASE = "block px-3 py-2 text-sm no-underline! hover:bg-muted";
const linkClass = (soft: boolean, narrow: boolean) => ({ isActive }: { isActive: boolean }) =>
  [LINK_BASE, narrow ? "py-2.5" : "",
    isActive ? "bg-muted font-semibold text-foreground! shadow-[inset_2px_0_0_var(--accent)]"
      : soft ? "text-muted-foreground!" : "text-foreground!"].join(" ");
const ROW = "flex! w-full items-center justify-between gap-3 border-0! bg-transparent! px-3.5! py-2.5! text-left text-sm! font-normal! text-foreground! no-underline! hover:bg-muted! hover:text-foreground!";

function MenuLinks({ entries, narrow }: { entries: MenuEntry[]; narrow: boolean }) {
  return (
    <>
      {entries.map((e) => (
        <NavLink key={e.key} to={e.to} end={e.to === "/"} data-testid={`menu-${e.key}`} className={linkClass(false, narrow)}>
          {e.label}
        </NavLink>
      ))}
      <div className="min-h-6 flex-1" />
      <NavLink to={SETTINGS_ENTRY.to} data-testid={`menu-${SETTINGS_ENTRY.key}`} className={linkClass(true, narrow)}>
        {SETTINGS_ENTRY.label}
      </NavLink>
    </>
  );
}

function AccountMenu({ who, small, workspaceUrl, logoutUrl, picture }: {
  who: string; small: string; workspaceUrl?: string; logoutUrl?: string; picture?: string;
}) {
  const [open, setOpen] = useState(false);
  const [theme, setTheme] = useState<Theme>(currentTheme);
  const [version, setVersion] = useState<string | null>(null);
  const box = useRef<HTMLDivElement>(null);
  const button = useRef<HTMLButtonElement>(null);
  const here = useLocation().pathname;

  // The version is read when the menu first opens, so the frame itself adds no read call.
  useEffect(() => {
    if (!open || version !== null) return;
    api.capabilities().then((c) => setVersion(c.version ?? "")).catch(() => {});
  }, [open, version]);
  useEffect(() => { setOpen(false); }, [here]);
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => { if (box.current && !box.current.contains(e.target as Node)) setOpen(false); };
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") { setOpen(false); button.current?.focus(); } };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => { document.removeEventListener("mousedown", onDown); document.removeEventListener("keydown", onKey); };
  }, [open]);

  const toggleTheme = () => {
    const next: Theme = theme === "dark" ? "light" : "dark";
    applyTheme(next);
    setTheme(next);
  };

  return (
    <div ref={box} className="relative">
      <button ref={button} type="button" data-testid="account-button" aria-label="Account menu" aria-haspopup="menu" aria-expanded={open}
              onClick={() => setOpen((o) => !o)} className="block! border-0! bg-transparent! p-0! hover:bg-transparent!">
        <Initials name={who} picture={picture} aria-hidden data-testid="account-avatar" />
      </button>
      {open && (
        <div role="menu" data-testid="account-menu"
             className="absolute right-0 top-11 z-50 w-[260px] border border-foreground bg-card shadow-[0_6px_18px_var(--sheet-shadow)]">
          <div className="border-b border-border px-3.5 py-3">
            <div className="truncate font-semibold" data-user-text>{who}</div>
            <div className="text-xs text-muted-foreground">{small}</div>
          </div>
          {/* The row says the mode you are in; the round toggle shows which side you are on, and the
              whole row switches to the other. */}
          <button type="button" role="menuitem" aria-label={theme === "dark" ? "Switch to light mode" : "Switch to dark mode"}
                  data-testid="theme-row" data-theme-now={theme} className={ROW} onClick={toggleTheme}>
            <span className="flex items-center gap-2" data-testid="theme-now">
              {theme === "dark" ? <Moon aria-hidden /> : <Sun aria-hidden />}
              {theme === "dark" ? "Dark mode" : "Light mode"}
            </span>
            <span aria-hidden data-state={theme === "dark" ? "dark" : "light"}
                  className="relative inline-flex h-5 w-9 shrink-0 items-center rounded-full border border-border bg-muted transition-colors motion-reduce:transition-none data-[state=dark]:border-foreground data-[state=dark]:bg-foreground">
              <span data-state={theme === "dark" ? "dark" : "light"}
                    className="block size-3.5 translate-x-0.5 rounded-full bg-[var(--accent)] transition-transform motion-reduce:transition-none data-[state=dark]:translate-x-[18px] data-[state=dark]:bg-background" />
            </span>
          </button>
          {workspaceUrl && (
            <a role="menuitem" className={ROW} href={workspaceUrl} target="_blank" rel="noreferrer">
              <span>Workspace</span>
              <span className="text-muted-foreground">opens in a new tab</span>
            </a>
          )}
          {auth.get() && (
            <button type="button" role="menuitem" className={ROW} onClick={() => signOut(logoutUrl)}>Sign out</button>
          )}
          {version && (
            <div data-testid="account-version" className="flex justify-between border-t border-border px-3.5 py-2.5 text-xs text-muted-foreground">
              <span>Version</span>
              <span>Tares {version}</span>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export default function EditionShell() {
  const { edition, name, credentialName, open: openCell, personPicture } = useEdition();
  const narrow = useNarrow();
  const [drawer, setDrawer] = useState(false);
  const [text, setText] = useState("");
  const here = useLocation().pathname;
  const entries = visibleEntries(edition ?? "platform", (to) => hasPage(edition, to));

  // Same sources as the platform sidebar: the cloud's workspace link, the control plane's sign-out
  // address and who is signed in there.
  const cloud = useCloud();
  const workspaceUrl = cloud.switcher || cloud.health?.workspace_api_url
    ? undefined : cloud.health?.workspace_url || undefined;
  const logoutUrl = cloud.health?.logout_url || undefined;
  const overview = useWorkspaceOverview(logoutUrl ? cloud.health?.workspace_api_url || undefined : undefined);
  const email = overview.result?.status === "ok" ? overview.result.data.you?.email : undefined;
  const who = email || credentialName || "Account";
  const small = email ? "Signed in to this workspace" : openCell ? "This cell has no sign-in" : "Signed in with an API key";
  // Wide, the workspace switcher sits in the header (it names the workspace, so it is the cell name
  // too). Narrow, it moves to the top of the menu and an AI SRE header keeps the plain name.
  const switcher = cloud.switcher ? <WorkspaceSwitcher cloud={cloud} /> : null;
  const cellName = narrow && edition === "sre" ? cloud.current?.slug : undefined;
  const menuButton = useRef<HTMLButtonElement>(null);

  useEffect(() => { setDrawer(false); }, [here]);
  useEffect(() => { if (!narrow) setDrawer(false); }, [narrow]);
  useEffect(() => {
    if (!drawer) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") { setDrawer(false); menuButton.current?.focus(); } };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [drawer]);

  const ask = (detail: string) => window.dispatchEvent(new CustomEvent("tares:ask", { detail }));

  return (
    <div className="flex min-h-screen w-full min-w-0 flex-col">
      <header data-testid="edition-header"
              className="sticky top-0 z-30 flex items-center justify-between gap-3 border-b border-border bg-card px-3 py-2.5 md:px-6 md:py-3.5">
        <div className="flex min-w-0 items-center gap-2.5">
          {narrow && (
            <Button ref={menuButton} variant="secondary" data-testid="menu-button" aria-expanded={drawer} aria-controls="edition-drawer"
                    className="mr-1.5 px-3!" onClick={() => setDrawer((d) => !d)}>
              Menu
            </Button>
          )}
          {/* the platform console's brand: the mark and the "tares" wordmark (App.tsx, .brand) */}
          <img src="/tares-mark.svg" alt="" aria-hidden data-testid="brand-mark" className="h-auto w-6 shrink-0" />
          <b className="text-[19px] font-semibold tracking-[-0.02em]" data-testid="brand-word">tares</b>
          <span data-testid="edition-name"
                className={"whitespace-nowrap text-base text-muted-foreground " + (cellName ? "shrink-0" : "min-w-0 truncate")}>{name}</span>
          {!narrow && switcher && (
            <>
              <span aria-hidden className="shrink-0 text-base text-[var(--ink-faint)]">/</span>
              <div data-testid="workspace-switcher" className="min-w-0 max-w-[280px] [&_.ws-wrap]:m-0! [&_.ws-switch]:py-1.5!">{switcher}</div>
            </>
          )}
          {cellName && (
            <>
              <span aria-hidden className="shrink-0 text-base text-[var(--ink-faint)]">/</span>
              <span data-testid="cell-name" data-user-text title={cellName} className="min-w-0 truncate text-base">{cellName}</span>
            </>
          )}
        </div>
        <div className="flex shrink-0 items-center gap-2.5">
          {narrow ? (
            <Button variant="secondary" data-testid="edition-search-button" aria-label="Search or ask"
                    className="w-9 px-0! text-muted-foreground!" onClick={() => ask("")}>
              <Search className="size-3.5" />
            </Button>
          ) : (
            <div className="relative w-[280px]">
              <Search className="pointer-events-none absolute left-3 top-1/2 size-3.5 -translate-y-1/2 text-muted-foreground" />
              <Input data-testid="edition-search" aria-label="Search or ask" placeholder="Search or ask" value={text}
                     className="pl-9!" onChange={(e) => setText(e.target.value)}
                     onKeyDown={(e) => {
                       if (e.key !== "Enter" || e.nativeEvent.isComposing) return;
                       e.preventDefault();
                       const q = text.trim();
                       if (!q) return;
                       ask(q);
                       setText("");
                     }} />
            </div>
          )}
          <AccountMenu who={who} small={small} workspaceUrl={workspaceUrl} logoutUrl={logoutUrl} picture={personPicture} />
        </div>
      </header>

      <div className="flex min-h-0 flex-1">
        {!narrow && (
          <nav aria-label="Main" data-testid="edition-menu"
               className="sticky top-[65px] flex h-[calc(100vh-65px)] w-[232px] shrink-0 flex-col gap-0.5 self-start overflow-y-auto border-r border-border px-3 py-5">
            <MenuLinks entries={entries} narrow={false} />
          </nav>
        )}
        <main className="main min-w-0 flex-1">
          <Outlet />
        </main>
      </div>

      {narrow && drawer && (
        <>
          <div data-testid="edition-scrim" className="fixed inset-x-0 bottom-0 top-[57px] z-40 bg-[var(--overlay)]" onClick={() => setDrawer(false)} />
          <nav id="edition-drawer" aria-label="Main" data-testid="edition-menu"
               className="fixed bottom-0 left-0 top-[57px] z-50 flex w-[260px] flex-col gap-0.5 border-r border-foreground bg-card px-3 py-4 shadow-[6px_0_18px_var(--sheet-shadow)]">
            {switcher && <div data-testid="workspace-switcher" className="mb-3 [&_.ws-wrap]:m-0!">{switcher}</div>}
            <MenuLinks entries={entries} narrow />
          </nav>
        </>
      )}

      <CommandPalette />
    </div>
  );
}
