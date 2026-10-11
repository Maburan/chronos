import { Handle, Position } from "@xyflow/react";
import { PlusIcon } from "../icons";

/**
 * A fixed, non-persisted visual anchor — "here's where this workflow
 * begins," and the entry point for adding the first step (clicking it
 * opens the step picker). It is never sent to the backend:
 * save_workflow_definition has no concept of a start node, and a task with
 * zero dependencies already means "runs immediately," which is exactly
 * what this represents.
 */
export function StartNode() {
  return (
    <div className="wf-start" title="Add a step">
      <span>Start</span>
      <span className="wf-start-plus" aria-hidden="true">
        <PlusIcon />
      </span>
      <Handle type="source" position={Position.Right} isConnectable={false} />
    </div>
  );
}
