import { useCallback, useEffect, useMemo, useState } from "react";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Select } from "@/components/ui/select";
import { useToast } from "@/components/ui/toast";
import { api, type SchemaField, type SchemaResponse } from "@/lib/api";
import { cn } from "@/lib/utils";

type FormValue = string | number | boolean | null;
type FormDraft = Record<string, FormValue>;

// ── dotted-path helpers ──────────────────────────────────────────────


function getDotted(obj: Record<string, unknown>, dotted: string): unknown {
  return dotted.split(".").reduce<unknown>(
    (acc, k) =>
      acc && typeof acc === "object" && k in acc
        ? (acc as Record<string, unknown>)[k]
        : undefined,
    obj,
  );
}

function setDotted(
  obj: Record<string, unknown>,
  dotted: string,
  value: unknown,
): Record<string, unknown> {
  const keys = dotted.split(".");
  const root: Record<string, unknown> = { ...obj };
  let cursor: Record<string, unknown> = root;
  for (let i = 0; i < keys.length - 1; i++) {
    const k = keys[i];
    const next = cursor[k];
    cursor[k] =
      next && typeof next === "object" && !Array.isArray(next)
        ? { ...(next as Record<string, unknown>) }
        : {};
    cursor = cursor[k] as Record<string, unknown>;
  }
  cursor[keys[keys.length - 1]] = value;
  return root;
}

// ── Form mode ────────────────────────────────────────────────────────


function FieldEditor({
  path,
  field,
  value,
  onChange,
}: {
  path: string;
  field: SchemaField;
  value: FormValue;
  onChange: (next: FormValue) => void;
}) {
  if (field.type === "select") {
    return (
      <Select
        value={String(value ?? "")}
        onChange={(e) => onChange(e.target.value)}
      >
        {(field.options ?? []).map((opt) => (
          <option key={opt} value={opt}>
            {opt || "(unset)"}
          </option>
        ))}
      </Select>
    );
  }
  if (field.type === "boolean") {
    return (
      <label className="inline-flex items-center gap-2">
        <input
          type="checkbox"
          className="h-4 w-4"
          checked={Boolean(value)}
          onChange={(e) => onChange(e.target.checked)}
        />
        <span className="text-xs text-zinc-400">{path}</span>
      </label>
    );
  }
  if (field.type === "number") {
    return (
      <Input
        type="number"
        value={value === null || value === undefined ? "" : String(value)}
        onChange={(e) => {
          const v = e.target.value;
          onChange(v === "" ? null : Number(v));
        }}
      />
    );
  }
  return (
    <Input
      value={value === null || value === undefined ? "" : String(value)}
      onChange={(e) => onChange(e.target.value)}
    />
  );
}

function FormMode({
  schema,
  draft,
  setDraft,
  onSave,
  saving,
  dirty,
}: {
  schema: SchemaResponse;
  draft: FormDraft;
  setDraft: (next: FormDraft) => void;
  onSave: () => Promise<void>;
  saving: boolean;
  dirty: boolean;
}) {
  // Group fields by category.
  const byCat = useMemo(() => {
    const m: Record<string, [string, SchemaField][]> = {};
    for (const [path, field] of Object.entries(schema.fields)) {
      (m[field.category] ??= []).push([path, field]);
    }
    return m;
  }, [schema]);

  const cats =
    schema.category_order.length > 0
      ? schema.category_order
      : Object.keys(byCat).sort();

  return (
    <div className="space-y-4">
      {cats.map((cat) => (
        <Card key={cat}>
          <CardHeader>
            <CardTitle className="capitalize">{cat}</CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            {(byCat[cat] ?? []).map(([path, field]) => (
              <div
                key={path}
                className="grid grid-cols-1 gap-1 md:grid-cols-[14rem_1fr]"
              >
                <div>
                  <label className="font-mono text-xs text-zinc-300">
                    {path}
                  </label>
                  {field.description && (
                    <p className="text-xs text-zinc-500">{field.description}</p>
                  )}
                </div>
                <div className="flex items-start gap-2">
                  <div className="flex-1">
                    <FieldEditor
                      path={path}
                      field={field}
                      value={(draft[path] ?? field.default) as FormValue}
                      onChange={(next) =>
                        setDraft({ ...draft, [path]: next })
                      }
                    />
                  </div>
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() =>
                      setDraft({
                        ...draft,
                        [path]: field.default as FormValue,
                      })
                    }
                    title="Reset to default"
                  >
                    Reset
                  </Button>
                </div>
              </div>
            ))}
          </CardContent>
        </Card>
      ))}

      <div className="flex items-center justify-end gap-3">
        <span
          className={cn(
            "text-xs",
            dirty ? "text-amber-300" : "text-zinc-500",
          )}
        >
          {dirty ? "unsaved changes" : "in sync"}
        </span>
        <Button onClick={onSave} disabled={saving || !dirty}>
          {saving ? "Saving…" : "Save"}
        </Button>
      </div>
    </div>
  );
}

// ── Raw YAML mode ────────────────────────────────────────────────────


function RawMode({
  text,
  setText,
  pristine,
  onReload,
  onSave,
  saving,
}: {
  text: string;
  setText: (v: string) => void;
  pristine: string;
  onReload: () => Promise<void>;
  onSave: () => Promise<void>;
  saving: boolean;
}) {
  const dirty = text !== pristine;
  return (
    <Card>
      <CardHeader>
        <CardTitle>~/.phalanx/config.yaml</CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        <textarea
          value={text}
          onChange={(e) => setText(e.target.value)}
          spellCheck={false}
          className={cn(
            "h-[60vh] w-full resize-y rounded-md border border-zinc-700",
            "bg-zinc-950 p-3 font-mono text-xs text-zinc-100",
            "focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-teal-400",
          )}
        />
        <div className="flex items-center justify-end gap-3">
          <span
            className={cn(
              "text-xs",
              dirty ? "text-amber-300" : "text-zinc-500",
            )}
          >
            {dirty ? "unsaved changes" : "in sync"}
          </span>
          <Button variant="outline" onClick={onReload} disabled={saving}>
            Reload
          </Button>
          <Button onClick={onSave} disabled={saving || !dirty}>
            {saving ? "Saving…" : "Save"}
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}

// ── Page ─────────────────────────────────────────────────────────────


export default function ConfigPage() {
  const toast = useToast();
  const [tab, setTab] = useState<"form" | "raw">("form");

  const [schema, setSchema] = useState<SchemaResponse | null>(null);
  const [config, setConfig] = useState<Record<string, unknown> | null>(null);
  const [draft, setDraft] = useState<FormDraft>({});

  const [rawText, setRawText] = useState("");
  const [rawPristine, setRawPristine] = useState("");

  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const reload = useCallback(async () => {
    setError(null);
    try {
      const [s, c, r] = await Promise.all([
        api.getSchema(),
        api.getConfig(),
        api.getConfigRaw(),
      ]);
      setSchema(s);
      setConfig(c);
      setRawText(r.yaml);
      setRawPristine(r.yaml);

      const initialDraft: FormDraft = {};
      for (const path of Object.keys(s.fields)) {
        const v = getDotted(c, path);
        if (v === undefined || v === null) {
          initialDraft[path] = s.fields[path].default as FormValue;
        } else {
          initialDraft[path] = v as FormValue;
        }
      }
      setDraft(initialDraft);
    } catch (err) {
      setError(String(err));
    }
  }, []);

  useEffect(() => {
    reload();
  }, [reload]);

  const formDirty = useMemo(() => {
    if (!schema || !config) return false;
    for (const path of Object.keys(schema.fields)) {
      const cur = getDotted(config, path);
      const next = draft[path];
      const norm = (v: unknown) =>
        v === undefined || v === "" ? null : v;
      if (norm(cur) !== norm(next)) return true;
    }
    return false;
  }, [schema, config, draft]);

  const onSaveForm = async () => {
    if (!schema || !config) return;
    setSaving(true);
    try {
      let next: Record<string, unknown> = { ...config };
      for (const path of Object.keys(schema.fields)) {
        const v = draft[path];
        // Empty string + null collapse to "remove the override".
        if (v === "" || v === null) {
          // For now leave it in place at default — phalanx doesn't deep-prune.
          next = setDotted(next, path, schema.fields[path].default);
        } else {
          next = setDotted(next, path, v);
        }
      }
      await api.saveConfig(next);
      toast.show("Config saved", "success");
      await reload();
    } catch (err) {
      toast.show(`Save failed: ${err}`, "error");
    } finally {
      setSaving(false);
    }
  };

  const onSaveRaw = async () => {
    setSaving(true);
    try {
      await api.saveConfigRaw(rawText);
      toast.show("Raw config saved", "success");
      await reload();
    } catch (err) {
      toast.show(`Save failed: ${err}`, "error");
    } finally {
      setSaving(false);
    }
  };

  if (error) {
    return (
      <div className="text-red-400">
        Failed to load: <span className="font-mono">{error}</span>
      </div>
    );
  }
  if (!schema || !config) {
    return <div className="text-zinc-500">Loading…</div>;
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h2 className="text-lg font-semibold tracking-tight">Config</h2>
        <div className="flex rounded-md border border-zinc-800 bg-zinc-900 p-0.5">
          {(["form", "raw"] as const).map((t) => (
            <button
              key={t}
              onClick={() => setTab(t)}
              className={cn(
                "rounded px-3 py-1 text-xs font-medium transition-colors",
                tab === t
                  ? "bg-zinc-700 text-zinc-100"
                  : "text-zinc-400 hover:text-zinc-200",
              )}
            >
              {t === "form" ? "Form" : "Raw YAML"}
            </button>
          ))}
        </div>
      </div>

      {tab === "form" ? (
        <FormMode
          schema={schema}
          draft={draft}
          setDraft={setDraft}
          onSave={onSaveForm}
          saving={saving}
          dirty={formDirty}
        />
      ) : (
        <RawMode
          text={rawText}
          setText={setRawText}
          pristine={rawPristine}
          onReload={reload}
          onSave={onSaveRaw}
          saving={saving}
        />
      )}
    </div>
  );
}
