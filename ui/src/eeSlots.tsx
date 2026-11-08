/// <reference types="vite/client" />
import { useEffect, useSyncExternalStore, type ComponentType, type ReactNode } from "react";

import LicenceNeeded from "@/components/LicenceNeeded";
import { Banner, SkeletonRows } from "@/components/edition";
import { useLicence } from "./licence";
import type { PageSlotName, SampleSets, SlotName, SlotProps } from "./slotProps";

/*
 * The seam between the console and the enterprise code in src/ee. Core pages show ee pieces through
 * named slots; src/ee registers them. This is the only file in the console that reaches into src/ee,
 * and only through a glob: with the folder deleted the glob is empty, nothing registers, every slot
 * shows its fallback, and the console builds and runs as the MIT one.
 */

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type AnyComponent = ComponentType<any>;
/** Each slot takes the props SlotProps (src/slotProps.ts) lists for its name, on both sides. */
export type RegisterSlot = <N extends SlotName>(name: N, component: ComponentType<SlotProps[N]>) => void;
/** Samples ee adds to a page's own, by page: the page shows them next to its own. */
export type RegisterSamples = <P extends keyof SampleSets>(page: P, samples: SampleSets[P]) => void;

type EeModule = { register?: (registerSlot: RegisterSlot, registerSamples: RegisterSamples) => void };

// {} when src/ee/index.ts does not exist (the MIT-only build).
const EE = import.meta.glob<EeModule>("./ee/index.ts");
const load = EE["./ee/index.ts"] as (() => Promise<EeModule>) | undefined;

const slots = new Map<string, AnyComponent>();
const sampleSets = new Map<string, Record<string, unknown>>();
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

export const registerSamples: RegisterSamples = (page, samples) => {
  sampleSets.set(page, { ...sampleSets.get(page), ...samples });
  emit();
};

const NO_SAMPLES = {};

/** The samples ee registered for a page; none until the ee chunk has loaded (and none in the MIT build). */
export function useEeSamples<P extends keyof SampleSets>(page: P): SampleSets[P] {
  useSyncExternalStore(subscribe, snapshot);
  return (sampleSets.get(page) ?? NO_SAMPLES) as SampleSets[P];
}

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
        mod.register?.(registerSlot, registerSamples);
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

/** True while the cell allows enterprise but the ee chunk has not registered yet (and has not failed):
 *  a page that would show an ee piece in its place shows a skeleton, so core's plainer version never flashes. */
export function useEePending(): boolean {
  const { eeAllowed, loading } = useLicence();
  useSyncExternalStore(subscribe, snapshot);
  return (loading || eeAllowed) && !!load && !loaded && !failed;
}

/** True when the ee chunk failed to load or to register. */
export function useEeFailed(): boolean {
  useSyncExternalStore(subscribe, snapshot);
  return failed;
}

/** The plain line for a failed ee chunk, the same wherever it shows. */
export const EE_FAILED_WORDS = "This part of the console did not load. Reload the page to try again.";

/** True once the ee chunk is loaded and has registered. */
export function useEeLoaded(): boolean {
  useSyncExternalStore(subscribe, snapshot);
  return loaded;
}

/** Renders what ee registered under `name`; `fallback` (nothing by default) until then. */
export function Slot<N extends SlotName>({ name, fallback = null, ...props }: { name: N; fallback?: ReactNode } & SlotProps[N]) {
  useSyncExternalStore(subscribe, snapshot);
  const C = slots.get(name);
  return C ? <C {...props} /> : <>{fallback}</>;
}

/** A whole ee page: the slot when loaded, a skeleton while the licence is being read, else the licence note. */
export function EePage<N extends PageSlotName>({ slot, ...props }: { slot: N } & SlotProps[N]) {
  const { loading, eeAllowed } = useLicence();
  useSyncExternalStore(subscribe, snapshot);
  const C = slots.get(slot);
  if (C) return <C {...props} />;
  if (failed) return <Banner tone="warn" data-testid="ee-failed">{EE_FAILED_WORDS}</Banner>;
  if (loading || (eeAllowed && load)) return <SkeletonRows />;
  return <LicenceNeeded variant="page" />;
}
