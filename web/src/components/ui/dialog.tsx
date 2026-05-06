import { useEffect, useRef } from "react";
import { cn } from "@/lib/utils";

interface DialogProps {
  open: boolean;
  onClose: () => void;
  title?: React.ReactNode;
  children: React.ReactNode;
  className?: string;
}

/**
 * Headless modal — backdrop + centred panel, Esc closes, click-outside
 * closes.  Deliberately minimal so future cherry-picks of upstream's
 * shadcn-style Dialog drop in.
 */
export function Dialog({
  open,
  onClose,
  title,
  children,
  className,
}: DialogProps) {
  const ref = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
      onClick={(e) => {
        if (ref.current && !ref.current.contains(e.target as Node)) {
          onClose();
        }
      }}
    >
      <div
        ref={ref}
        className={cn(
          "max-h-[85vh] w-full max-w-2xl overflow-hidden rounded-lg",
          "border border-zinc-800 bg-zinc-900 shadow-xl flex flex-col",
          className,
        )}
        role="dialog"
        aria-modal="true"
      >
        {title ? (
          <div className="border-b border-zinc-800 px-5 py-3 text-sm font-semibold">
            {title}
          </div>
        ) : null}
        <div className="overflow-auto p-5">{children}</div>
      </div>
    </div>
  );
}
