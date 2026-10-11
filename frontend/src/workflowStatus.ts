import type { WorkflowStatus } from "./api";

/** The subset of status fields these helpers actually read — lets them
 * work with either a workflow-level status or a single run's status. */
type StatusCounts = Pick<
  WorkflowStatus,
  "total_tasks" | "pending" | "ready" | "running" | "succeeded" | "failed_terminal" | "skipped" | "is_complete" | "has_failures"
>;

/**
 * Derives a single high-level display status for a workflow from the
 * per-state counts GET /workflows/{id}/status already returns — the one
 * place that mapping lives, so the dashboard, the workflow list, and the
 * detail header all describe a workflow's status identically.
 *
 * Nothing here is invented: every branch is a plain reading of fields the
 * backend computed (is_complete / has_failures / the six state counts).
 */
export type WorkflowDisplayTone = "draft" | "running" | "waiting" | "completed" | "failed" | "unknown";

export interface WorkflowDisplayStatus {
  tone: WorkflowDisplayTone;
  label: string;
  badgeClass: string;
}

export function deriveWorkflowStatus(status: StatusCounts | null): WorkflowDisplayStatus {
  if (!status) return { tone: "unknown", label: "Unavailable", badgeClass: "badge-neutral" };
  if (status.total_tasks === 0) return { tone: "draft", label: "Draft", badgeClass: "badge-neutral" };
  if (status.is_complete && status.has_failures) return { tone: "failed", label: "Failed", badgeClass: "badge-failed" };
  if (status.is_complete) return { tone: "completed", label: "Completed", badgeClass: "badge-succeeded" };
  if (status.running > 0) return { tone: "running", label: "Running", badgeClass: "badge-running" };
  return { tone: "waiting", label: "In progress", badgeClass: "badge-running" };
}

/** Proportional segments (succeeded / failed / skipped / running / waiting)
 * for a workflow-status progress meter. Percentages sum to 100 when there
 * is at least one task; an empty definition yields an all-waiting bar. */
export function statusMeterSegments(status: StatusCounts): Array<{ kind: string; pct: number }> {
  const total = status.total_tasks || 1;
  const waiting = status.pending + status.ready;
  return [
    { kind: "succeeded", pct: (status.succeeded / total) * 100 },
    { kind: "failed", pct: (status.failed_terminal / total) * 100 },
    { kind: "skipped", pct: (status.skipped / total) * 100 },
    { kind: "running", pct: (status.running / total) * 100 },
    { kind: "waiting", pct: (waiting / total) * 100 },
  ].filter((s) => s.pct > 0);
}
