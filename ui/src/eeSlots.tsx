/// <reference types="vite/client" />
import { useEffect, useSyncExternalStore, type ComponentType, type ReactNode } from "react";

import LicenceNeeded from "@/components/LicenceNeeded";
import { Banner, SkeletonRows } from "@/components/edition";
import { useLicence } from "./licence";

/*
 * The seam between the console and the enterprise code in src/ee. Core pages show ee pieces through
 * named slots; src/ee registers them. This is the only file in the console that reaches into src/ee,
 * and only through a glob: with the folder deleted the glob is empty, nothing registers, every slot
 * shows its fallback, and the console builds and runs as the MIT one.
 */

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type AnyComponent = ComponentType<any>;
export type RegisterSlot = (name: string, component: AnyComponent) => void;

type EeModule = { register?: (registerSlot: RegisterSlot) => void };

// {} when src/ee/index.ts does not exist (the MIT-only build).
const EE = import.meta.glob<EeModule>("./ee/index.ts");
const load = EE["./ee/index.ts"] as (() => Promise<EeModule>) | undefined;

const slots = new Map<string, AnyComponent>();
const listeners = new Set<() => void>();
let loaded = false;
let failed = false;
let started = false;
let version = 0;

function emit() {
  version++;
  listeners.forEach((l) => l());
}

export const registerSlot: RegisterSlot = (name, component) => {
  slots.set(name, component);
  emit();
};

function subscribe(l: () => void) {
  listeners.add(l);
  return () => { listeners.delete(l); };
}
const snapshot = () => version;

/** Fetches the ee chunk once the cell allows enterprise and lets it register its slots. Mounted once,
 *  next to the licence provider. A failed load is logged and leaves every slot empty. */
export function EeLoader() {
  const { eeAllowed } = useLicence();
  useEffect(() => {
    if (!eeAllowed || started || !load) return;
    started = true;
    const fail = (e: unknown) => {
      failed = true;
      console.warn("The enterprise part of the console did not load.", e);
      emit();
    };
    load().then((mod) => {
      try {
        mod.register?.(registerSlot);
      } catch (e) {
        fail(e);
        return;
      }
      loaded = true;
      emit();
    }, fail);
  }, [eeAllowed]);
  return null;
}

/** True once the ee chunk is loaded and has registered. */
export function useEeLoaded(): boolean {
  useSyncExternalStore(subscribe, snapshot);
  return loaded;
}

/** Renders what ee registered under `name`; `fallback` (nothing by default) until then. */
export function Slot({ name, fallback = null, ...props }: { name: string; fallback?: ReactNode; [prop: string]: unknown }) {
  useSyncExternalStore(subscribe, snapshot);
  const C = slots.get(name);
  return C ? <C {...props} /> : <>{fallback}</>;
}

/** A whole ee page: the slot when loaded, a skeleton while the licence is being read, else the licence note. */
export function EePage({ slot }: { slot: string }) {
  const { loading, eeAllowed } = useLicence();
  useSyncExternalStore(subscribe, snapshot);
  const C = slots.get(slot);
  if (C) return <C />;
  if (failed) return <Banner tone="warn">This part of the console did not load. Reload the page to try again.</Banner>;
  if (loading || (eeAllowed && load)) return <SkeletonRows />;
  return <LicenceNeeded variant="page" />;
}
