import { useEffect, useState } from "react";
import { getReadyz } from "../api";
import type { Workflow } from "../api";

type HealthState = "checking" | "ok" | "down";

const HEALTH_POLL_MS = 5000;

/**
 * Chronos branding + a real backend readiness indicator + a manual
 * refresh trigger + the currently-loaded workflow. Polls GET /readyz
 * (not /healthz) on its own timer, independent of the main task/status
 * poll — readiness means "Postgres is reachable," the one dependency the
 * API itself has (see api/health.py), so this is the honest thing to
 * show as "is Chronos up," not a fabricated multi-service status.
 */
export function Header({
  workflow,
  onRefresh,
}: {
  workflow: Workflow | null;
  onRefresh: () => void;
}) {
  const [health, setHealth] = useState<HealthState>("checking");

  useEffect(() => {
    let cancelled = false;
    function check() {
      getReadyz()
        .then(() => {
          if (!cancelled) setHealth("ok");
        })
        .catch(() => {
          if (!cancelled) setHealth("down");
        });
    }
    check();
    const id = setInterval(check, HEALTH_POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, []);

  return (
    <header className="top-header">
      <div className="brand">
        <div className="brand-mark">C</div>
        <div className="brand-text">
          <h1>Chronos</h1>
          <p>Distributed Task Orchestration</p>
        </div>
      </div>

      <div className="header-right">
        {workflow && (
          <span className="current-workflow-chip">
            #{workflow.id} {workflow.name}
          </span>
        )}
        <span className="health-pill" title="GET /readyz — Postgres reachability">
          <span className={`health-dot ${health}`} />
          {health === "checking" && "checking API…"}
          {health === "ok" && "API ready"}
          {health === "down" && "API unreachable"}
        </span>
        <button type="button" className="ghost small" onClick={onRefresh}>
          ↻ Refresh
        </button>
      </div>
    </header>
  );
}
