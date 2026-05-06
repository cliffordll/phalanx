import { forwardRef, type SelectHTMLAttributes } from "react";
import { cn } from "@/lib/utils";

/**
 * Plain native <select> with the same dark styling as Input.  shadcn's
 * radix-based Select is overkill for the dashboard's filter dropdowns;
 * this keeps the bundle lean and is keyboard-accessible by default.
 */
export const Select = forwardRef<
  HTMLSelectElement,
  SelectHTMLAttributes<HTMLSelectElement>
>(({ className, children, ...props }, ref) => (
  <select
    ref={ref}
    className={cn(
      "flex h-9 w-full rounded-md border border-zinc-700 bg-zinc-900 px-2 text-sm",
      "focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-teal-400",
      "disabled:opacity-50",
      className,
    )}
    {...props}
  >
    {children}
  </select>
));
Select.displayName = "Select";
