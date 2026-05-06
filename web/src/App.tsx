import { NavLink, Route, Routes, Navigate } from "react-router-dom";

import { ToastProvider } from "@/components/ui/toast";
import StatusPage from "@/pages/StatusPage";
import SessionsPage from "@/pages/SessionsPage";
import LogsPage from "@/pages/LogsPage";
import ConfigPage from "@/pages/ConfigPage";
import EnvPage from "@/pages/EnvPage";
import AnalyticsPage from "@/pages/AnalyticsPage";
import { cn } from "@/lib/utils";

interface NavEntry {
  label: string;
  to: string;
}

// Phalanx MVP §2.7 — only pages with a backing subsystem appear here.
// Each §2.8 deliverable adds its NavBar entry alongside its own page.
const NAV: NavEntry[] = [
  { label: "Status", to: "/status" },
  { label: "Sessions", to: "/sessions" },
  { label: "Logs", to: "/logs" },
  { label: "Analytics", to: "/analytics" },
  { label: "Config", to: "/config" },
  { label: "Env", to: "/env" },
];

function NavBar() {
  return (
    <nav className="border-b border-zinc-800 bg-zinc-900/40 backdrop-blur">
      <div className="mx-auto flex max-w-6xl items-center gap-6 px-6 py-3">
        <span className="text-sm font-semibold tracking-wide">
          <span className="text-teal-400">Phalanx</span>{" "}
          <span className="text-zinc-500">dashboard</span>
        </span>
        <ul className="flex gap-1">
          {NAV.map((entry) => (
            <li key={entry.to}>
              <NavLink
                to={entry.to}
                className={({ isActive }) =>
                  cn(
                    "rounded-md px-3 py-1.5 text-sm font-medium transition-colors",
                    isActive
                      ? "bg-zinc-800 text-zinc-100"
                      : "text-zinc-400 hover:bg-zinc-800/60 hover:text-zinc-200",
                  )
                }
              >
                {entry.label}
              </NavLink>
            </li>
          ))}
        </ul>
      </div>
    </nav>
  );
}

export default function App() {
  return (
    <ToastProvider>
      <div className="min-h-screen">
        <NavBar />
        <main className="mx-auto max-w-6xl px-6 py-6">
          <Routes>
            <Route path="/" element={<Navigate to="/status" replace />} />
            <Route path="/status" element={<StatusPage />} />
            <Route path="/sessions" element={<SessionsPage />} />
            <Route path="/logs" element={<LogsPage />} />
            <Route path="/analytics" element={<AnalyticsPage />} />
            <Route path="/config" element={<ConfigPage />} />
            <Route path="/env" element={<EnvPage />} />
            <Route
              path="*"
              element={
                <div className="text-zinc-400">Page not found.</div>
              }
            />
          </Routes>
        </main>
      </div>
    </ToastProvider>
  );
}
