import { Handle, Position, type NodeProps } from "@xyflow/react";
import { useAdvancedMode } from "../../advancedMode";
import { AlertIcon } from "../icons";

export interface StepNodeData {
  name: string;
  icon: string;
  typeLabel: string;
  taskType: string;
  incomplete: boolean;
  shake?: boolean;
  [key: string]: unknown;
}

/**
 * One step on the builder canvas — a workflow_definition-time node. It has
 * no live execution state (no run has necessarily happened yet), so it
 * never renders a run-status badge; showing "Running"/"Done" here without
 * a real task behind it would be fabricated state. It communicates what
 * the step is (icon + name + task-type label) and, if required fields are
 * missing, an unmistakable "needs attention" flag.
 */
export function StepNode({ data, selected }: NodeProps) {
  const { advanced } = useAdvancedMode();
  const d = data as unknown as StepNodeData;

  return (
    <div className={`wf-node${selected ? " selected" : ""}${d.incomplete ? " incomplete" : ""}${d.shake ? " shake" : ""}`}>
      <Handle type="target" position={Position.Left} />
      <div className="wf-node-body">
        <div className="wf-node-icon" aria-hidden="true">
          {d.icon}
        </div>
        <div className="wf-node-text">
          <div className={`wf-node-name${d.name ? "" : " placeholder"}`}>{d.name || "Untitled step"}</div>
          <div className="wf-node-type">
            {d.typeLabel}
            {advanced && <span className="faint"> · {d.taskType}</span>}
          </div>
        </div>
      </div>
      {d.incomplete && (
        <div className="wf-node-flag" title="This step is missing required information">
          <AlertIcon />
        </div>
      )}
      <Handle type="source" position={Position.Right} />
    </div>
  );
}
