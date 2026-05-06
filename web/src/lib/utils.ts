import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

/** Tailwind class merger — dedupes conflicting utilities so e.g.
 * `cn("px-2", isWide && "px-4")` resolves cleanly. */
export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs));
}
