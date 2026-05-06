import { useCallback, useEffect, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Dialog } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { useToast } from "@/components/ui/toast";
import {
  ApiError,
  api,
  type SessionMessage,
  type SessionRow,
} from "@/lib/api";
import { cn } from "@/lib/utils";

const PAGE_SIZE = 20;

function formatTimestamp(secs: number): string {
  if (!secs) return "—";
  return new Date(secs * 1000).toLocaleString();
}

function formatTokens(input: number, output: number): string {
  const total = input + output;
  if (!total) return "—";
  if (total < 1000) return String(total);
  if (total < 1_000_000) return `${(total / 1000).toFixed(1)}k`;
  return `${(total / 1_000_000).toFixed(2)}M`;
}

// ── Title cell with double-click inline edit ──────────────────────────


function TitleCell({
  session,
  onChange,
}: {
  session: SessionRow;
  onChange: (id: string, title: string | null) => Promise<void>;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(session.title ?? "");
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    setBusy(true);
    try {
      const next = draft.trim() || null;
      if (next !== session.title) {
        await onChange(session.id, next);
      }
      setEditing(false);
    } finally {
      setBusy(false);
    }
  };

  if (!editing) {
    return (
      <button
        type="button"
        className={cn(
          "block w-full truncate text-left text-sm",
          session.title
            ? "font-medium text-zinc-200"
            : "italic text-zinc-500",
        )}
        title="Double-click to edit title"
        onDoubleClick={() => {
          setDraft(session.title ?? "");
          setEditing(true);
        }}
      >
        {session.title || "Untitled"}
      </button>
    );
  }

  return (
    <Input
      autoFocus
      value={draft}
      disabled={busy}
      onChange={(e) => setDraft(e.target.value)}
      onBlur={submit}
      onKeyDown={(e) => {
        if (e.key === "Enter") submit();
        if (e.key === "Escape") {
          setDraft(session.title ?? "");
          setEditing(false);
        }
      }}
      className="h-7 px-2 text-xs"
    />
  );
}

// ── Messages dialog ───────────────────────────────────────────────────


function MessagesDialog({
  sessionId,
  onClose,
}: {
  sessionId: string | null;
  onClose: () => void;
}) {
  const [messages, setMessages] = useState<SessionMessage[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!sessionId) return;
    setMessages(null);
    setError(null);
    api
      .getSessionMessages(sessionId)
      .then((res) => setMessages(res.messages))
      .catch((err) => setError(String(err)));
  }, [sessionId]);

  return (
    <Dialog
      open={Boolean(sessionId)}
      onClose={onClose}
      title={
        sessionId ? (
          <span className="font-mono">{sessionId.slice(0, 16)}…</span>
        ) : (
          "Messages"
        )
      }
      className="max-w-4xl"
    >
      {error && <div className="text-red-400">{error}</div>}
      {!error && !messages && (
        <div className="text-zinc-500">Loading messages…</div>
      )}
      {messages && messages.length === 0 && (
        <div className="text-zinc-500">No messages.</div>
      )}
      {messages && messages.length > 0 && (
        <ol className="space-y-3">
          {messages.map((m) => (
            <li
              key={m.id}
              className="rounded border border-zinc-800 bg-zinc-950/60 p-3"
            >
              <div className="mb-1 flex items-center gap-2 text-xs text-zinc-500">
                <span className="font-medium uppercase tracking-wide text-zinc-300">
                  {m.role}
                </span>
                {m.tool_name && <Badge tone="muted">{m.tool_name}</Badge>}
                {m.finish_reason && (
                  <Badge tone="muted">{m.finish_reason}</Badge>
                )}
                <span className="ml-auto font-mono">
                  {formatTimestamp(m.timestamp)}
                </span>
              </div>
              <pre className="whitespace-pre-wrap text-xs text-zinc-300">
                {typeof m.content === "string"
                  ? m.content
                  : JSON.stringify(m.content, null, 2)}
              </pre>
            </li>
          ))}
        </ol>
      )}
    </Dialog>
  );
}

// ── Delete confirm dialog ─────────────────────────────────────────────


function DeleteConfirm({
  session,
  onCancel,
  onConfirm,
}: {
  session: SessionRow | null;
  onCancel: () => void;
  onConfirm: () => Promise<void>;
}) {
  const [busy, setBusy] = useState(false);
  return (
    <Dialog
      open={Boolean(session)}
      onClose={busy ? () => undefined : onCancel}
      title="Delete session?"
    >
      {session && (
        <div className="space-y-4">
          <div className="text-sm text-zinc-300">
            This permanently deletes the session and all its messages.
            <div className="mt-2 rounded border border-zinc-800 bg-zinc-950/60 p-2 text-xs">
              <div className="font-mono">{session.id}</div>
              {session.title && (
                <div className="text-zinc-400">{session.title}</div>
              )}
            </div>
          </div>
          <div className="flex justify-end gap-2">
            <Button variant="ghost" onClick={onCancel} disabled={busy}>
              Cancel
            </Button>
            <Button
              variant="destructive"
              disabled={busy}
              onClick={async () => {
                setBusy(true);
                try {
                  await onConfirm();
                } finally {
                  setBusy(false);
                }
              }}
            >
              {busy ? "Deleting…" : "Delete"}
            </Button>
          </div>
        </div>
      )}
    </Dialog>
  );
}

// ── Page ──────────────────────────────────────────────────────────────


export default function SessionsPage() {
  const toast = useToast();
  const [page, setPage] = useState(0);
  const [data, setData] = useState<{
    sessions: SessionRow[];
    total: number;
  } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [viewMessagesId, setViewMessagesId] = useState<string | null>(null);
  const [deleting, setDeleting] = useState<SessionRow | null>(null);

  const reload = useCallback(async () => {
    setError(null);
    try {
      const res = await api.getSessions(PAGE_SIZE, page * PAGE_SIZE);
      setData({ sessions: res.sessions, total: res.total });
    } catch (err) {
      setError(String(err));
    }
  }, [page]);

  useEffect(() => {
    reload();
  }, [reload]);

  const onSetTitle = useCallback(
    async (id: string, title: string | null) => {
      try {
        await api.setSessionTitle(id, title);
        toast.show("Title saved", "success");
        await reload();
      } catch (err) {
        if (err instanceof ApiError && err.status === 409) {
          toast.show("Title already in use", "error");
        } else {
          toast.show(`Failed: ${err}`, "error");
        }
      }
    },
    [reload, toast],
  );

  const onCopyResume = (sessionId: string) => {
    const cmd = `hermes --resume ${sessionId} chat`;
    if (navigator.clipboard?.writeText) {
      navigator.clipboard.writeText(cmd).then(
        () => toast.show("Resume command copied", "success"),
        () => toast.show("Copy failed (clipboard blocked)", "error"),
      );
    } else {
      toast.show(`Copy manually: ${cmd}`, "info");
    }
  };

  const onDelete = async () => {
    if (!deleting) return;
    try {
      await api.deleteSession(deleting.id);
      toast.show("Session deleted", "success");
      setDeleting(null);
      await reload();
    } catch (err) {
      toast.show(`Failed: ${err}`, "error");
    }
  };

  if (error) {
    return (
      <div className="text-red-400">
        Failed to load sessions:{" "}
        <span className="font-mono">{error}</span>
      </div>
    );
  }
  if (!data) {
    return <div className="text-zinc-500">Loading…</div>;
  }

  const totalPages = Math.max(1, Math.ceil(data.total / PAGE_SIZE));

  return (
    <div className="space-y-4">
      <div className="flex items-baseline justify-between">
        <h2 className="text-lg font-semibold tracking-tight">Sessions</h2>
        <span className="text-xs text-zinc-500">
          {data.total} total · page {page + 1} of {totalPages}
        </span>
      </div>

      <Card>
        <CardContent className="p-0">
          <table className="w-full text-sm">
            <thead className="border-b border-zinc-800 text-left text-xs uppercase tracking-wide text-zinc-500">
              <tr>
                <th className="px-4 py-2 font-medium">Title</th>
                <th className="px-4 py-2 font-medium">Source</th>
                <th className="px-4 py-2 font-medium">Model</th>
                <th className="px-4 py-2 font-medium">Started</th>
                <th className="px-4 py-2 font-medium text-right">Msgs</th>
                <th className="px-4 py-2 font-medium text-right">Tokens</th>
                <th className="px-4 py-2 font-medium text-right">Actions</th>
              </tr>
            </thead>
            <tbody>
              {data.sessions.length === 0 && (
                <tr>
                  <td
                    colSpan={7}
                    className="px-4 py-6 text-center text-zinc-500"
                  >
                    No sessions yet — run{" "}
                    <code className="font-mono">hermes oneshot "..."</code>{" "}
                    to create one.
                  </td>
                </tr>
              )}
              {data.sessions.map((s) => (
                <tr
                  key={s.id}
                  className="border-b border-zinc-800/60 hover:bg-zinc-800/20"
                >
                  <td className="max-w-xs px-4 py-2">
                    <TitleCell session={s} onChange={onSetTitle} />
                    {s.preview && (
                      <div className="mt-0.5 truncate text-xs text-zinc-500">
                        {s.preview}
                      </div>
                    )}
                  </td>
                  <td className="px-4 py-2 text-xs">
                    <Badge tone="muted">{s.source}</Badge>
                    {s.is_active && (
                      <Badge tone="success" className="ml-1">
                        active
                      </Badge>
                    )}
                  </td>
                  <td className="px-4 py-2 font-mono text-xs text-zinc-400">
                    {s.model || "—"}
                  </td>
                  <td className="px-4 py-2 text-xs text-zinc-400">
                    {formatTimestamp(s.started_at)}
                  </td>
                  <td className="px-4 py-2 text-right tabular-nums text-xs">
                    {s.message_count}
                  </td>
                  <td className="px-4 py-2 text-right tabular-nums text-xs">
                    {formatTokens(s.input_tokens, s.output_tokens)}
                  </td>
                  <td className="px-4 py-2 text-right">
                    <div className="flex justify-end gap-1">
                      <Button
                        size="sm"
                        variant="ghost"
                        onClick={() => setViewMessagesId(s.id)}
                      >
                        View
                      </Button>
                      <Button
                        size="sm"
                        variant="ghost"
                        onClick={() => onCopyResume(s.id)}
                        title="Copy `hermes --resume <id> chat` to clipboard"
                      >
                        Resume
                      </Button>
                      <Button
                        size="sm"
                        variant="ghost"
                        className="text-red-300 hover:bg-red-500/10"
                        onClick={() => setDeleting(s)}
                      >
                        Delete
                      </Button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </CardContent>
      </Card>

      <div className="flex items-center justify-end gap-2">
        <Button
          size="sm"
          variant="outline"
          disabled={page === 0}
          onClick={() => setPage((p) => Math.max(0, p - 1))}
        >
          Previous
        </Button>
        <Button
          size="sm"
          variant="outline"
          disabled={page + 1 >= totalPages}
          onClick={() => setPage((p) => p + 1)}
        >
          Next
        </Button>
      </div>

      <MessagesDialog
        sessionId={viewMessagesId}
        onClose={() => setViewMessagesId(null)}
      />
      <DeleteConfirm
        session={deleting}
        onCancel={() => setDeleting(null)}
        onConfirm={onDelete}
      />
    </div>
  );
}
