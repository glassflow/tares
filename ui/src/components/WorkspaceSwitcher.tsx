import { useEffect, useRef, useState } from "react";

import type { CloudWorkspace } from "../api";
import { type Cloud, signInLink, slugFromHost } from "../cloud";
import { ChevronRight } from "./icons";

// Tares Cloud (TR-370): the top-left brand becomes a menu of the person's workspaces, so switching
// is one click from the console instead of a trip to the control plane. Opening another workspace
// goes through its `open_url`, the control plane's handoff, which checks membership and hands that
// workspace a key. The list loads in the background: the slug from the address shows at once and
// nothing in the console waits for it.

const STATE_WORDS: Record<string, string> = {
  provisioning: "setting up",
  suspended: "suspended",
  error: "not running",
};

export default function WorkspaceSwitcher({ cloud }: { cloud: Cloud }) {
  const [open, setOpen] = useState(false);
  const [pos, setPos] = useState<{ top: number; left: number }>();
  const wrap = useRef<HTMLDivElement>(null);
  const btn = useRef<HTMLButtonElement>(null);
  const pop = useRef<HTMLDivElement>(null);
  const { health, list, current } = cloud;
  const slug = current?.slug ?? slugFromHost();

  const close = (refocus: boolean) => {
    setOpen(false);
    if (refocus) btn.current?.focus();
  };

  // fixed, measured from the button: the sidebar scrolls, so an absolute popover would be clipped
  const toggle = () => {
    if (!open && btn.current) {
      const r = btn.current.getBoundingClientRect();
      setPos({ top: r.bottom + 4, left: r.left });
    }
    setOpen((o) => !o);
  };

  useEffect(() => {
    if (!open) return;
    pop.current?.querySelector<HTMLElement>("a")?.focus();   // keyboard users start in the menu
    const onDown = (e: MouseEvent) => {
      if (wrap.current && !wrap.current.contains(e.target as Node)) close(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") { e.preventDefault(); close(true); }
    };
    const onResize = () => close(false);
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    window.addEventListener("resize", onResize);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
      window.removeEventListener("resize", onResize);
    };
  }, [open]);

  const links = list?.status === "ok" ? list.links : {};
  const item = (w: CloudWorkspace) => {
    const here = w === current;
    const note = w.is_default ? <span className="ws-note">default</span> : null;
    if (here) {
      return (
        <div key={w.slug} className="menu-item ws-item ws-current" aria-current="true">
          <span className="ws-check" aria-hidden="true">✓</span>
          <span className="ws-slug">{w.slug}</span>{note}
        </div>
      );
    }
    if (w.state !== "active") {
      return (
        <div key={w.slug} className="menu-item ws-item ws-off" aria-disabled="true">
          <span className="ws-check" aria-hidden="true" />
          <span className="ws-slug">{w.slug}</span>
          <span className="ws-note">{STATE_WORDS[w.state] ?? w.state}</span>
        </div>
      );
    }
    return (
      <a key={w.slug} className="menu-item ws-item" href={w.open_url}>
        <span className="ws-check" aria-hidden="true" />
        <span className="ws-slug">{w.slug}</span>{note}
      </a>
    );
  };

  return (
    <div className="brand ws-wrap" ref={wrap}
         onBlur={(e) => {   // Tab out of the menu closes it
           if (open && !wrap.current?.contains(e.relatedTarget as Node | null)) setOpen(false);
         }}>
      <button ref={btn} type="button" className="ws-switch" aria-haspopup="true" aria-expanded={open}
              aria-controls="ws-pop" title="Switch workspace" onClick={toggle}>
        <img className="brand-mark" src="/tares-mark.svg" alt="Tares" />
        <span className="brand-word ws-name">{slug}</span>
        <ChevronRight className="ico ws-chev" />
      </button>
      {open && (
        <div id="ws-pop" ref={pop} className="menu-pop ws-pop" aria-label="Workspaces" tabIndex={-1}
             style={pos && { top: pos.top, left: pos.left }}>
          {list === undefined && <div className="ws-msg">Loading your workspaces…</div>}
          {list?.status === "signed_out" && health?.login_url && (
            <a className="menu-item" href={signInLink(health.login_url)}>Sign in to switch workspaces</a>
          )}
          {list?.status === "unknown" && (
            <div className="ws-msg">Your other workspaces could not be loaded.</div>
          )}
          {list?.status === "ok" && (
            <>
              {list.workspaces.map(item)}
              <div className="menu-sep" role="separator" />
              {health?.workspace_url && (
                <a className="menu-item" href={health.workspace_url}>Workspace settings</a>
              )}
              {links.all && <a className="menu-item" href={links.all}>All workspaces</a>}
              {links.new && <a className="menu-item" href={links.new}>New workspace</a>}
              {links.account && <a className="menu-item" href={links.account}>Account</a>}
            </>
          )}
          {list?.status !== "ok" && !(list?.status === "signed_out" && health?.login_url)
            && health?.workspace_url && (
            <>
              <div className="menu-sep" role="separator" />
              <a className="menu-item" href={health.workspace_url}>Workspace settings</a>
            </>
          )}
        </div>
      )}
    </div>
  );
}
