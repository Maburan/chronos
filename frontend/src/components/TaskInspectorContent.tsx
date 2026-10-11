import type { Task, TaskState } from "../api";
import { StateBadge } from "./StateBadge";
import { lifecycleFor, STATE_LABEL } from "../stateStyle";
import { useAdvancedMode } from "../advancedMode";
import { useTaskTypes, taskTypeInfo } from "../useTaskTypes";
import { XIcon } from "./icons";

/**
 * The inner content of a task inspector (eyebrow, title, status, lifecycle
 * stepper, details, payload) — head + body, no overlay/scrim. Rendered
 * inside a floating `.canvas-inspector` panel so it sits over the workflow
 * canvas without dimming it: the graph stays visible while you inspect a
 * step. Simple mode explains what happened in plain language; Advanced
 * adds the raw lease/attempt internals and the exact payload.
 */
function formatDate(iso: string | null): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleString();
}

export function TaskInspectorContent({ task, onClose }: { task: Task; onClose: () => void }) {
  const { advanced } = useAdvancedMode();
  const taskTypes = useTaskTypes();
  const info = taskTypeInfo(taskTypes, task.task_type);

  const rawPath = lifecycleFor(task.state);
  const labels = rawPath.map((s) => (advanced ? s : STATE_LABEL[s]));
  const isFailedAt = rawPath.map((s) => s === "FAILED_TERMINAL");
  const steps: Array<{ label: string; failed: boolean; rawIndex: number }> = [];
  labels.forEach((label, i) => {
    if (steps.length > 0 && steps[steps.length - 1].label === label) return;
    steps.push({ label, failed: isFailedAt[i], rawIndex: i });
  });
  const currentRaw = rawPath.indexOf(task.state as TaskState);
  let currentIndex = 0;
  steps.forEach((s, i) => {
    if (s.rawIndex <= currentRaw) currentIndex = i;
  });

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
            {task.name}
          </div>
        </div>
        <button type="button" className="btn ghost icon sm" aria-label="Close details" onClick={onClose}>
          <XIcon />
        </button>
      </div>

      <div className="inspector-body">
        <div>
          <StateBadge state={task.state} />
        </div>

        <div>
          <p className="section-label">Progress</p>
          <div className="stepper">
            {steps.map((step, i) => (
              <span key={i} style={{ display: "flex", alignItems: "center", gap: "4px" }}>
                <span
                  className={
                    "stepper-node" +
                    (i < currentIndex ? " passed" : "") +
                    (i === currentIndex ? " current" : "") +
                    (i === currentIndex && step.failed ? " failed" : "")
                  }
                >
                  {step.label}
                </span>
                {i < steps.length - 1 && <span className="stepper-arrow">▶</span>}
              </span>
            ))}
          </div>
          {task.attempt_count > 1 && (
            <p className="field-help" style={{ marginTop: "var(--s-2)" }}>
              Retried {task.attempt_count - 1} time{task.attempt_count - 1 === 1 ? "" : "s"} before reaching this status.
            </p>
          )}
        </div>

        <div>
          <p className="section-label">Details</p>
          <dl className="detail-list">
            {advanced && (
              <div className="detail-row">
                <dt>Task type</dt>
                <dd>{task.task_type}</dd>
              </div>
            )}
            <div className="detail-row">
              <dt>Workflow</dt>
              <dd>#{task.workflow_id}</dd>
            </div>
            {advanced && (
              <div className="detail-row">
                <dt>Attempts</dt>
                <dd>{task.attempt_count} / {task.max_attempts}</dd>
              </div>
            )}
            {advanced && (
              <div className="detail-row">
                <dt>Lease owner</dt>
                <dd>{task.lease_owner ?? "—"}</dd>
              </div>
            )}
            {advanced && (
              <div className="detail-row">
                <dt>Lease expires</dt>
                <dd>{formatDate(task.lease_expires_at)}</dd>
              </div>
            )}
            <div className="detail-row">
              <dt>Created</dt>
              <dd>{formatDate(task.created_at)}</dd>
            </div>
            <div className="detail-row">
              <dt>Updated</dt>
              <dd>{formatDate(task.updated_at)}</dd>
            </div>
          </dl>
        </div>

        {advanced && (
          <div>
            <p className="section-label">Payload</p>
            <div className="code-block">{task.payload ? JSON.stringify(task.payload, null, 2) : "(none)"}</div>
          </div>
        )}
      </div>
    </>
  );
}
