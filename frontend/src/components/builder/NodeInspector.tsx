import { useState } from "react";
import type { TaskTypeInfo } from "../../api";
import { FieldInput } from "../../taskFieldForm";
import { useAdvancedMode } from "../../advancedMode";
import { XIcon, TrashIcon, ChevronRightIcon, BranchIcon } from "../icons";

export interface InspectorNode {
  id: string;
  name: string;
  taskType: string;
  fieldValues: Record<string, unknown>;
  maxAttempts: number;
}

interface NodeInspectorProps {
  node: InspectorNode;
  info: TaskTypeInfo | undefined;
  dependencyNames: string[];
  onNameChange: (name: string) => void;
  onFieldChange: (fieldName: string, value: unknown) => void;
  onMaxAttemptsChange: (value: number) => void;
  onDelete: () => void;
  onClose: () => void;
}

/**
 * Docked configuration panel for the selected canvas node — part of the
 * builder layout, not a detached form. Dependencies are shown read-only
 * here; they're created and removed by dragging connections on the canvas
 * (the visual, obvious mechanism), never by a checkbox list duplicating
 * that same information.
 */
export function NodeInspector({
  node,
  info,
  dependencyNames,
  onNameChange,
  onFieldChange,
  onMaxAttemptsChange,
  onDelete,
  onClose,
}: NodeInspectorProps) {
  const { advanced } = useAdvancedMode();
  const [showMore, setShowMore] = useState(false);

  const simpleFields = info?.fields.filter((f) => !f.advanced) ?? [];
  const advancedFields = info?.fields.filter((f) => f.advanced) ?? [];

  return (
    <>
      <div className="inspector-head">
        <div>
          <div className="sheet-eyebrow">
            {info ? (
              <>
                <span aria-hidden="true">{info.icon}</span> {info.label}
              </>
            ) : (
              "Step"
            )}
          </div>
          <div className="sheet-title" style={{ fontSize: "var(--fs-15)" }}>
            Configure step
          </div>
        </div>
        <button type="button" className="btn ghost icon sm" aria-label="Close settings" onClick={onClose}>
          <XIcon />
        </button>
      </div>

      <div className="inspector-body">
        <div className="field">
          <span className="field-label">Step name</span>
          <input value={node.name} onChange={(e) => onNameChange(e.target.value)} placeholder="e.g. Fetch latest data" />
        </div>

        {info?.description && <p className="field-help">{info.description}</p>}

        {simpleFields.map((field) => (
          <FieldInput
            key={field.name}
            field={field}
            value={node.fieldValues[field.name]}
            onChange={(v) => onFieldChange(field.name, v)}
          />
        ))}

        <div>
          <p className="section-label" style={{ display: "flex", alignItems: "center", gap: 5 }}>
            <BranchIcon width={13} height={13} /> Runs after
          </p>
          {dependencyNames.length === 0 ? (
            <p className="field-help">
              Nothing — this step starts right away. Drag a connection from another step's right edge into this one to
              change that.
            </p>
          ) : (
            <div className="def-chips">
              {dependencyNames.map((name, i) => (
                <span key={i} className="def-chip">
                  {name}
                </span>
              ))}
            </div>
          )}
        </div>

        <button
          type="button"
          className="btn subtle sm"
          style={{ alignSelf: "flex-start" }}
          onClick={() => setShowMore((v) => !v)}
          aria-expanded={showMore}
        >
          <ChevronRightIcon
            style={{ transform: showMore ? "rotate(90deg)" : "none", transition: "transform var(--dur-2)" }}
          />
          More options
        </button>

        {showMore && (
          <div className="advanced-panel">
            {advancedFields.map((field) => (
              <FieldInput
                key={field.name}
                field={field}
                value={node.fieldValues[field.name]}
                onChange={(v) => onFieldChange(field.name, v)}
              />
            ))}
            <div className="field" style={{ maxWidth: 160 }}>
              <span className="field-label">Retry up to</span>
              <input
                type="number"
                min={1}
                value={node.maxAttempts}
                onChange={(e) => onMaxAttemptsChange(Math.max(1, Number(e.target.value) || 1))}
              />
              <span className="field-help">How many times to retry this step if it fails.</span>
            </div>
            {advanced && (
              <p className="field-help">
                task_type: <code>{node.taskType}</code>
              </p>
            )}
          </div>
        )}

        <button type="button" className="btn danger sm" style={{ alignSelf: "flex-start" }} onClick={onDelete}>
          <TrashIcon /> Remove step
        </button>
      </div>
    </>
  );
}
