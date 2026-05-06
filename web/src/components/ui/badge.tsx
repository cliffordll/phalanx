import type { HTMLAttributes } from "react";
import { cn } from "@/lib/utils";

type Tone = "default" | "muted" | "success" | "warn" | "danger";

const toneClasses: Record<Tone, string> = {
  default: "bg-teal-500/15 text-teal-300 border border-teal-500/30",
  muted: "bg-zinc-700/40 text-zinc-300 border border-zinc-600/40",
  success: "bg-emerald-500/15 text-emerald-300 border border-emerald-500/30",
  warn: "bg-amber-500/15 text-amber-300 border border-amber-500/30",
  danger: "bg-red-500/15 text-red-300 border border-red-500/30",
};

interface BadgeProps extends HTMLAttributes<HTMLSpanElement> {
  tone?: Tone;
}

export function Badge({ tone = "default", className, ...props }: BadgeProps) {
  return (
    <span
      className={cn(
        "inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium",
        toneClasses[tone],
        className,
      )}
      {...props}
    />
  );
}
