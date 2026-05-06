import { useCallback, useEffect, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { useToast } from "@/components/ui/toast";
import { ApiError, api, type EnvVarInfo } from "@/lib/api";
import { cn } from "@/lib/utils";

interface RowState {
  draft: string;
  showing: boolean;          // value revealed inline
  revealed: string | null;   // last reveal result
  busy: boolean;
}

const EMPTY_ROW: RowState = {
  draft: "",
  showing: false,
  revealed: null,
  busy: false,
};

function EnvRow({
  name,
  info,
  state,
  setState,
  onSet,
  onClear,
  onReveal,
}: {
  name: string;
  info: EnvVarInfo;
  state: RowState;
  setState: (next: RowState) => void;
  onSet: (key: string, value: string) => Promise<void>;
  onClear: (key: string) => Promise<void>;
  onReveal: (key: string) => Promise<void>;
}) {
  const editing = state.draft !== "";
  return (
    <tr className="border-b border-zinc-800/60 hover:bg-zinc-800/20">
      <td className="px-4 py-3 align-top">
        <div className="font-mono text-xs">{name}</div>
        {info.url && (
          <a
            href={info.url}
            target="_blank"
            rel="noreferrer"
            className="text-xs text-teal-400 hover:underline"
          >
            issue page ↗
          </a>
        )}
      </td>
      <td className="px-4 py-3 align-top text-xs text-zinc-400 max-w-md">
        {info.description}
      </td>
      <td className="px-4 py-3 align-top">
        {info.is_set ? (
          <Badge tone="success">set</Badge>
        ) : (
          <Badge tone="muted">unset</Badge>
        )}
      </td>
      <td className="px-4 py-3 align-top font-mono text-xs">
        {state.showing && state.revealed !== null ? (
          <span className="text-amber-200 break-all">{state.revealed}</span>
        ) : info.is_set ? (
          <span className="text-zinc-400">{info.redacted_value}</span>
        ) : (
          <span className="text-zinc-600">—</span>
        )}
      </td>
      <td className="px-4 py-3 align-top">
        <div className="flex flex-wrap gap-2">
          <Input
            value={state.draft}
            disabled={state.busy}
            placeholder={info.is_password ? "new value" : "new value"}
            type={info.is_password && !state.showing ? "password" : "text"}
            className="h-8 w-48"
            onChange={(e) => setState({ ...state, draft: e.target.value })}
            onKeyDown={(e) => {
              if (e.key === "Enter" && state.draft) {
                onSet(name, state.draft);
              }
            }}
          />
          <Button
            size="sm"
            disabled={!editing || state.busy}
            onClick={() => onSet(name, state.draft)}
          >
            Save
          </Button>
          {info.is_set && (
            <>
              <Button
                size="sm"
                variant="ghost"
                disabled={state.busy}
                onClick={() => onReveal(name)}
              >
                {state.showing ? "Hide" : "Reveal"}
              </Button>
              <Button
                size="sm"
                variant="ghost"
                className="text-red-300 hover:bg-red-500/10"
                disabled={state.busy}
                onClick={() => onClear(name)}
              >
                Clear
              </Button>
            </>
          )}
        </div>
      </td>
    </tr>
  );
}

function CategorySection({
  title,
  vars,
  rows,
  setRow,
  onSet,
  onClear,
  onReveal,
  defaultOpen = true,
}: {
  title: string;
  vars: [string, EnvVarInfo][];
  rows: Record<string, RowState>;
  setRow: (key: string, next: RowState) => void;
  onSet: (key: string, value: string) => Promise<void>;
  onClear: (key: string) => Promise<void>;
  onReveal: (key: string) => Promise<void>;
  defaultOpen?: boolean;
}) {
  const [open, setOpen] = useState(defaultOpen);
  if (vars.length === 0) return null;
  return (
    <Card>
      <CardHeader>
        <button
          className="flex w-full items-center justify-between text-left"
          onClick={() => setOpen((v) => !v)}
        >
          <CardTitle className="capitalize">{title}</CardTitle>
          <span className="text-xs text-zinc-500">
            {vars.length} entries · {open ? "▾" : "▸"}
          </span>
        </button>
      </CardHeader>
      {open && (
        <CardContent className="p-0">
          <table className="w-full text-sm">
            <thead className="border-b border-zinc-800 text-left text-xs uppercase tracking-wide text-zinc-500">
              <tr>
                <th className="px-4 py-2 font-medium">Variable</th>
                <th className="px-4 py-2 font-medium">Description</th>
                <th className="px-4 py-2 font-medium">State</th>
                <th className="px-4 py-2 font-medium">Value</th>
                <th className="px-4 py-2 font-medium">Actions</th>
              </tr>
            </thead>
            <tbody>
              {vars.map(([name, info]) => (
                <EnvRow
                  key={name}
                  name={name}
                  info={info}
                  state={rows[name] ?? EMPTY_ROW}
                  setState={(next) => setRow(name, next)}
                  onSet={onSet}
                  onClear={onClear}
                  onReveal={onReveal}
                />
              ))}
            </tbody>
          </table>
        </CardContent>
      )}
    </Card>
  );
}

export default function EnvPage() {
  const toast = useToast();
  const [vars, setVars] = useState<Record<string, EnvVarInfo> | null>(null);
  const [rows, setRows] = useState<Record<string, RowState>>({});
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      const v = await api.getEnvVars();
      setVars(v);
    } catch (err) {
      setError(String(err));
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const setRow = (key: string, next: RowState) =>
    setRows((r) => ({ ...r, [key]: next }));

  const onSet = async (key: string, value: string) => {
    setRow(key, { ...(rows[key] ?? EMPTY_ROW), busy: true });
    try {
      await api.setEnvVar(key, value);
      toast.show(`${key} saved`, "success");
      setRow(key, { ...EMPTY_ROW });
      await load();
    } catch (err) {
      if (err instanceof ApiError && err.status === 400) {
        toast.show(`Invalid: ${err.message}`, "error");
      } else {
        toast.show(`Save failed: ${err}`, "error");
      }
      setRow(key, { ...(rows[key] ?? EMPTY_ROW), busy: false });
    }
  };

  const onClear = async (key: string) => {
    setRow(key, { ...(rows[key] ?? EMPTY_ROW), busy: true });
    try {
      await api.deleteEnvVar(key);
      toast.show(`${key} cleared`, "success");
      setRow(key, { ...EMPTY_ROW });
      await load();
    } catch (err) {
      toast.show(`Clear failed: ${err}`, "error");
      setRow(key, { ...(rows[key] ?? EMPTY_ROW), busy: false });
    }
  };

  const onReveal = async (key: string) => {
    const cur = rows[key] ?? EMPTY_ROW;
    if (cur.showing) {
      setRow(key, { ...cur, showing: false, revealed: null });
      return;
    }
    setRow(key, { ...cur, busy: true });
    try {
      const res = await api.revealEnvVar(key);
      setRow(key, { ...cur, showing: true, revealed: res.value, busy: false });
    } catch (err) {
      if (err instanceof ApiError && err.status === 429) {
        toast.show("Reveal rate-limited (5 per 30s)", "error");
      } else {
        toast.show(`Reveal failed: ${err}`, "error");
      }
      setRow(key, { ...cur, busy: false });
    }
  };

  if (error) {
    return (
      <div className="text-red-400">
        Failed to load env: <span className="font-mono">{error}</span>
      </div>
    );
  }
  if (!vars) {
    return <div className="text-zinc-500">Loading…</div>;
  }

  // Group by category, advanced section last + collapsed.
  const grouped: Record<string, [string, EnvVarInfo][]> = {};
  const advanced: [string, EnvVarInfo][] = [];
  for (const [name, info] of Object.entries(vars)) {
    if (info.advanced) advanced.push([name, info]);
    else {
      const cat = info.category || "other";
      (grouped[cat] ??= []).push([name, info]);
    }
  }

  const orderedCats = Object.keys(grouped).sort((a, b) => {
    const order = ["providers", "tools", "phalanx", "other"];
    const ia = order.indexOf(a);
    const ib = order.indexOf(b);
    return (ia === -1 ? 99 : ia) - (ib === -1 ? 99 : ib);
  });

  return (
    <div className={cn("space-y-4")}>
      <h2 className="text-lg font-semibold tracking-tight">Environment</h2>
      <p className="text-xs text-zinc-500">
        Values write to{" "}
        <span className="font-mono text-zinc-400">~/.phalanx/.env</span>.
        Reveal is rate-limited (5 per 30s).
      </p>
      {orderedCats.map((cat) => (
        <CategorySection
          key={cat}
          title={cat}
          vars={grouped[cat]}
          rows={rows}
          setRow={setRow}
          onSet={onSet}
          onClear={onClear}
          onReveal={onReveal}
        />
      ))}
      <CategorySection
        title="advanced"
        vars={advanced}
        rows={rows}
        setRow={setRow}
        onSet={onSet}
        onClear={onClear}
        onReveal={onReveal}
        defaultOpen={false}
      />
    </div>
  );
}
