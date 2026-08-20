import type { WorkflowStatus } from "../api";
import { EmptyState } from "./EmptyState";

interface MetricCardsProps {
  status: WorkflowStatus | null;
}

const CARDS: Array<{ key: keyof WorkflowStatus; label: string; tone: string }> = [
  { key: "pending", label: "Pending", tone: "pending" },
  { key: "ready", label: "Ready", tone: "ready" },
  { key: "running", label: "Running", tone: "running" },
  { key: "succeeded", label: "Succeeded", tone: "succeeded" },
  { key: "failed_terminal", label: "Failed", tone: "failed" },
  { key: "skipped", label: "Skipped", tone: "skipped" },
];

/**
 * Renders GET /workflows/{id}/status verbatim as metric cards. Every
 * number here is computed by one PostgreSQL aggregation query
 * (db/queries.py::SELECT_WORKFLOW_TASK_STATE_COUNTS) — this component
 * does no counting of its own. The "running" card's subtle pulse is the
 * one bit of motion here, and it's driven directly by `running > 0`,
 * not a fabricated animation.
 */
export function MetricCards({ status }: MetricCardsProps) {
  if (!status) {
    return (
      <section className="card">
        <div className="card-title">
          <h2>Workflow summary</h2>
        </div>
        <EmptyState icon="◌" title="No workflow selected" detail="Create or load a workflow to see its task states." />
      </section>
    );
  }

  return (
    <section className="card">
      <div className="card-title">
        <h2>Workflow summary</h2>
        <span className="hint">{status.total_tasks} task{status.total_tasks === 1 ? "" : "s"} total</span>
      </div>

      <div className="metric-grid">
        {CARDS.map(({ key, label, tone }) => {
          const value = status[key] as number;
          const isRunning = key === "running" && value > 0;
          return (
            <div key={key} className={`metric-card${isRunning ? " active" : ""}`} data-tone={tone}>
              <div className="metric-value">{value}</div>
              <div className="metric-label">{label}</div>
            </div>
          );
        })}
      </div>

      <div className="completion-row">
        {status.is_complete ? (
          <span className="badge badge-succeeded">✓ Workflow complete</span>
        ) : (
          <span className="badge badge-running">in progress</span>
        )}
        {status.has_failures && <span className="badge badge-failed">has failures</span>}
        {status.total_tasks === 0 && <span className="muted">No tasks yet — create one below.</span>}
      </div>
    </section>
  );
}
