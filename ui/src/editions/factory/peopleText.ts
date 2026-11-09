// Small pieces the People page and its enterprise parts share.

export const MUTED = "text-[13px] text-muted-foreground";
export const LINK = "text-foreground! underline! decoration-border! underline-offset-[3px]";

/** A refusal as one sentence that ends with a full stop. */
export const sentence = (s: string) => {
  const t = s.trim();
  return t ? t.charAt(0).toUpperCase() + t.slice(1) + (/[.!?]$/.test(t) ? "" : ".") : "That did not go through.";
};
