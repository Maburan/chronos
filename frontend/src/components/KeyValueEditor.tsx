import { XIcon, PlusIcon } from "./icons";

interface KeyValueEditorProps {
  value: Record<string, string>;
  onChange: (next: Record<string, string>) => void;
  keyPlaceholder?: string;
  valuePlaceholder?: string;
}

/**
 * Plain name/value row editor for object-shaped fields (HTTP headers, a
 * calculation's input values, ...) -- the Simple-mode substitute for
 * hand-typed JSON. Internally still just builds an object, but nobody
 * using it ever needs to type a brace or a quote.
 */
export function KeyValueEditor({
  value,
  onChange,
  keyPlaceholder = "name",
  valuePlaceholder = "value",
}: KeyValueEditorProps) {
  const rows = Object.entries(value);

  function updateRow(index: number, key: string, val: string) {
    const next = [...rows];
    next[index] = [key, val];
    onChange(Object.fromEntries(next));
  }

  function removeRow(index: number) {
    const next = rows.filter((_, i) => i !== index);
    onChange(Object.fromEntries(next));
  }

  function addRow() {
    onChange({ ...value, "": "" });
  }

  return (
    <div className="kv">
      {rows.map(([key, val], i) => (
        <div className="kv-row" key={i}>
          <input
            value={key}
            placeholder={keyPlaceholder}
            onChange={(e) => updateRow(i, e.target.value, val)}
          />
          <input
            value={val}
            placeholder={valuePlaceholder}
            onChange={(e) => updateRow(i, key, e.target.value)}
          />
          <button type="button" className="btn ghost icon sm" aria-label="Remove row" onClick={() => removeRow(i)}>
            <XIcon />
          </button>
        </div>
      ))}
      <button type="button" className="btn subtle sm" style={{ alignSelf: "flex-start" }} onClick={addRow}>
        <PlusIcon /> Add
      </button>
    </div>
  );
}
