import { useEffect, useState } from "react";
import type { TaskTypeInfo } from "../../api";
import { SearchIcon, XIcon } from "../icons";

const CATEGORY_LABELS: Record<string, string> = {
  utility: "Utility",
  data: "Data",
  logic: "Logic",
};

function categoryLabel(category: string | undefined): string {
  if (!category) return "Other";
  return CATEGORY_LABELS[category] ?? category[0].toUpperCase() + category.slice(1);
}

/**
 * "Choose a step" — entirely metadata-driven from GET /task-types
 * (TASK_TYPE_METADATA). A new backend task type appears here
 * automatically, grouped by its declared category, with zero changes to
 * this component.
 */
export function StepPicker({
  taskTypes,
  onPick,
  onClose,
}: {
  taskTypes: TaskTypeInfo[];
  onPick: (info: TaskTypeInfo) => void;
  onClose: () => void;
}) {
  const [query, setQuery] = useState("");

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const q = query.trim().toLowerCase();
  const filtered = taskTypes.filter(
    (t) => !q || t.label.toLowerCase().includes(q) || t.description.toLowerCase().includes(q)
  );

  const grouped = new Map<string, TaskTypeInfo[]>();
  for (const t of filtered) {
    const key = t.category ?? "other";
    if (!grouped.has(key)) grouped.set(key, []);
    grouped.get(key)!.push(t);
  }

  return (
    <div className="overlay modal-scrim" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()} role="dialog" aria-label="Choose a step">
        <div style={{ padding: "var(--s-5)" }}>
          <div className="card-head">
            <h2>Add a step</h2>
            <button type="button" className="btn ghost icon sm" aria-label="Close" onClick={onClose}>
              <XIcon />
            </button>
          </div>

          <div className="picker-search" style={{ marginBottom: "var(--s-4)" }}>
            <SearchIcon />
            <input
              autoFocus
              type="text"
              placeholder="Search steps…"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
            />
          </div>

          {filtered.length === 0 ? (
            <p className="muted" style={{ fontSize: "var(--fs-13)" }}>
              No steps match “{query}”.
            </p>
          ) : (
            [...grouped.entries()].map(([category, types]) => (
              <div key={category} className="picker-group">
                <p className="section-label">{categoryLabel(category)}</p>
                <div className="picker-grid">
                  {types.map((t) => (
                    <button key={t.type} type="button" className="picker-tile" onClick={() => onPick(t)}>
                      <span className="picker-tile-icon" aria-hidden="true">
                        {t.icon ?? "▢"}
                      </span>
                      <span className="picker-tile-body">
                        <span className="picker-tile-label">{t.label}</span>
                        <span className="picker-tile-desc">{t.description}</span>
                      </span>
                    </button>
                  ))}
                </div>
              </div>
            ))
          )}
        </div>
      </div>
    </div>
  );
}
