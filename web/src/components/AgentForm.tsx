// One typed form for every agent (D5), rendered from the agent's own input schema.
//
// GET /v1/agents carries each agent's pydantic InputModel as JSON Schema, so field names,
// required-ness and limits come from the same model the API validates against. This file
// adds only presentation: labels, help text, which strings deserve a textarea, and which
// fields are tucked under "Advanced". An agent nobody wrote hints for still gets a labelled,
// validated form - never a raw JSON box.
//
// Validation runs twice, on purpose. Here, so a person sees "at least 50 characters" under
// the field before anything is sent; and in the API, which is the authority. A 422 that
// still comes back is mapped onto the field it names (loc[0]), not dumped above the form.

import { useState, type FormEvent } from "react";
import type { JsonSchema, SchemaProperty } from "../types";

type Hint = { label?: string; help?: string; textarea?: boolean; rows?: number; advanced?: boolean };

const HINTS: Record<string, Record<string, Hint>> = {
  echo: {
    message: { label: "Message", help: "Sent back unchanged, with the tenant id that ran it.", textarea: true, rows: 3 },
  },
  summarize: {
    text: { label: "Text to summarize", textarea: true, rows: 8 },
    max_sentences: { label: "Max sentences" },
  },
  answer: {
    question: { label: "Question", help: "Answered only from this tenant's documents." },
    top_k: { label: "Passages to retrieve (top-k)" },
    min_similarity: {
      label: "Similarity floor",
      help: "Passages below this cosine similarity are ignored. Higher refuses more often.",
      advanced: true,
    },
  },
  docwriter: {
    ticket_id: { label: "Ticket id" },
    title: { label: "Title" },
    description: { label: "Description", textarea: true, rows: 6 },
    status: { label: "Ticket status", advanced: true },
    labels: { label: "Labels", help: "Comma-separated.", advanced: true },
  },
};

const RUN_LABEL: Record<string, string> = {
  echo: "Run echo",
  summarize: "Summarize",
  answer: "Ask",
  docwriter: "Write document",
};

export type FieldErrors = Record<string, string>;

type Field = { name: string; prop: SchemaProperty; required: boolean; hint: Hint };

function fieldsOf(agent: string, schema: JsonSchema): Field[] {
  const required = new Set(schema.required ?? []);
  return Object.entries(schema.properties ?? {}).map(([name, prop]) => ({
    name,
    prop,
    required: required.has(name),
    hint: HINTS[agent]?.[name] ?? {},
  }));
}

function labelOf(f: Field): string {
  return f.hint.label ?? f.prop.title ?? f.name;
}

function initialValue(prop: SchemaProperty): string {
  if (prop.default === undefined || prop.default === null) return "";
  if (Array.isArray(prop.default)) return prop.default.join(", ");
  return String(prop.default);
}

function limitsText(f: Field): string | null {
  const p = f.prop;
  if (p.type === "string" && (p.minLength !== undefined || p.maxLength !== undefined)) {
    if (p.minLength && p.minLength > 1 && p.maxLength)
      return `${p.minLength.toLocaleString()}–${p.maxLength.toLocaleString()} characters`;
    if (p.maxLength) return `up to ${p.maxLength.toLocaleString()} characters`;
  }
  if ((p.type === "integer" || p.type === "number") && p.minimum !== undefined && p.maximum !== undefined)
    return `${p.minimum}–${p.maximum}`;
  return null;
}

// Parse and check one field. Returns [value to send | undefined to omit, error | null].
export function checkField(f: Field, raw: string): [unknown, string | null] {
  const p = f.prop;
  const label = labelOf(f);
  const empty = raw.trim() === "";

  if (empty) {
    // Optional and blank: omit it and let the model's default apply.
    return [undefined, f.required ? `${label} is required.` : null];
  }

  switch (p.type) {
    case "string": {
      if (p.minLength !== undefined && raw.length < p.minLength)
        return [raw, `At least ${p.minLength.toLocaleString()} characters (now ${raw.length.toLocaleString()}).`];
      if (p.maxLength !== undefined && raw.length > p.maxLength)
        return [raw, `At most ${p.maxLength.toLocaleString()} characters (now ${raw.length.toLocaleString()}).`];
      return [raw, null];
    }
    case "integer":
    case "number": {
      const n = Number(raw);
      if (!Number.isFinite(n)) return [raw, "Enter a number."];
      if (p.type === "integer" && !Number.isInteger(n)) return [raw, "Enter a whole number."];
      if (p.minimum !== undefined && n < p.minimum) return [n, `Must be at least ${p.minimum}.`];
      if (p.maximum !== undefined && n > p.maximum) return [n, `Must be at most ${p.maximum}.`];
      return [n, null];
    }
    case "boolean":
      return [raw === "true", null];
    case "array": {
      const items = raw
        .split(",")
        .map((s) => s.trim())
        .filter(Boolean);
      if (p.maxItems !== undefined && items.length > p.maxItems) return [items, `At most ${p.maxItems} items.`];
      return [items, null];
    }
    default:
      // A shape this renderer does not know: accept JSON for this one field only.
      try {
        return [JSON.parse(raw), null];
      } catch {
        return [raw, "Enter valid JSON for this field."];
      }
  }
}

export function AgentForm({
  agent,
  schema,
  busy,
  serverErrors,
  onRun,
}: {
  agent: string;
  schema: JsonSchema;
  busy: boolean;
  serverErrors: FieldErrors;
  onRun: (payload: Record<string, unknown>) => void;
}) {
  const fields = fieldsOf(agent, schema);
  const [values, setValues] = useState<Record<string, string>>(() =>
    Object.fromEntries(fields.map((f) => [f.name, initialValue(f.prop)])),
  );
  const [errors, setErrors] = useState<FieldErrors>({});
  // A server error belongs to the value that caused it; editing the field clears it.
  const [edited, setEdited] = useState<Set<string>>(new Set());

  function submit(e: FormEvent) {
    e.preventDefault();
    const payload: Record<string, unknown> = {};
    const found: FieldErrors = {};
    for (const f of fields) {
      const [value, error] = checkField(f, values[f.name] ?? "");
      if (error) found[f.name] = error;
      else if (value !== undefined) payload[f.name] = value;
    }
    setErrors(found);
    setEdited(new Set());
    if (Object.keys(found).length === 0) onRun(payload);
  }

  function set(name: string, value: string) {
    setValues((v) => ({ ...v, [name]: value }));
    setEdited((s) => new Set(s).add(name));
    if (errors[name]) setErrors(({ [name]: _gone, ...rest }) => rest);
  }

  const errorFor = (name: string) => errors[name] ?? (edited.has(name) ? undefined : serverErrors[name]);
  const basic = fields.filter((f) => !f.hint.advanced);
  const advanced = fields.filter((f) => f.hint.advanced);

  return (
    <form className="panel" onSubmit={submit} noValidate>
      {basic.map((f) => (
        <FieldInput key={f.name} agent={agent} field={f} value={values[f.name] ?? ""} error={errorFor(f.name)} onChange={set} />
      ))}
      {advanced.length > 0 && (
        <details className="advanced" open={advanced.some((f) => errorFor(f.name))}>
          <summary className="muted">Advanced</summary>
          {advanced.map((f) => (
            <FieldInput key={f.name} agent={agent} field={f} value={values[f.name] ?? ""} error={errorFor(f.name)} onChange={set} />
          ))}
        </details>
      )}
      <div>
        <button disabled={busy}>{busy ? "Running…" : (RUN_LABEL[agent] ?? "Run")}</button>
      </div>
    </form>
  );
}

function FieldInput({
  agent,
  field: f,
  value,
  error,
  onChange,
}: {
  agent: string;
  field: Field;
  value: string;
  error: string | undefined;
  onChange: (name: string, value: string) => void;
}) {
  const id = `f-${agent}-${f.name}`;
  const errId = `${id}-error`;
  const limits = limitsText(f);
  const p = f.prop;
  const common = {
    id,
    name: f.name,
    value,
    "aria-invalid": error ? true : undefined,
    "aria-describedby": error ? errId : undefined,
    className: error ? "invalid" : undefined,
  };
  const long = f.hint.textarea ?? (p.type === "string" && (p.maxLength ?? 0) > 300);

  let input;
  if (p.type === "boolean") {
    input = (
      <input
        {...common}
        type="checkbox"
        checked={value === "true"}
        onChange={(e) => onChange(f.name, String(e.target.checked))}
      />
    );
  } else if (p.type === "integer" || p.type === "number") {
    input = (
      <input
        {...common}
        type="number"
        inputMode={p.type === "integer" ? "numeric" : "decimal"}
        step={p.type === "integer" ? 1 : "any"}
        min={p.minimum}
        max={p.maximum}
        onChange={(e) => onChange(f.name, e.target.value)}
      />
    );
  } else if (long || (p.type !== "string" && p.type !== "array")) {
    input = <textarea {...common} rows={f.hint.rows ?? 4} onChange={(e) => onChange(f.name, e.target.value)} />;
  } else {
    input = <input {...common} onChange={(e) => onChange(f.name, e.target.value)} />;
  }

  return (
    <label htmlFor={id} className="field">
      <span>
        {f.hint.label ?? p.title ?? f.name}
        {!f.required && <span className="muted"> (optional)</span>}
        {limits && <span className="muted"> · {limits}</span>}
      </span>
      {input}
      {f.hint.help && !error && <span className="muted help">{f.hint.help}</span>}
      {error && (
        <span id={errId} className="field-error" role="alert">
          {error}
        </span>
      )}
    </label>
  );
}

// FastAPI 422 detail -> errors keyed by field. loc is ["field"] from the run endpoint's own
// model_validate, or ["body", "field"] from request parsing; anything that names no field we
// render goes back as `rest`, shown above the form as before.
export function splitServerErrors(detail: unknown, fieldNames: string[]): { byField: FieldErrors; rest: string[] } {
  const byField: FieldErrors = {};
  const rest: string[] = [];
  if (!Array.isArray(detail)) {
    rest.push(typeof detail === "string" ? detail : "Invalid input.");
    return { byField, rest };
  }
  for (const d of detail) {
    const row = d as { loc?: unknown[]; msg?: string };
    const path = (row.loc ?? []).filter((p) => p !== "body");
    const name = typeof path[0] === "string" ? path[0] : undefined;
    const msg = (row.msg ?? "Invalid value.").replace(/^Value error, /, "");
    if (name && fieldNames.includes(name)) byField[name] = byField[name] ? `${byField[name]} ${msg}` : msg;
    else rest.push(path.length ? `${path.join(".")}: ${msg}` : msg);
  }
  return { byField, rest };
}
