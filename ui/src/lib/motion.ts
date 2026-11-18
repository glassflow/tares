// The motion tokens for motion driven from script (TR-1431): the list glide (auto-animate) and the
// theme crossfade. They mirror --dur-* and --ease-out in styles.css; `npm run motion-contract`
// (tests/motion.check.cjs) fails when the two disagree.

export const DUR = { 1: 120, 2: 180, 3: 260, 4: 900 } as const;
export const EASE_OUT = "cubic-bezier(0.2, 0.8, 0.2, 1)";

/** True when the person asked the OS for less motion. Script-driven motion checks it; CSS has the guard. */
export function reducedMotion(): boolean {
  return typeof window !== "undefined" && !!window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
}
