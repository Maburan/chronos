import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { createWorkflow } from "../api";
import { describeError, type DisplayError } from "../errors";
import { ErrorBanner } from "../components/ErrorBanner";
import { ArrowLeftIcon } from "../components/icons";

/**
 * Creates the workflow (POST /workflows) then hands off straight to its
 * builder, where steps and connections are added to the *definition*
 * (PUT /workflows/{id}/definition). Saving a definition never creates a
 * dispatchable task on its own — running it (materializing the definition
 * into an executable set of tasks) is a separate, explicit action on the
 * workflow page.
 */
export function WorkflowNewPage() {
  const navigate = useNavigate();
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<DisplayError | null>(null);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!name.trim()) return;
    setBusy(true);
    setError(null);
    try {
      const wf = await createWorkflow(name.trim());
      navigate(`/workflows/${wf.id}/builder`);
    } catch (err) {
      setError(describeError(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="container stack">
      <Link to="/workflows" className="back-link">
        <ArrowLeftIcon /> Back to workflows
      </Link>
      <div className="page-head">
        <div className="page-title-group">
          <span className="eyebrow">New</span>
          <h1>Create a workflow</h1>
        </div>
      </div>

      <section className="card" style={{ maxWidth: 520 }}>
        <form onSubmit={handleSubmit} className="stack-sm">
          <div className="field">
            <span className="field-label">Workflow name</span>
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="data-processing-pipeline"
              autoFocus
            />
            <span className="field-help">
              You'll design the steps and their connections on the next screen.
            </span>
          </div>
          <div style={{ marginTop: "var(--s-2)" }}>
            <button type="submit" className="btn primary" disabled={busy || !name.trim()}>
              {busy ? "Creating…" : "Create & open builder"}
            </button>
          </div>
        </form>
        {error && (
          <div style={{ marginTop: "var(--s-4)" }}>
            <ErrorBanner error={error} onDismiss={() => setError(null)} />
          </div>
        )}
      </section>
    </div>
  );
}
