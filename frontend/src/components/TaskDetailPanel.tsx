import type { Task } from "../api";
import { StateBadge } from "./StateBadge";
import { lifecycleFor } from "../stateStyle";

interface TaskDetailPanelProps {
  task: Task;
  onClose: () => void;
}

function formatDate(iso: string | null): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleString();
}

/**
 * A read-only slide-over showing every field GET /tasks/{id} (via the
 * already-fetched Task) actually returns, plus a lifecycle stepper.
 *
 * The stepper shows the LEGAL PATH for this task's current terminal/
 * in-flight state (see core/transitions.py's state machine, mirrored in
 * stateStyle.ts::lifecycleFor) with steps up to and including the
 * current one marked "passed"/"current" — it does NOT claim to know
 * when each step actually happened, because the API returns only the
 * current row, not a transition history. A retried task's real path
 * (READY -> RUNNING -> READY -> RUNNING -> SUCCEEDED) is genuinely
 * invisible to this view beyond attempt_count; that's disclosed, not
 * hidden.
 */
export function TaskDetailPanel({ task, onClose }: TaskDetailPanelProps) {
  const path = lifecycleFor(task.state);
  const currentIndex = path.indexOf(task.state);

  return (
    <div className="detail-overlay" onClick={onClose}>
      <div className="detail-panel" onClick={(e) => e.stopPropagation()}>
        <div className="detail-panel-header">
          <div>
            <div className="faint" style={{ fontSize: "0.78rem" }}>
              Task #{task.id}
            </div>
            <h2 style={{ margin: "0.15rem 0 0", fontSize: "1.1rem" }}>{task.name}</h2>
          </div>
          <button type="button" className="ghost small" onClick={onClose}>
            ✕
          </button>
        </div>

        <StateBadge state={task.state} />

        <p className="section-title" style={{ marginTop: "var(--space-5)" }}>
          Lifecycle
        </p>
        <div className="lifecycle">
          {path.map((step, i) => (
            <span key={step} style={{ display: "flex", alignItems: "center", gap: "0.2rem" }}>
              <span
                className={
                  "lifecycle-step" +
                  (i < currentIndex ? " passed" : "") +
                  (i === currentIndex ? " current" : "")
                }
                style={i === currentIndex ? { background: "var(--accent)" } : undefined}
              >
                {step}
              </span>
              {i < path.length - 1 && <span className="lifecycle-arrow">→</span>}
            </span>
          ))}
        </div>
        {task.attempt_count > 1 && (
          <p className="faint" style={{ marginTop: "-0.75rem", fontSize: "0.74rem" }}>
            attempt_count={task.attempt_count}: this task was retried. The API only returns the current
            row, not a transition history, so the exact READY↔RUNNING cycles aren't shown here.
          </p>
        )}

        <p className="section-title">Details</p>
        <dl style={{ margin: 0 }}>
          <div className="detail-field">
            <dt>Task type</dt>
            <dd>{task.task_type}</dd>
          </div>
          <div className="detail-field">
            <dt>Workflow</dt>
            <dd>#{task.workflow_id}</dd>
          </div>
          <div className="detail-field">
            <dt>Attempts</dt>
            <dd>
              {task.attempt_count} / {task.max_attempts}
            </dd>
          </div>
          <div className="detail-field">
            <dt>Lease owner</dt>
            <dd>{task.lease_owner ?? "—"}</dd>
          </div>
          <div className="detail-field">
            <dt>Lease expires</dt>
            <dd>{formatDate(task.lease_expires_at)}</dd>
          </div>
          <div className="detail-field">
            <dt>Created</dt>
            <dd>{formatDate(task.created_at)}</dd>
          </div>
          <div className="detail-field">
            <dt>Updated</dt>
            <dd>{formatDate(task.updated_at)}</dd>
          </div>
        </dl>

        <p className="section-title">Payload</p>
        <div className="detail-payload">
          {task.payload ? JSON.stringify(task.payload, null, 2) : "(none)"}
        </div>
      </div>
    </div>
  );
}
