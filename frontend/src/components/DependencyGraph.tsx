import type { Task } from "../api";
import { STATE_COLOR_VAR } from "../stateStyle";
import { EmptyState } from "./EmptyState";

interface DependencyGraphProps {
  tasks: Task[];
  // task_id -> depends_on_ids, remembered client-side at creation time.
  // See App.tsx's dependencyEdges state for why this can't come from the
  // API: no backend endpoint reads task_dependencies back out.
  dependencyEdges: Map<number, number[]>;
}

const NODE_W = 148;
const NODE_H = 50;
const COL_GAP = 64;
const ROW_GAP = 22;
const PADDING = 20;

/**
 * Draws a layered DAG view from client-side-remembered dependency edges
 * only (see dependencyEdges prop doc). A task never seen with a
 * depends_on_ids value in this session (whether it truly has none, or it
 * was just created before this page loaded) is drawn as depth 0 and
 * marked "edges not tracked" (dashed border) so it's never confused with
 * a verified, dependency-free root.
 */
export function DependencyGraph({ tasks, dependencyEdges }: DependencyGraphProps) {
  if (tasks.length === 0) {
    return (
      <section className="card">
        <div className="card-title">
          <h2>Dependency graph</h2>
        </div>
        <EmptyState icon="◇" title="No tasks yet" />
      </section>
    );
  }

  const taskById = new Map(tasks.map((t) => [t.id, t]));

  // depth(task) = 1 + max(depth(dep) for dep in known deps), 0 if no
  // known deps recorded for this task at all. Cycle-guarded defensively
  // (the backend's own FK/DAG rules prevent real cycles, but this map is
  // client-derived and could in principle be inconsistent, e.g. after
  // loading a workflow whose tasks were created elsewhere).
  const depthCache = new Map<number, number>();
  function depthOf(taskId: number, visiting: Set<number>): number {
    if (depthCache.has(taskId)) return depthCache.get(taskId)!;
    if (visiting.has(taskId)) return 0; // cycle guard
    const deps = dependencyEdges.get(taskId);
    if (!deps || deps.length === 0) {
      depthCache.set(taskId, 0);
      return 0;
    }
    visiting.add(taskId);
    const known = deps.filter((d) => taskById.has(d));
    const depth = known.length === 0 ? 0 : 1 + Math.max(...known.map((d) => depthOf(d, visiting)));
    visiting.delete(taskId);
    depthCache.set(taskId, depth);
    return depth;
  }

  const layers = new Map<number, number[]>();
  for (const t of tasks) {
    const d = depthOf(t.id, new Set());
    if (!layers.has(d)) layers.set(d, []);
    layers.get(d)!.push(t.id);
  }

  const positions = new Map<number, { x: number; y: number }>();
  const sortedDepths = [...layers.keys()].sort((a, b) => a - b);
  let maxRows = 1;
  for (const depth of sortedDepths) {
    const ids = layers.get(depth)!.sort((a, b) => a - b);
    maxRows = Math.max(maxRows, ids.length);
    ids.forEach((id, row) => {
      positions.set(id, {
        x: PADDING + depth * (NODE_W + COL_GAP),
        y: PADDING + row * (NODE_H + ROW_GAP),
      });
    });
  }

  const width = PADDING * 2 + (sortedDepths.length - 1) * (NODE_W + COL_GAP) + NODE_W;
  const height = PADDING * 2 + maxRows * (NODE_H + ROW_GAP);

  const edges: Array<{ from: number; to: number }> = [];
  for (const [taskId, deps] of dependencyEdges.entries()) {
    if (!taskById.has(taskId)) continue;
    for (const depId of deps) {
      if (positions.has(depId) && positions.has(taskId)) {
        edges.push({ from: depId, to: taskId });
      }
    }
  }

  const trackedCount = tasks.filter((t) => dependencyEdges.has(t.id)).length;

  return (
    <section className="card">
      <div className="card-title">
        <h2>Dependency graph</h2>
        <span className="hint">
          {trackedCount}/{tasks.length} edge sets known this session
        </span>
      </div>
      <p className="card-sub">
        Edges are remembered client-side at task-creation time only — the API has no endpoint to read a
        task's dependencies back out. Dashed nodes had no <code>depends_on_ids</code> submitted this
        session; they may still have real dependencies enforced by the backend that this view can't see.
      </p>
      <div className="graph-wrap">
        <svg width={width} height={height} role="img" aria-label="Task dependency graph">
          <defs>
            <marker id="dep-arrow" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto">
              <path d="M0,0 L8,4 L0,8 z" fill="var(--text-faint)" />
            </marker>
          </defs>
          {edges.map((e, i) => {
            const from = positions.get(e.from)!;
            const to = positions.get(e.to)!;
            const x1 = from.x + NODE_W;
            const y1 = from.y + NODE_H / 2;
            const x2 = to.x;
            const y2 = to.y + NODE_H / 2;
            const midX = (x1 + x2) / 2;
            return (
              <path
                key={i}
                d={`M ${x1} ${y1} C ${midX} ${y1}, ${midX} ${y2}, ${x2} ${y2}`}
                fill="none"
                style={{ stroke: "var(--text-faint)" }}
                strokeWidth={1.5}
                markerEnd="url(#dep-arrow)"
              />
            );
          })}
          {tasks.map((t) => {
            const pos = positions.get(t.id);
            if (!pos) return null;
            const tracked = dependencyEdges.has(t.id);
            const color = STATE_COLOR_VAR[t.state];
            return (
              <g key={t.id} transform={`translate(${pos.x}, ${pos.y})`}>
                <rect
                  width={NODE_W}
                  height={NODE_H}
                  rx={8}
                  style={{ fill: "var(--bg-panel)", stroke: color }}
                  strokeWidth={1.5}
                  strokeDasharray={tracked ? undefined : "4 3"}
                />
                <text x={10} y={19} fontSize={11.5} fontWeight={600} style={{ fill: "var(--text)" }}>
                  #{t.id} {t.name.length > 15 ? t.name.slice(0, 14) + "…" : t.name}
                </text>
                <text x={10} y={37} fontSize={10} fontFamily="var(--mono)" style={{ fill: color }}>
                  {t.state}
                </text>
              </g>
            );
          })}
        </svg>
      </div>
    </section>
  );
}
