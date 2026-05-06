import { forwardRef, type InputHTMLAttributes } from "react";
import { cn } from "@/lib/utils";

export const Input = forwardRef<
  HTMLInputElement,
  InputHTMLAttributes<HTMLInputElement>
>(({ className, ...props }, ref) => (
  <input
    ref={ref}
    className={cn(
      "flex h-9 w-full rounded-md border border-zinc-700 bg-zinc-900 px-3 py-1 text-sm",
      "placeholder:text-zinc-500",
      "focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-teal-400",
      "disabled:opacity-50",
      className,
    )}
    {...props}
  />
));
Input.displayName = "Input";
