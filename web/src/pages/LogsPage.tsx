import { useCallback, useEffect, useState } from "react";

import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Select } from "@/components/ui/select";
import { api, type LogsResponse } from "@/lib/api";
import { cn } from "@/lib/utils";

const FILES = ["agent", "errors", "gateway"] as const;
const LEVELS = ["ALL", "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] as const;
const LINE_OPTIONS = [50, 100, 200, 500] as const;

function lineTone(line: string): string {
  if (/\bERROR\b|\bCRITICAL\b/.test(line)) return "text-red-300";
  if (/\bWARNING\b/.test(line)) return "text-amber-300";
  if (/\bDEBUG\b/.test(line)) return "text-zinc-500";
  return "text-zinc-300";
}

export default function LogsPage() {
  const [file, setFile] = useState<(typeof FILES)[number]>("agent");
  const [level, setLevel] = useState<(typeof LEVELS)[number]>("ALL");
  const [lines, setLines] = useState<number>(100);
  const [search, setSearch] = useState("");
  const [data, setData] = useState<LogsResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await api.getLogs({
        file,
        lines,
        level: level === "ALL" ? undefined : level,
        search: search.trim() || undefined,
      });
      setData(res);
    } catch (err) {
      setError(String(err));
    } finally {
      setLoading(false);
    }
  }, [file, lines, level, search]);

  useEffect(() => {
    load();
  }, [load]);

  return (
    <div className="space-y-4">
      <div className="flex items-baseline justify-between">
        <h2 className="text-lg font-semibold tracking-tight">Logs</h2>
        <span className="text-xs text-zinc-500">
          {data ? `${data.lines.length} lines` : "—"}
        </span>
      </div>

      <Card>
        <CardContent className="flex flex-wrap items-end gap-3 p-4">
          <label className="flex flex-col gap-1">
            <span className="text-xs uppercase tracking-wide text-zinc-500">
              File
            </span>
            <Select
              value={file}
              onChange={(e) =>
                setFile(e.target.value as (typeof FILES)[number])
              }
              className="w-32"
            >
              {FILES.map((f) => (
                <option key={f} value={f}>
                  {f}
                </option>
              ))}
            </Select>
          </label>

          <label className="flex flex-col gap-1">
            <span className="text-xs uppercase tracking-wide text-zinc-500">
              Level
            </span>
            <Select
              value={level}
              onChange={(e) =>
                setLevel(e.target.value as (typeof LEVELS)[number])
              }
              className="w-32"
            >
              {LEVELS.map((l) => (
                <option key={l} value={l}>
                  {l}
                </option>
              ))}
            </Select>
          </label>

          <label className="flex flex-col gap-1">
            <span className="text-xs uppercase tracking-wide text-zinc-500">
              Lines
            </span>
            <Select
              value={String(lines)}
              onChange={(e) => setLines(Number(e.target.value))}
              className="w-24"
            >
              {LINE_OPTIONS.map((n) => (
                <option key={n} value={n}>
                  {n}
                </option>
              ))}
            </Select>
          </label>

          <label className="flex flex-1 flex-col gap-1 min-w-[12rem]">
            <span className="text-xs uppercase tracking-wide text-zinc-500">
              Search
            </span>
            <Input
              placeholder="case-insensitive substring filter"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") load();
              }}
            />
          </label>

          <Button onClick={load} disabled={loading}>
            {loading ? "Loading…" : "Refresh"}
          </Button>
        </CardContent>
      </Card>

      <Card>
        <CardContent className="p-0">
          {error && (
            <div className="p-4 text-red-400">
              <span className="font-mono">{error}</span>
            </div>
          )}
          {!error && data && data.lines.length === 0 && (
            <div className="p-4 text-zinc-500">
              No matching lines.
              {file === "errors" || file === "gateway"
                ? ` (~/.phalanx/logs/${file}.log may not exist yet)`
                : null}
            </div>
          )}
          {!error && data && data.lines.length > 0 && (
            <div className="max-h-[70vh] overflow-auto">
              <ol className="divide-y divide-zinc-900">
                {data.lines.map((line, i) => (
                  <li
                    key={`${i}-${line.slice(0, 24)}`}
                    className={cn(
                      "whitespace-pre-wrap px-4 py-1 font-mono text-xs",
                      lineTone(line),
                    )}
                  >
                    {line}
                  </li>
                ))}
              </ol>
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
