import { useCallback, useEffect, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Select } from "@/components/ui/select";
import { api, type AnalyticsResponse } from "@/lib/api";

const PERIODS = [7, 30, 90, 180, 365] as const;

function formatTokens(n: number): string {
  if (!n) return "0";
  if (n < 1000) return String(n);
  if (n < 1_000_000) return `${(n / 1000).toFixed(1)}k`;
  return `${(n / 1_000_000).toFixed(2)}M`;
}

function formatCost(usd: number): string {
  if (!usd) return "$0";
  if (usd < 0.01) return "<$0.01";
  return `$${usd.toFixed(2)}`;
}

function StatCard({
  label,
  value,
  hint,
}: {
  label: string;
  value: React.ReactNode;
  hint?: React.ReactNode;
}) {
  return (
    <Card>
      <CardContent className="space-y-1 p-4">
        <div className="text-xs uppercase tracking-wide text-zinc-500">
          {label}
        </div>
        <div className="text-2xl font-semibold tabular-nums">{value}</div>
        {hint && <div className="text-xs text-zinc-500">{hint}</div>}
      </CardContent>
    </Card>
  );
}

export default function AnalyticsPage() {
  const [days, setDays] = useState<number>(30);
  const [data, setData] = useState<AnalyticsResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      const d = await api.getAnalytics(days);
      setData(d);
    } catch (err) {
      setError(String(err));
    }
  }, [days]);

  useEffect(() => {
    load();
  }, [load]);

  if (error) {
    return (
      <div className="text-red-400">
        Failed to load analytics:{" "}
        <span className="font-mono">{error}</span>
      </div>
    );
  }
  if (!data) {
    return <div className="text-zinc-500">Loading…</div>;
  }

  const t = data.totals;

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h2 className="text-lg font-semibold tracking-tight">Analytics</h2>
        <label className="flex items-center gap-2 text-xs text-zinc-400">
          period
          <Select
            value={String(days)}
            onChange={(e) => setDays(Number(e.target.value))}
            className="w-24"
          >
            {PERIODS.map((p) => (
              <option key={p} value={p}>
                {p}d
              </option>
            ))}
          </Select>
        </label>
      </div>

      <div className="grid gap-3 md:grid-cols-4">
        <StatCard
          label="Sessions"
          value={t.total_sessions}
          hint={`${t.total_api_calls} API calls`}
        />
        <StatCard
          label="Input tokens"
          value={formatTokens(t.total_input)}
          hint={`+${formatTokens(t.total_cache_read)} cache-read`}
        />
        <StatCard
          label="Output tokens"
          value={formatTokens(t.total_output)}
          hint={`+${formatTokens(t.total_reasoning)} reasoning`}
        />
        <StatCard
          label="Cost"
          value={formatCost(t.total_estimated_cost)}
          hint={
            t.total_actual_cost > 0
              ? `actual ${formatCost(t.total_actual_cost)}`
              : "estimated"
          }
        />
      </div>

      <Card>
        <CardHeader>
          <CardTitle>By model</CardTitle>
        </CardHeader>
        <CardContent className="p-0">
          <table className="w-full text-sm">
            <thead className="border-b border-zinc-800 text-left text-xs uppercase tracking-wide text-zinc-500">
              <tr>
                <th className="px-4 py-2 font-medium">Model</th>
                <th className="px-4 py-2 font-medium text-right">Sessions</th>
                <th className="px-4 py-2 font-medium text-right">Input</th>
                <th className="px-4 py-2 font-medium text-right">Output</th>
                <th className="px-4 py-2 font-medium text-right">Cost</th>
                <th className="px-4 py-2 font-medium text-right">API calls</th>
              </tr>
            </thead>
            <tbody>
              {data.by_model.length === 0 && (
                <tr>
                  <td
                    colSpan={6}
                    className="px-4 py-6 text-center text-zinc-500"
                  >
                    No model usage in this period.
                  </td>
                </tr>
              )}
              {data.by_model.map((row) => (
                <tr
                  key={row.model}
                  className="border-b border-zinc-800/60 hover:bg-zinc-800/20"
                >
                  <td className="px-4 py-2">
                    <Badge tone="muted">{row.model}</Badge>
                  </td>
                  <td className="px-4 py-2 text-right tabular-nums">
                    {row.sessions}
                  </td>
                  <td className="px-4 py-2 text-right tabular-nums">
                    {formatTokens(row.input_tokens)}
                  </td>
                  <td className="px-4 py-2 text-right tabular-nums">
                    {formatTokens(row.output_tokens)}
                  </td>
                  <td className="px-4 py-2 text-right tabular-nums">
                    {formatCost(row.estimated_cost)}
                  </td>
                  <td className="px-4 py-2 text-right tabular-nums">
                    {row.api_calls}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Daily</CardTitle>
        </CardHeader>
        <CardContent className="p-0">
          <table className="w-full text-sm">
            <thead className="border-b border-zinc-800 text-left text-xs uppercase tracking-wide text-zinc-500">
              <tr>
                <th className="px-4 py-2 font-medium">Day</th>
                <th className="px-4 py-2 font-medium text-right">Sessions</th>
                <th className="px-4 py-2 font-medium text-right">Input</th>
                <th className="px-4 py-2 font-medium text-right">Output</th>
                <th className="px-4 py-2 font-medium text-right">Cost</th>
                <th className="px-4 py-2 font-medium text-right">API calls</th>
              </tr>
            </thead>
            <tbody>
              {data.daily.length === 0 && (
                <tr>
                  <td
                    colSpan={6}
                    className="px-4 py-6 text-center text-zinc-500"
                  >
                    No activity in this period.
                  </td>
                </tr>
              )}
              {data.daily.map((row) => (
                <tr
                  key={row.day}
                  className="border-b border-zinc-800/60 hover:bg-zinc-800/20"
                >
                  <td className="px-4 py-2 font-mono text-xs text-zinc-400">
                    {row.day}
                  </td>
                  <td className="px-4 py-2 text-right tabular-nums">
                    {row.sessions}
                  </td>
                  <td className="px-4 py-2 text-right tabular-nums">
                    {formatTokens(row.input_tokens)}
                  </td>
                  <td className="px-4 py-2 text-right tabular-nums">
                    {formatTokens(row.output_tokens)}
                  </td>
                  <td className="px-4 py-2 text-right tabular-nums">
                    {formatCost(row.estimated_cost)}
                  </td>
                  <td className="px-4 py-2 text-right tabular-nums">
                    {row.api_calls}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </CardContent>
      </Card>
    </div>
  );
}
