import { Fragment } from "react";
import type { WorkflowStatus } from "../api";

interface PipelineVisualProps {
  status: WorkflowStatus | null;
}

const STAGES: Array<{ icon: string; name: string; detail: string }> = [
  { icon: "▣", name: "API", detail: "POST /workflows/{id}/tasks" },
  { icon: "▤", name: "Postgres / Outbox", detail: "task row + outbox row, same transaction" },
  { icon: "↻", name: "Outbox Relay", detail: "polls outbox, publishes on confirm" },
  { icon: "≋", name: "RabbitMQ", detail: "dispatch hint only, not truth" },
  { icon: "⚙", name: "Worker", detail: "claims, executes, reports" },
  { icon: "●", name: "Task Execution", detail: "SUCCEEDED / retried / failed" },
];

/**
 * A static, educational diagram of the Chronos pipeline — NOT a live
 * infrastructure monitor. The API has no endpoint that reports relay,
 * RabbitMQ, or worker health individually, so this component never
 * claims to know any of that per-stage. The one real signal it uses:
 * whether the *current workflow* has any READY or RUNNING tasks (from
 * GET /workflows/{id}/status, already-fetched data) — if so, there is
 * genuinely work moving through this pipeline right now, so the whole
 * diagram gets a subtle "active" treatment. That is real, not invented;
 * everything else here is a fixed explanation of the architecture.
 */
export function PipelineVisual({ status }: PipelineVisualProps) {
  const active = !!status && (status.ready > 0 || status.running > 0);

  return (
    <section className="card">
      <div className="card-title">
        <h2>What happens after you click "Create task"</h2>
        <span className="hint">{active ? "work in flight for this workflow" : "static architecture reference"}</span>
      </div>
      <p className="card-sub">
        Every stage is real Chronos code — see HANDOVER.md. This diagram doesn't monitor RabbitMQ or the
        worker process individually (the API has no endpoint for that); it only reflects whether the
        current workflow has READY/RUNNING tasks right now.
      </p>
      <div className={`pipeline${active ? " active" : ""}`}>
        {STAGES.map((stage, i) => (
          <Fragment key={stage.name}>
            <div className="pipeline-stage">
              <div className="stage-icon">{stage.icon}</div>
              <div className="stage-name">{stage.name}</div>
              <div className="stage-detail">{stage.detail}</div>
            </div>
            {i < STAGES.length - 1 && <div className="pipeline-arrow">→</div>}
          </Fragment>
        ))}
      </div>
    </section>
  );
}
