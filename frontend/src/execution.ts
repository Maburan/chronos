import type { Task, TaskState } from "./api";

/**
 * Shared helpers for visualizing a run as *work moving through a graph* —
 * used by both the live ExecutionGraph (detail page) and the MiniDag
 * strips (workflow cards). Everything here is a pure reading of real task
 * states returned by the API; nothing is fabricated or simulated.
 */

export interface StateCounts {
  total: number;
  pending: number;
  ready: number;
  running: number;
  succeeded: number;
  failed_terminal: number;
  skipped: number;
  /** terminal-and-not-skipped-or-not — "done enough to count as progress" */
  settled: number;
  /** 0–100, fraction of tasks in a terminal state */
  percent: number;
}

const TERMINAL: TaskState[] = ["SUCCEEDED", "FAILED_TERMINAL", "SKIPPED"];

export function countStates(tasks: Task[]): StateCounts {
  const c: StateCounts = {
    total: tasks.length,
    pending: 0,
    ready: 0,
    running: 0,
    succeeded: 0,
    failed_terminal: 0,
    skipped: 0,
    settled: 0,
    percent: 0,
  };
  for (const t of tasks) {
    if (t.state === "PENDING") c.pending++;
    else if (t.state === "READY") c.ready++;
    else if (t.state === "RUNNING") c.running++;
    else if (t.state === "SUCCEEDED") c.succeeded++;
    else if (t.state === "FAILED_TERMINAL") c.failed_terminal++;
    else if (t.state === "SKIPPED") c.skipped++;
    if (TERMINAL.includes(t.state)) c.settled++;
  }
  c.percent = c.total === 0 ? 0 : Math.round((c.settled / c.total) * 100);
  return c;
}

/** depth(task) = 1 + max(depth(dep)), 0 for a root. Cycle-guarded. */
export function depthMap(tasks: Task[]): Map<number, number> {
  const byId = new Map(tasks.map((t) => [t.id, t]));
  const cache = new Map<number, number>();
  function depth(id: number, visiting: Set<number>): number {
    if (cache.has(id)) return cache.get(id)!;
    if (visiting.has(id)) return 0;
    const deps = (byId.get(id)?.depends_on_ids ?? []).filter((d) => byId.has(d));
    if (deps.length === 0) {
      cache.set(id, 0);
      return 0;
    }
    visiting.add(id);
    const d = 1 + Math.max(...deps.map((x) => depth(x, visiting)));
    visiting.delete(id);
    cache.set(id, d);
    return d;
  }
  const out = new Map<number, number>();
  for (const t of tasks) out.set(t.id, depth(t.id, new Set()));
  return out;
}

/** Tasks in execution order: by dependency depth, then id. */
export function topoOrder(tasks: Task[]): Task[] {
  const depths = depthMap(tasks);
  return [...tasks].sort((a, b) => (depths.get(a.id)! - depths.get(b.id)!) || a.id - b.id);
}

export type EdgeActivity = "flow" | "done" | "failed" | "idle";

/**
 * How an edge from `src` → `tgt` should read, from the real states of the
 * two tasks it connects:
 *   flow   — work is actively moving across it (something is running, or a
 *            finished upstream is feeding a not-yet-started downstream)
 *   done   — both ends are settled successfully; the path is "complete"
 *   failed — the downstream failed or was skipped (a dead path)
 *   idle   — nothing has reached this edge yet
 */
export function edgeActivity(src: TaskState, tgt: TaskState): EdgeActivity {
  if (tgt === "RUNNING" || src === "RUNNING") return "flow";
  if (src === "SUCCEEDED" && (tgt === "READY" || tgt === "PENDING")) return "flow";
  if (src === "SUCCEEDED" && tgt === "SUCCEEDED") return "done";
  if (tgt === "FAILED_TERMINAL" || tgt === "SKIPPED" || src === "FAILED_TERMINAL") return "failed";
  return "idle";
}

export function prefersReducedMotion(): boolean {
  return typeof window !== "undefined" && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}
