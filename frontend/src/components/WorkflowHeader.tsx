import { useState } from "react";
import { createWorkflow, getWorkflow, type Workflow } from "../api";
import { describeError } from "../errors";
import { ErrorBanner } from "./ErrorBanner";

interface WorkflowHeaderProps {
  workflow: Workflow | null;
  onWorkflowChange: (workflow: Workflow) => void;
}

/**
 * Create a new workflow (POST /workflows) or load an existing one by id
 * (GET /workflows/{id}). There is no "list workflows" endpoint on the
 * backend today (confirmed by reading api/routes.py) — loading by a
 * known id is the only way back into a workflow created earlier. This
 * component does not pretend otherwise; the parent only remembers the
 * *last-used* id, as a plain convenience, in localStorage.
 */
export function WorkflowHeader({ workflow, onWorkflowChange }: WorkflowHeaderProps) {
  const [name, setName] = useState("");
  const [loadId, setLoadId] = useState("");
  const [error, setError] = useState<ReturnType<typeof describeError> | null>(null);
  const [busy, setBusy] = useState(false);

  async function handleCreate(e: React.FormEvent) {
    e.preventDefault();
    if (!name.trim()) return;
    setBusy(true);
    setError(null);
    try {
      const wf = await createWorkflow(name.trim());
      onWorkflowChange(wf);
      setName("");
    } catch (err) {
      setError(describeError(err));
    } finally {
      setBusy(false);
    }
  }

  async function handleLoad(e: React.FormEvent) {
    e.preventDefault();
    const id = Number(loadId);
    if (!Number.isInteger(id) || id <= 0) {
      setError({ title: "Invalid id", detail: "Workflow id must be a positive integer." });
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const wf = await getWorkflow(id);
      onWorkflowChange(wf);
    } catch (err) {
      setError(describeError(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="card">
      <div className="card-title">
        <h2>Workflow</h2>
        {workflow ? (
          <span className="muted" style={{ fontSize: "0.82rem" }}>
            #{workflow.id} · created {new Date(workflow.created_at).toLocaleString()}
          </span>
        ) : (
          <span className="hint">No workflow selected</span>
        )}
      </div>

      <div className="field-row">
        <form onSubmit={handleCreate} className="field-row" style={{ flex: 2 }}>
          <label style={{ flex: 1 }}>
            New workflow name
            <input value={name} onChange={(e) => setName(e.target.value)} placeholder="demo-workflow" />
          </label>
          <button type="submit" className="primary" disabled={busy || !name.trim()} style={{ alignSelf: "flex-end" }}>
            Create
          </button>
        </form>

        <div style={{ width: 1, alignSelf: "stretch", background: "var(--border)" }} />

        <form onSubmit={handleLoad} className="field-row" style={{ flex: 1 }}>
          <label style={{ flex: 1 }}>
            Load by id
            <input value={loadId} onChange={(e) => setLoadId(e.target.value)} placeholder="123" inputMode="numeric" />
          </label>
          <button type="submit" disabled={busy || !loadId.trim()} style={{ alignSelf: "flex-end" }}>
            Load
          </button>
        </form>
      </div>

      {error && <div style={{ marginTop: "var(--space-4)" }}><ErrorBanner error={error} onDismiss={() => setError(null)} /></div>}
    </section>
  );
}
