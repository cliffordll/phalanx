import { createContext, useCallback, useContext, useState } from "react";
import { cn } from "@/lib/utils";

type Tone = "info" | "success" | "error";

interface Toast {
  id: number;
  msg: string;
  tone: Tone;
}

interface ToastApi {
  show: (msg: string, tone?: Tone) => void;
}

const ToastContext = createContext<ToastApi | null>(null);

let _id = 0;

export function ToastProvider({ children }: { children: React.ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);

  const show = useCallback<ToastApi["show"]>((msg, tone = "info") => {
    const id = ++_id;
    setToasts((t) => [...t, { id, msg, tone }]);
    setTimeout(() => {
      setToasts((t) => t.filter((x) => x.id !== id));
    }, 3500);
  }, []);

  return (
    <ToastContext.Provider value={{ show }}>
      {children}
      <div className="pointer-events-none fixed bottom-4 right-4 z-50 flex flex-col gap-2">
        {toasts.map((t) => (
          <div
            key={t.id}
            className={cn(
              "pointer-events-auto rounded-md border px-3 py-2 text-sm shadow",
              t.tone === "success" &&
                "border-emerald-500/40 bg-emerald-500/15 text-emerald-200",
              t.tone === "error" &&
                "border-red-500/40 bg-red-500/15 text-red-200",
              t.tone === "info" &&
                "border-zinc-700 bg-zinc-900 text-zinc-200",
            )}
          >
            {t.msg}
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}

export function useToast(): ToastApi {
  const ctx = useContext(ToastContext);
  if (!ctx) throw new Error("useToast must be used inside <ToastProvider>");
  return ctx;
}
