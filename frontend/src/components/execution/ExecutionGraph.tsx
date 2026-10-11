import { useEffect, useMemo, useRef, useState } from "react";
import {
  ReactFlow,
  BaseEdge,
  getSmoothStepPath,
  Handle,
  Position,
  MarkerType,
  type Node,
  type Edge,
  type NodeProps,
  type EdgeProps,
} from "@xyflow/react";
import type { Task, TaskState } from "../../api";
import { STATE_LABEL } from "../../stateStyle";
import { useAdvancedMode } from "../../advancedMode";
import { useTaskTypes, taskTypeInfo } from "../../useTaskTypes";
import { depthMap, edgeActivity, prefersReducedMotion, type EdgeActivity } from "../../execution";

/**
 * A live, React Flow-based view of a run as *work moving through a DAG* —
 * the visual centerpiece of "watch it execute". Every node's colour and
 * every edge's activity is a pure reading of the real task states polled
 * from the API: a running node pulses, edges carrying work show flowing
 * particles, completed paths settle, a failed node reacts once. Nothing is
 * simulated — when the run is finished, nothing animates.
 */

interface ExecNodeData {
  task: Task;
  icon: string;
  label: string;
  reduced: boolean;
  [key: string]: unknown;
}

function ExecNode({ data }: NodeProps) {
  const d = data as unknown as ExecNodeData;
  const state = d.task.state;
  const { advanced } = useAdvancedMode();

  const prev = useRef<TaskState>(state);
  const [flash, setFlash] = useState(false);
  useEffect(() => {
    if (prev.current !== state) {
      prev.current = state;
      if (!d.reduced) {
        setFlash(true);
        const id = window.setTimeout(() => setFlash(false), 500);
        return () => window.clearTimeout(id);
      }
    }
  }, [state, d.reduced]);

  const shake = flash && state === "FAILED_TERMINAL";

  return (
    <div className={`exec-node${flash ? " changed" : ""}${shake ? " shake" : ""}`} data-state={state}>
      <Handle type="target" position={Position.Top} isConnectable={false} />
      <div className="exec-node-body">
        <div className="exec-node-icon" aria-hidden="true">
          {d.icon}
        </div>
        <div className="exec-node-text">
          <div className="exec-node-name">{d.label}</div>
          <div className="exec-node-state">
            <span className="dot" />
            {advanced ? state : STATE_LABEL[state]}
          </div>
        </div>
      </div>
      <Handle type="source" position={Position.Bottom} isConnectable={false} />
    </div>
  );
}

const EDGE_COLOR: Record<EdgeActivity, string> = {
  flow: "var(--accent)",
  done: "var(--c-succeeded)",
  failed: "var(--c-failed)",
  idle: "var(--line-strong)",
};

interface FlowEdgeData {
  activity: EdgeActivity;
  reduced: boolean;
  [key: string]: unknown;
}

function FlowEdge({ sourceX, sourceY, targetX, targetY, sourcePosition, targetPosition, markerEnd, data }: EdgeProps) {
  const [path] = getSmoothStepPath({ sourceX, sourceY, targetX, targetY, sourcePosition, targetPosition, borderRadius: 12 });
  const d = data as unknown as FlowEdgeData;
  const color = EDGE_COLOR[d.activity];
  const flowing = d.activity === "flow" && !d.reduced;

  return (
    <>
      <BaseEdge
        path={path}
        markerEnd={markerEnd}
        style={{
          stroke: color,
          strokeWidth: d.activity === "flow" ? 2 : 1.6,
          opacity: d.activity === "idle" ? 0.55 : 1,
          strokeDasharray: d.activity === "failed" ? "5 4" : undefined,
          transition: "stroke 340ms ease, opacity 340ms ease",
        }}
      />
      {flowing && (
        <>
          <circle r={2.7} fill="var(--accent)">
            <animateMotion dur="1.5s" repeatCount="indefinite" path={path} />
          </circle>
          <circle r={2.4} fill="var(--accent)" opacity={0.55}>
            <animateMotion dur="1.5s" begin="0.75s" repeatCount="indefinite" path={path} />
          </circle>
        </>
      )}
    </>
  );
}

const nodeTypes = { exec: ExecNode };
const edgeTypes = { flow: FlowEdge };

// top-down layout: depth flows DOWN the canvas, siblings spread horizontally
const V_GAP = 132;
const H_GAP = 210;

interface ExecutionGraphProps {
  tasks: Task[];
  onSelectTask?: (task: Task) => void;
}

export function ExecutionGraph({ tasks, onSelectTask }: ExecutionGraphProps) {
  const taskTypes = useTaskTypes();
  const reduced = prefersReducedMotion();

  const { nodes, edges } = useMemo(() => {
    const depths = depthMap(tasks);
    const byId = new Map(tasks.map((t) => [t.id, t]));

    // group by depth (each depth is one horizontal row, laid top → bottom)
    const layers = new Map<number, number[]>();
    for (const t of tasks) {
      const dpt = depths.get(t.id)!;
      if (!layers.has(dpt)) layers.set(dpt, []);
      layers.get(dpt)!.push(t.id);
    }
    const pos = new Map<number, { x: number; y: number }>();
    for (const [dpt, ids] of layers) {
      ids.sort((a, b) => a - b);
      const mid = (ids.length - 1) / 2;
      ids.forEach((id, row) => pos.set(id, { x: (row - mid) * H_GAP, y: dpt * V_GAP }));
    }

    const flowNodes: Node[] = tasks.map((t) => {
      const info = taskTypeInfo(taskTypes, t.task_type);
      return {
        id: String(t.id),
        type: "exec",
        position: pos.get(t.id)!,
        sourcePosition: Position.Bottom,
        targetPosition: Position.Top,
        data: { task: t, icon: info?.icon ?? "▢", label: t.name, reduced },
        draggable: false,
        connectable: false,
      };
    });

    const flowEdges: Edge[] = [];
    for (const t of tasks) {
      for (const depId of t.depends_on_ids) {
        const src = byId.get(depId);
        if (!src) continue;
        const activity = edgeActivity(src.state, t.state);
        flowEdges.push({
          id: `e-${depId}-${t.id}`,
          source: String(depId),
          target: String(t.id),
          type: "flow",
          data: { activity, reduced },
          markerEnd: { type: MarkerType.ArrowClosed, color: EDGE_COLOR[activity], width: 16, height: 16 },
        });
      }
    }
    return { nodes: flowNodes, edges: flowEdges };
  }, [tasks, taskTypes, reduced]);

  return (
    <div className="exec-graph">
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        edgeTypes={edgeTypes}
        onInit={(inst) => inst.fitView({ padding: 0.28, maxZoom: 1.1 })}
        nodesDraggable={false}
        nodesConnectable={false}
        elementsSelectable={!!onSelectTask}
        panOnScroll={false}
        zoomOnScroll={false}
        preventScrolling={false}
        proOptions={{ hideAttribution: true }}
        onNodeClick={(_, node) => {
          const t = tasks.find((x) => String(x.id) === node.id);
          if (t && onSelectTask) onSelectTask(t);
        }}
      />
    </div>
  );
}
