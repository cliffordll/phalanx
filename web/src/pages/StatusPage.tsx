import { useEffect, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { api, type StatusResponse } from "@/lib/api";

function Field({
  label,
  value,
  monospace = false,
}: {
  label: string;
  value: React.ReactNode;
  monospace?: boolean;
}) {
  return (
    <div className="flex items-baseline gap-3">
      <span className="w-32 shrink-0 text-xs uppercase tracking-wide text-zinc-500">
        {label}
      </span>
      <span
        className={
          monospace
            ? "font-mono text-xs text-zinc-300 break-all"
            : "text-sm text-zinc-200"
        }
      >
        {value}
      </span>
    </div>
  );
}

export default function StatusPage() {
  const [status, setStatus] = useState<StatusResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    api
      .getStatus()
      .then((s) => {
        if (alive) setStatus(s);
      })
      .catch((err) => {
        if (alive) setError(String(err));
      });
    return () => {
      alive = false;
    };
  }, []);

  if (error) {
    return (
      <div className="text-red-400">
        Failed to load status: <span className="font-mono">{error}</span>
      </div>
    );
  }
  if (!status) {
    return <div className="text-zinc-500">Loading…</div>;
  }

  return (
    <div className="grid gap-4 md:grid-cols-2">
      <Card className="md:col-span-2">
        <CardHeader>
          <div className="flex items-center justify-between">
            <CardTitle>Phalanx</CardTitle>
            <Badge tone="muted">v{status.version}</Badge>
          </div>
        </CardHeader>
        <CardContent>
          <div className="text-xs text-zinc-500">{status.release_date}</div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Paths</CardTitle>
        </CardHeader>
        <CardContent className="space-y-2">
          <Field label="PHALANX_HOME" value={status.phalanx_home} monospace />
          <Field label="config.yaml" value={status.config_path} monospace />
          <Field label=".env" value={status.env_path} monospace />
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Model</CardTitle>
        </CardHeader>
        <CardContent className="space-y-2">
          <Field
            label="model"
            value={status.model || <span className="text-zinc-500">unset</span>}
            monospace
          />
          <Field
            label="base_url"
            value={
              status.base_url || <span className="text-zinc-500">default</span>
            }
            monospace
          />
          <Field
            label="provider"
            value={
              status.provider ? (
                <Badge>{status.provider}</Badge>
              ) : (
                <span className="text-zinc-500">auto-infer</span>
              )
            }
          />
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Sessions</CardTitle>
        </CardHeader>
        <CardContent className="flex items-center gap-6">
          <div>
            <div className="text-2xl font-semibold tabular-nums">
              {status.session_count}
            </div>
            <div className="text-xs text-zinc-500">total</div>
          </div>
          <div>
            <div className="text-2xl font-semibold tabular-nums text-teal-300">
              {status.active_sessions}
            </div>
            <div className="text-xs text-zinc-500">active (last 5min)</div>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Tools registered</CardTitle>
        </CardHeader>
        <CardContent>
          {status.tools.length === 0 ? (
            <span className="text-sm text-zinc-500">none</span>
          ) : (
            <div className="flex flex-wrap gap-1.5">
              {status.tools.map((t) => (
                <Badge key={t} tone="muted">
                  {t}
                </Badge>
              ))}
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
