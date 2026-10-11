import type { TaskTypeField, TaskTypeInfo } from "./api";
import { KeyValueEditor } from "./components/KeyValueEditor";

/**
 * Shared metadata-driven field logic -- the single place that turns one
 * TASK_TYPE_METADATA field (via GET /task-types) into a form control and
 * back into a payload value. Used by the workflow builder's node
 * inspector; kept separate from any one component so a new task type or
 * field needs no new frontend branch anywhere that renders a form.
 */

export function defaultFieldValues(info: TaskTypeInfo): Record<string, unknown> {
  const values: Record<string, unknown> = {};
  for (const field of info.fields) {
    if (field.input_type === "keyvalue") {
      values[field.name] = {};
    } else if (field.default !== undefined && field.default !== null) {
      values[field.name] = field.default;
    } else {
      values[field.name] = "";
    }
  }
  return values;
}

/** Every keyvalue field's string values are numeric-coerced only for
 * "variables" (a calculation's inputs) -- a user typing "3" there means
 * the number 3, not the string "3" (see worker/tasks/python_task.py's
 * arithmetic operators, which do string concatenation on strings). Every
 * other keyvalue field (e.g. HTTP headers) keeps plain strings, which is
 * what that transport actually expects. */
export function coerceFieldValue(field: TaskTypeField, raw: unknown): unknown {
  if (field.input_type === "number") {
    if (raw === "" || raw === undefined || raw === null) return undefined;
    const n = Number(raw);
    return Number.isNaN(n) ? raw : n;
  }
  if (field.input_type === "keyvalue") {
    const entries = Object.entries((raw as Record<string, string>) ?? {}).filter(([k]) => k !== "");
    if (field.name === "variables") {
      return Object.fromEntries(
        entries.map(([k, v]) => {
          const n = Number(v);
          return [k, v !== "" && !Number.isNaN(n) ? n : v];
        })
      );
    }
    return Object.fromEntries(entries);
  }
  if (field.input_type === "json") {
    if (typeof raw !== "string" || !raw.trim()) return undefined;
    return JSON.parse(raw);
  }
  return raw === "" ? undefined : raw;
}

export function fieldValuesFromPayload(
  info: TaskTypeInfo,
  payload: Record<string, unknown> | null
): Record<string, unknown> {
  const values = defaultFieldValues(info);
  if (!payload) return values;
  for (const field of info.fields) {
    const raw = payload[field.name];
    if (raw === undefined) continue;
    if (field.input_type === "json") {
      values[field.name] = JSON.stringify(raw, null, 2);
    } else if (field.input_type === "keyvalue") {
      values[field.name] = Object.fromEntries(
        Object.entries(raw as Record<string, unknown>).map(([k, v]) => [k, String(v)])
      );
    } else {
      values[field.name] = raw as unknown;
    }
  }
  return values;
}

/** Builds the payload object for one task type from its current field
 * values. Throws (JSON.parse failure) if an Advanced "json" field
 * contains invalid JSON -- callers should surface that as a validation
 * error, not let it propagate as an unhandled rejection. */
export function buildPayload(
  info: TaskTypeInfo,
  fieldValues: Record<string, unknown>
): { payload: Record<string, unknown>; missingField: TaskTypeField | null } {
  const payload: Record<string, unknown> = {};
  for (const field of info.fields) {
    const coerced = coerceFieldValue(field, fieldValues[field.name]);
    if (field.required && (coerced === undefined || coerced === "")) {
      return { payload, missingField: field };
    }
    if (coerced !== undefined) payload[field.name] = coerced;
  }
  return { payload, missingField: null };
}

export function FieldInput({
  field,
  value,
  onChange,
}: {
  field: TaskTypeField;
  value: unknown;
  onChange: (value: unknown) => void;
}) {
  const label = field.label ?? field.name;
  const labelEl = (
    <span className="field-label">
      {label}
      {field.required && <span className="field-req" title="Required">*</span>}
    </span>
  );
  const help = field.help_text && <span className="field-help">{field.help_text}</span>;

  if (field.input_type === "keyvalue") {
    return (
      <div className="field">
        {labelEl}
        <KeyValueEditor value={(value as Record<string, string>) ?? {}} onChange={onChange} />
        {help}
      </div>
    );
  }

  if (field.input_type === "select" && field.options) {
    return (
      <div className="field">
        {labelEl}
        <select value={(value as string) ?? field.options[0]} onChange={(e) => onChange(e.target.value)}>
          {field.options.map((opt) => (
            <option key={opt} value={opt}>
              {opt}
            </option>
          ))}
        </select>
        {help}
      </div>
    );
  }

  if (field.input_type === "json") {
    return (
      <div className="field">
        {labelEl}
        <textarea
          rows={3}
          value={(value as string) ?? ""}
          placeholder={field.placeholder ?? "{}"}
          spellCheck={false}
          className="mono-input"
          onChange={(e) => onChange(e.target.value)}
        />
        {help}
      </div>
    );
  }

  return (
    <div className="field">
      {labelEl}
      <input
        type={field.input_type === "url" ? "url" : field.input_type === "number" ? "number" : "text"}
        value={(value as string | number) ?? ""}
        placeholder={field.placeholder}
        min={field.validation?.min}
        max={field.validation?.max}
        className={field.input_type === "expression" ? "mono-input" : undefined}
        onChange={(e) => onChange(e.target.value)}
      />
      {help}
    </div>
  );
}
