// Generated shadcn files (`npx shadcn add ...`) import icons from lucide-react, because
// components.json says iconLibrary lucide. The console has no such dependency: after generating
// a file, change its icon imports to `@/components/icons` (add the icon there if it is missing).
import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}
