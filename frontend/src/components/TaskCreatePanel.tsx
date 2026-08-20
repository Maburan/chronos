import { useState } from "react";
import { createTask, type Task } from "../api";
import { describeError } from "../errors";
import { ErrorBanner } from "./ErrorBanner";

interface TaskCreatePanelProps {
  workflowId: number;
  existingTasks: Task[];
  onTaskCreated: (task: Task, dependsOnIds: number[]) => void;
}

const DEFAULT_PAYLOAD = '{\n  "message": "hello"\n}';

const RETRY_DEMO_PAYLOAD = JSON.stringify(
  { message: "retry demo", fail_until_attempt: 3 },
  null,
  2
);

/**
 * POST /workflows/{id}/tasks. Zero dependencies -> the task is created
 * READY and gets an outbox row in the same transaction (dispatchable
 * almost immediately). One or more dependencies -> PENDING, and it only
 * becomes READY once every dependency reaches SUCCEEDED (see
 * db/persistence.py::_propagate_ready) — nothing dispatches it yet.
 *
 * depends_on_ids is NOT readable back from the API afterwards (no
 * endpoint returns a task's dependency edges) — the caller is
 * responsible for remembering what was submitted, for the dependency
 * graph view (see App.tsx's `dependencyEdges` state).
 */
export function TaskCreatePanel({ workflowId, existingTasks, onTaskCreated }: TaskCreatePanelProps) {
  const [name, setName] = useState("");
  const [taskType, setTaskType] = useState("echo");
  const [maxAttempts, setMaxAttempts] = useState(1);
  const [payloadText, setPayloadText] = useState("");
  const [dependsOn, setDependsOn] = useState<number[]>([]);
  const [error, setError] = useState<ReturnType<typeof describeError> | null>(null);
  const [busy, setBusy] = useState(false);
  const [retryDemoArmed, setRetryDemoArmed] = useState(false);

  function toggleDependency(id: number) {
    setDependsOn((prev) => (prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]));
  }

  function fillRetryDemo() {
    setTaskType("echo");
    setMaxAttempts(5);
    setPayloadText(RETRY_DEMO_PAYLOAD);
    setRetryDemoArmed(true);
    if (!name.trim()) setName("retry-demo");
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!name.trim() || !taskType.trim()) return;

    let payload: Record<string, unknown> | null = null;
    if (payloadText.trim()) {
      try {
        payload = JSON.parse(payloadText);
      } catch {
        setError({ title: "Invalid payload", detail: "Payload must be valid JSON, or left empty." });
        return;
      }
    }

    setBusy(true);
    setError(null);
    try {
      const task = await createTask(workflowId, {
        name: name.trim(),
        task_type: taskType.trim(),
        max_attempts: maxAttempts,
        payload,
        depends_on_ids: dependsOn.length > 0 ? dependsOn : null,
      });
      onTaskCreated(task, dependsOn);
      setName("");
      setPayloadText("");
      setDependsOn([]);
      setRetryDemoArmed(false);
    } catch (err) {
      setError(describeError(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="card">
      <div className="card-title">
        <h2>Create task</h2>
        <button type="button" className="ghost small" onClick={fillRetryDemo}>
          ⟳ Fill retry demo
        </button>
      </div>
      {retryDemoArmed && (
        <p className="card-sub">
          Payload set to fail on attempts 1–2 and succeed on attempt 3 (<code>fail_until_attempt: 3</code>,{" "}
          <code>max_attempts: 5</code>). Nothing runs until you click Create — watch{" "}
          <code>attempt_count</code> climb 1 → 2 → 3 in the task list afterward.
        </p>
      )}

      <form onSubmit={handleSubmit} className="task-form">
        <div className="field-row">
          <label>
            Name
            <input value={name} onChange={(e) => setName(e.target.value)} placeholder="fetch-data" />
          </label>
          <label>
            Task type
            <input value={taskType} onChange={(e) => setTaskType(e.target.value)} />
          </label>
          <label style={{ maxWidth: 140 }}>
            Max attempts
            <input
              type="number"
              min={1}
              value={maxAttempts}
              onChange={(e) => setMaxAttempts(Math.max(1, Number(e.target.value) || 1))}
            />
          </label>
        </div>

        <label>
          <div className="payload-toolbar">
            <span>Payload (JSON, optional)</span>
          </div>
          <textarea
            rows={4}
            value={payloadText}
            onChange={(e) => {
              setPayloadText(e.target.value);
              setRetryDemoArmed(false);
            }}
            placeholder={DEFAULT_PAYLOAD}
            spellCheck={false}
          />
        </label>
        <p className="faint" style={{ margin: "-0.6rem 0 0", fontSize: "0.72rem" }}>
          demo-only knobs for <code>task_type: echo</code> (see worker/demo_task.py):{" "}
          <code>fail_until_attempt</code>, <code>sleep_seconds</code>, <code>sleep_until_attempt</code>
        </p>

        {existingTasks.length > 0 && (
          <fieldset>
            <legend>Depends on — task stays PENDING until all of these SUCCEED</legend>
            <div className="dep-list">
              {existingTasks.map((t) => (
                <label key={t.id} className="dep-checkbox">
                  <input
                    type="checkbox"
                    checked={dependsOn.includes(t.id)}
                    onChange={() => toggleDependency(t.id)}
                  />
                  #{t.id} {t.name} <span className="faint">({t.state})</span>
                </label>
              ))}
            </div>
          </fieldset>
        )}

        <div>
          <button type="submit" className="primary" disabled={busy || !name.trim()}>
            Create task
          </button>
        </div>
      </form>
      {error && <div style={{ marginTop: "var(--space-4)" }}><ErrorBanner error={error} onDismiss={() => setError(null)} /></div>}
    </section>
  );
}
