import { createContext, Fragment, useCallback, useContext, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { flushSync } from "react-dom";
import { NavLink, Outlet, useLocation, useNavigate } from "react-router-dom";

import { api, auth } from "../api";
import { useCloud, useWorkspaceOverview } from "../cloud";
import CommandPalette from "../components/CommandPalette";
import LicenceBanner from "../components/LicenceBanner";
import WorkspaceSwitcher from "../components/WorkspaceSwitcher";
import {
  Activity, BarChart, Book, Bot, Chat, ChevronDown, ChevronRight, Database, Eye, Flag, Folder, Grid, HomeIcon, Moon, Plug,
  Search, Settings as SettingsIcon, Sun, Ticket, Users, Zap,
} from "../components/icons";
import { Button, ProgressBar, usePresence } from "../components/edition";
import { Initials, initialsOf } from "../components/edition/Initials";
import { Input } from "@paper-kit/ui/input";
import { embedded } from "../embed";
import { reducedMotion } from "../lib/motion";
import { applyTheme, currentTheme, type Theme } from "../theme";
import { signOut } from "./account";
import { BrainReadContext, normalizeBrain, type BrainRead } from "./brainRead";
import { useEdition } from "./edition";
import { useSample } from "./sample";
import { askBrainRepos } from "./factory/BrainAsk.api";
import { SETTINGS_ENTRY, visibleEntries, type MenuEntry } from "./menus";
import { hasPage, preloadPage } from "./routes";
import { isSettingsPath, rememberPlace } from "./settingsBack";
import { setupProgress, type SetupProgress } from "./factory/FactorySetup.api";

// The shell of the factory and sre editions: a header, a left menu, and the page in between.
// The page keeps its own heading; the shell adds no breadcrumbs.
//
// Embedded in the Tares desktop (embed.ts, TR-1553) the desktop draws the cell's name, the sign-in and
// the theme, so the header goes: search moves to the top of the menu, a page's header actions get a
// row of their own above the page, and on a phone width only a slim bar with Menu and search stays.

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
const LINK_BASE = "block px-3 py-2 text-sm no-underline hover:bg-muted";
const linkClass = (soft: boolean, narrow: boolean) => ({ isActive }: { isActive: boolean }) =>
  [LINK_BASE, narrow ? "py-2.5" : "",
    isActive ? "bg-muted font-semibold text-foreground shadow-[inset_2px_0_0_var(--accent)]"
      : soft ? "text-muted-foreground" : "text-foreground"].join(" ");
const ROW = "flex w-full items-center justify-between gap-3 border-0 bg-transparent px-3.5 py-2.5 text-left text-sm font-normal text-foreground no-underline hover:bg-muted hover:text-foreground";

const groupSlug = (g: string) => g.toLowerCase().replace(/\s+/g, "-");

// Each page of the menu has its icon, so a page reads apart from a group's heading (which has none).
const MENU_ICONS: Record<string, (p: React.SVGProps<SVGSVGElement>) => React.ReactElement> = {
  home: HomeIcon, integrations: Plug, projects: Folder, tickets: Ticket, reviews: Eye, report: BarChart, crew: Bot, people: Users,
  docs: Book, "brain-repos": Database, "brain-decisions": Flag, "brain-ask": Chat, settings: SettingsIcon,
  services: Grid, incidents: Zap, timeline: Activity, runbooks: Book,
};

function EntryLabel({ e }: { e: MenuEntry }) {
  const Icon = MENU_ICONS[e.key];
  return (
    <span className="flex items-center gap-2.5">
      {Icon ? <Icon className="size-4 flex-none" aria-hidden /> : <span className="size-4 flex-none" aria-hidden />}
      <span>{e.label}</span>
    </span>
  );
}

/** A card on top of a factory cell's menu while setup is not done: how far it is, and the way in.
 *  It goes once every step is done; Integrations has the same sections after that. */
function SetupCard({ setup }: { setup: SetupProgress }) {
  return (
    <NavLink to="/setup" data-testid="menu-setup"
             className="mb-[18px] grid gap-2 border border-border bg-card px-3 py-2.5 text-sm text-foreground no-underline hover:bg-muted">
      <span className="font-semibold">Set up your factory</span>
      <ProgressBar value={setup.total ? (setup.set / setup.total) * 100 : 0} aria-label="Setup done"
                   className="h-1 [&>div:first-child]:bg-foreground" />
      <span className="text-xs text-muted-foreground" data-testid="menu-setup-count">{setup.text}</span>
    </NavLink>
  );
}

// A group the person folded stays folded in this browser. Only a convenience: without storage every
// group is open.
const FOLDED_KEY = "tares.menu_folded";

function readFolded(): string[] {
  try {
    const v = JSON.parse(localStorage.getItem(FOLDED_KEY) ?? "[]");
    return Array.isArray(v) ? v.filter((x) => typeof x === "string") : [];
  } catch { return []; }
}

function saveFolded(groups: string[]): void {
  try { localStorage.setItem(FOLDED_KEY, JSON.stringify(groups)); } catch { /* no storage: not kept */ }
}

function MenuLinks({ entries, narrow, setup }: {
  entries: MenuEntry[]; narrow: boolean; setup?: SetupProgress | null;
}) {
  const { edition } = useEdition();
  // the page's code starts loading while the pointer rests on its link (TR-1433)
  const early = (to: string) => () => preloadPage(edition, to);
  const link = (e: MenuEntry) => (
    <NavLink key={e.key} to={e.to} end={e.to === "/" || e.to === "/brain"} data-testid={`menu-${e.key}`} className={linkClass(false, narrow)}
             onMouseEnter={early(e.to)} onFocus={early(e.to)}>
      <EntryLabel e={e} />
    </NavLink>
  );
  const [folded, setFolded] = useState<string[]>(readFolded);
  const toggle = (g: string) => setFolded((f) => {
    const next = f.includes(g) ? f.filter((x) => x !== g) : [...f, g];
    saveFolded(next);
    return next;
  });
  const { pathname } = useLocation();
  // the page you are on never hides: a folded group with it shows that one entry
  const here = (e: MenuEntry) => (e.to === "/" ? pathname === "/" : pathname === e.to || pathname.startsWith(`${e.to}/`));
  // Entries of one group sit in a role="group" named by the group's heading, in the menu and in the drawer.
  // The heading is a button that folds the group.
  const blocks: { group?: string; items: MenuEntry[] }[] = [];
  for (const e of entries) {
    const last = blocks[blocks.length - 1];
    if (last && last.group === e.group) last.items.push(e);
    else blocks.push({ group: e.group, items: [e] });
  }
  return (
    <>
      {setup && setup.set < setup.total && <SetupCard setup={setup} />}
      {blocks.map((b, i) => b.group ? (
        <div key={b.group} role="group" aria-labelledby={`menu-group-label-${groupSlug(b.group)}`}>
          <button type="button" id={`menu-group-label-${groupSlug(b.group)}`} data-testid={`menu-group-${groupSlug(b.group)}`}
                  aria-expanded={!folded.includes(b.group)} onClick={() => toggle(b.group!)}
                  className={`group flex w-full items-center gap-1 border-0 bg-transparent px-3 pb-1.5 pt-1 text-left text-xs font-semibold text-muted-foreground cursor-pointer hover:text-foreground ${i > 0 ? "mt-[18px]" : ""}`}>
            <span>{b.group}</span>
            {folded.includes(b.group)
              ? <ChevronRight className="size-3 flex-none" aria-hidden />
              : <ChevronDown className="size-3 flex-none opacity-0 group-hover:opacity-100 group-focus-visible:opacity-100" aria-hidden />}
          </button>
          {(folded.includes(b.group) ? b.items.filter(here) : b.items).map(link)}
        </div>
      ) : <Fragment key={`b${i}`}>{b.items.map(link)}</Fragment>)}
      <div className="min-h-6 flex-1" />
      <NavLink to={SETTINGS_ENTRY.to} data-testid={`menu-${SETTINGS_ENTRY.key}`} className={linkClass(true, narrow)}
               onMouseEnter={early(SETTINGS_ENTRY.to)} onFocus={early(SETTINGS_ENTRY.to)}>
        <EntryLabel e={SETTINGS_ENTRY} />
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

  // The page crossfades into the other theme instead of every colour flipping at once (TR-1436): a view
  // transition snapshots the page, swaps the theme, and fades between the two. Where the browser has none,
  // or the person asked for less motion, it switches at once.
  const toggleTheme = () => {
    const next: Theme = theme === "dark" ? "light" : "dark";
    const swap = () => { applyTheme(next); flushSync(() => setTheme(next)); };
    if (!reducedMotion() && typeof document.startViewTransition === "function") document.startViewTransition(swap);
    else swap();
  };

  return (
    <div ref={box} className="relative">
      {/* The name starts with the initials the button shows, so "click AK" works by voice (WCAG 2.5.3, TR-1269). */}
      <button ref={button} type="button" data-testid="account-button" aria-label={`${initialsOf(who)}, account menu`} aria-haspopup="menu" aria-expanded={open}
              onClick={() => setOpen((o) => !o)} className="block border-0 bg-transparent p-0 hover:bg-transparent">
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
                    className="block size-3.5 translate-x-0.5 rounded-full bg-brand transition-transform motion-reduce:transition-none data-[state=dark]:translate-x-[18px] data-[state=dark]:bg-background" />
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

/** A page with its own menu calls `useOwnMenu()`: the main menu hides while it shows. */
const OwnMenu = createContext<(on: boolean) => void>(() => undefined);

export function useOwnMenu() {
  const set = useContext(OwnMenu);
  useLayoutEffect(() => { set(true); return () => set(false); }, [set]);
}

/** Two places in the header a page may fill (through a portal): after the cell name, and before the
 *  account menu. Both are null until the header has mounted. */
interface HeaderSlots { crumb: HTMLElement | null; actions: HTMLElement | null }
const HeaderSlotsContext = createContext<HeaderSlots>({ crumb: null, actions: null });

export function useHeaderSlots(): HeaderSlots {
  return useContext(HeaderSlotsContext);
}

export default function EditionShell() {
  const { edition, name, credentialName, open: openCell, personPicture } = useEdition();
  const narrow = useNarrow();
  const embed = embedded();
  const [drawer, setDrawer] = useState(false);
  const [text, setText] = useState("");
  const { pathname: here, search } = useLocation();
  // A factory cell's Settings has its own sidebar with a Back link, in place of the main menu, and so
  // has a factory project's frame while it shows (TR-1245; today's page for another kind keeps the menu).
  const [frame, setFrame] = useState(false);
  const noMenu = edition === "factory" && (isSettingsPath(here) || frame);
  useEffect(() => { rememberPlace(here, search); }, [here, search]);
  // The Second Brain group shows once the cell says it has a brain. A cell without a licence has no
  // such route (404), and any failed read means no brain too: the group stays out, with no error.
  // The read is shared with the Ask page (brainRead.ts), so that page makes no read of its own.
  const [brain, setBrain] = useState(false);
  const [brainRead, setBrainRead] = useState<BrainRead>({ state: "loading" });
  const brainLive = useRef(true);
  // each read has a number; an answer that is not the latest read's is dropped, so a slow first read
  // cannot overwrite the page's Retry
  const brainSeq = useRef(0);
  // A quiet read (the Repos page's poll) keeps what is shown while it runs; if it fails, the last answer
  // stays beside the error and the menu keeps its Second Brain group.
  const readBrain = useCallback((quiet = false) => {
    const mine = ++brainSeq.current;
    if (!quiet) setBrainRead({ state: "loading" });
    const lastOf = (r: BrainRead) => (r.state === "ok" ? r.data : r.state === "error" ? r.last : undefined);
    const failed = () => {
      if (!brainLive.current || mine !== brainSeq.current) return;
      if (quiet) { setBrainRead((r) => ({ state: "error", last: lastOf(r) })); return; }
      setBrain(false); setBrainRead({ state: "error" });
    };
    askBrainRepos().then(
      (raw) => {
        if (!brainLive.current || mine !== brainSeq.current) return;
        // a body that is not an object (null, a string) is a failed read, not a page to draw (TR-1438)
        const data = normalizeBrain(raw);
        if (!data) return failed();
        setBrain(data.configured);
        setBrainRead({ state: "ok", data });
      },
      failed,
    );
  }, []);
  useEffect(() => {
    brainLive.current = true;
    if (edition !== "factory") { setBrain(false); return; }
    readBrain();
    return () => { brainLive.current = false; };
  }, [edition, readBrain]);
  const brainValue = useMemo(() => ({ read: brainRead, reload: readBrain }), [brainRead, readBrain]);
  const entries = visibleEntries(edition ?? "platform", (to) => hasPage(edition, to), brain);
  // How far the factory is set up, read again on each move so a step done elsewhere shows. A key that
  // may not set the factory up gets a refusal: no Setup card then.
  const [setup, setSetup] = useState<(SetupProgress & { projects: number }) | null>(null);
  // Setup itself, Settings and a project's own pages draw their own menu, so no card: the count is not
  // read there. Known from the address, because those pages load later and say so only once drawn.
  // a sample page shows sample data: it does not read the cell's own setup either
  const sample = useSample();
  const ownMenuPath = here === "/setup" || isSettingsPath(here) || /^\/projects\/(?!new(\/|$))[^/]+/.test(here);
  useEffect(() => {
    if (edition !== "factory") { setSetup(null); return; }
    if (ownMenuPath || noMenu || sample) return;
    let live = true;
    setupProgress().then((p) => { if (live) setSetup(p); }).catch(() => { if (live) setSetup(null); });
    return () => { live = false; };
  }, [edition, here, noMenu, ownMenuPath, sample]);
  const navigate = useNavigate();
  // A cell with no project yet and setup not done opens on Setup when the console is opened on the
  // home. Going home from inside the console (Exit setup, the Home entry) is never sent back.
  // a sample of a page (?sample= with the sample flag on) is never sent anywhere else
  const landing = useRef<string | null>(sample ? null : here);
  useEffect(() => { if (here !== landing.current) landing.current = null; }, [here]);
  useEffect(() => {
    if (!setup || landing.current !== "/" || setup.set >= setup.total || setup.projects > 0) return;
    landing.current = null;
    navigate("/setup", { replace: true });
  }, [setup, navigate]);

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
  const [crumbEl, setCrumbEl] = useState<HTMLElement | null>(null);
  const [actionsEl, setActionsEl] = useState<HTMLElement | null>(null);
  const slots = useMemo(() => ({ crumb: crumbEl, actions: actionsEl }), [crumbEl, actionsEl]);
  const drawerShown = usePresence<HTMLDivElement>(narrow && drawer);

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
      {(!embed || narrow) && (
      <header data-testid={embed ? "embed-bar" : "edition-header"}
              className="sticky top-0 z-30 flex items-center justify-between gap-3 border-b border-border bg-card px-3 py-2.5 md:px-6 md:py-3.5">
        <div className="flex min-w-0 items-center gap-2.5">
          {narrow && !noMenu && (
            <Button ref={menuButton} variant="secondary" data-testid="menu-button" aria-expanded={drawer} aria-controls="edition-drawer"
                    className="mr-1.5 px-3" onClick={() => setDrawer((d) => !d)}>
              Menu
            </Button>
          )}
          {!embed && (<>
          {/* the platform console's brand: the mark and the "tares" wordmark (App.tsx, .brand) */}
          <img src="/tares-mark.svg" alt="" aria-hidden data-testid="brand-mark" className="h-auto w-6 shrink-0" />
          <b className="text-[19px] font-semibold tracking-[-0.02em]" data-testid="brand-word">tares</b>
          <span data-testid="edition-name"
                className={"whitespace-nowrap text-base text-muted-foreground " + (cellName ? "shrink-0" : "min-w-0 truncate")}>{name}</span>
          {!narrow && switcher && (
            <>
              <span aria-hidden className="shrink-0 text-base text-faint">/</span>
              <div data-testid="workspace-switcher" className="min-w-0 max-w-[280px] [&_.ws-wrap]:m-0 [&_.ws-switch]:py-1.5">{switcher}</div>
            </>
          )}
          {cellName && (
            <>
              <span aria-hidden className="shrink-0 text-base text-faint">/</span>
              <span data-testid="cell-name" data-user-text title={cellName} className="min-w-0 truncate text-base">{cellName}</span>
            </>
          )}
          <span ref={setCrumbEl} className="contents" />
          </>)}
        </div>
        <div className="flex shrink-0 items-center gap-2.5">
          {narrow ? (
            <Button variant="secondary" data-testid="edition-search-button" aria-label="Search or ask"
                    className="w-9 px-0 text-muted-foreground" onClick={() => ask("")}>
              <Search className="size-3.5" />
            </Button>
          ) : !embed && (
            <div className="relative w-[280px]">
              <Search className="pointer-events-none absolute left-3 top-1/2 size-3.5 -translate-y-1/2 text-muted-foreground" />
              <Input data-testid="edition-search" aria-label="Search or ask" placeholder="Search or ask" value={text}
                     className="pl-9" onChange={(e) => setText(e.target.value)}
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
          <span ref={setActionsEl} className="contents" />
          {!embed && <AccountMenu who={who} small={small} workspaceUrl={workspaceUrl} logoutUrl={logoutUrl} picture={personPicture} />}
        </div>
      </header>
      )}

      <div className="flex min-h-0 flex-1">
        {!narrow && !noMenu && (
          <nav aria-label="Main" data-testid="edition-menu"
               className={(embed ? "top-0 h-screen" : "top-[65px] h-[calc(100vh-65px)]")
                 + " sticky flex w-[232px] shrink-0 flex-col gap-0.5 self-start overflow-y-auto border-r border-border px-3 py-5"}>
            {embed && (
              <Button variant="secondary" data-testid="menu-search" aria-label="Search or ask"
                      className="mb-[18px] w-full justify-start gap-2 text-muted-foreground" onClick={() => ask("")}>
                <Search className="size-3.5" aria-hidden /> Search or ask
              </Button>
            )}
            <MenuLinks entries={entries} narrow={false} setup={setup} />
          </nav>
        )}
        <main className="main min-w-0 flex-1">
          {/* embedded and wide there is no header: a page's header actions sit here, and the row
              takes no room while no page fills it */}
          {embed && !narrow && <div ref={setActionsEl} data-testid="embed-actions" className="flex justify-end gap-2.5 px-6 pt-3 empty:hidden" />}
          <LicenceBanner />
          <OwnMenu.Provider value={setFrame}>
            <HeaderSlotsContext.Provider value={slots}>
              <BrainReadContext.Provider value={brainValue}>
                {/* each page fades in as it arrives, in opacity only so nothing fixed inside it moves (TR-1433) */}
                <div className="motion-page contents"><Outlet /></div>
              </BrainReadContext.Provider>
            </HeaderSlotsContext.Provider>
          </OwnMenu.Provider>
        </main>
      </div>

      {/* the phone menu slides in over a fading scrim, and back out (TR-1432); while it leaves it no
          longer counts as the menu, so a check or a screen reader sees it gone at once */}
      {drawerShown.mounted && (
        <div ref={drawerShown.ref} data-state={drawerShown.state} className="contents">
          <div data-testid={drawer ? "edition-scrim" : undefined} data-state={drawerShown.state}
               className="motion-scrim fixed inset-x-0 bottom-0 top-[57px] z-40 bg-scrim" onClick={() => setDrawer(false)} />
          <nav id="edition-drawer" aria-label="Main" data-testid={drawer ? "edition-menu" : undefined} data-state={drawerShown.state}
               aria-hidden={drawer ? undefined : true}
               className="motion-drawer data-[state=closed]:pointer-events-none fixed bottom-0 left-0 top-[57px] z-50 flex w-[260px] flex-col gap-0.5 border-r border-foreground bg-card px-3 py-4 shadow-[6px_0_18px_var(--sheet-shadow)]">
            {switcher && !embed && <div data-testid="workspace-switcher" className="mb-3 [&_.ws-wrap]:m-0">{switcher}</div>}
            <MenuLinks entries={entries} narrow setup={setup} />
          </nav>
        </div>
      )}

      <CommandPalette />
    </div>
  );
}
