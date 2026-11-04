import { Toaster } from "@/components/ui/toaster";
import { useToast as useBaseToast } from "@/components/ui/use-toast";

/** How long a message stays on screen. */
export const TOAST_MS = 4000;

/** Mount once where messages should appear (the page root does not mount it). */
export { Toaster };

/** `const { toast } = useToast(); toast("Copied")`: one sentence that hides itself after 4 seconds. */
export function useToast() {
  const { toast } = useBaseToast();
  return { toast: (message: string) => toast({ description: message, duration: TOAST_MS }) };
}
