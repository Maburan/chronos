import { useCallback, useEffect, useRef, useState } from "react";
import { getWorkflow, getWorkflowStatus, listWorkflowTasks, type Task, type Workflow, type WorkflowStatus } from "./api";
import { describeError, type DisplayError } from "./errors";
import { Header } from "./components/Header";
import { WorkflowHeader } from "./components/WorkflowHeader";
import { MetricCards } from "./components/MetricCards";
import { PipelineVisual } from "./components/PipelineVisual";
import { TaskCreatePanel } from "./components/TaskCreatePanel";
import { TaskList } from "./components/TaskList";
import { TaskDetailPanel } from "./components/TaskDetailPanel";
import { DependencyGraph } from "./components/DependencyGraph";
import { ErrorBanner } from "./components/ErrorBanner";

const LAST_WORKFLOW_ID_KEY = "chronos.lastWorkflowId";
const POLL_INTERVAL_MS = 2000;
const CHANGE_FLASH_MS = 1400;

/** Snapshot of the fields whose change we consider worth flashing a row
 * for — state, attempt_count, lease_owner. Timestamps alone (updated_at
 * ticking without anything else changing) never happens server-side, so
 * comparing these three is sufficient and avoids over-triggering. */
function fingerprint(t: Task): string {
  return `${t.state}|${t.attempt_count}|${t.lease_owner ?? ""}`;
}

function App() {
  const [workflow, setWorkflow] = useState<Workflow | null>(null);
  const [status, setStatus] = useState<WorkflowStatus | null>(null);
  const [tasks, setTasks] = useState<Task[]>([]);
  // Client-side-only memory of which depends_on_ids each task was created
  // with. The backend never returns this (no endpoint reads
  // task_dependencies back out — confirmed by reading api/routes.py), so
  // this map only knows about tasks created in THIS browser session.
  const [dependencyEdges, setDependencyEdges] = useState<Map<number, number[]>>(new Map());
  const [loadError, setLoadError] = useState<DisplayError | null>(null);
  const [polling, setPolling] = useState(true);
  const [lastPolledAt, setLastPolledAt] = useState<Date | null>(null);
  const [selectedTask, setSelectedTask] = useState<Task | null>(null);
  const [changedTaskIds, setChangedTaskIds] = useState<Set<number>>(new Set());

  const prevFingerprints = useRef<Map<number, string>>(new Map());
  const flashTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const applyTasks = useCallback((next: Task[]) => {
    const prev = prevFingerprints.current;
    const changed = new Set<number>();
    for (const t of next) {
      const fp = fingerprint(t);
      if (prev.has(t.id) && prev.get(t.id) !== fp) changed.add(t.id);
    }
    prevFingerprints.current = new Map(next.map((t) => [t.id, fingerprint(t)]));
    setTasks(next);

    // Keep the open detail panel (if any) showing the freshest row for
    // the same task, rather than a stale snapshot from when it was
    // clicked.
    setSelectedTask((sel) => {
      if (!sel) return sel;
      return next.find((t) => t.id === sel.id) ?? sel;
    });

    if (changed.size > 0) {
      setChangedTaskIds(changed);
      if (flashTimer.current) clearTimeout(flashTimer.current);
      flashTimer.current = setTimeout(() => setChangedTaskIds(new Set()), CHANGE_FLASH_MS);
    }
  }, []);

  const refreshTasks = useCallback(
    (workflowId: number) => {
      listWorkflowTasks(workflowId)
        .then(applyTasks)
        .catch((err) => setLoadError(describeError(err)));
    },
    [applyTasks]
  );

  const refreshAll = useCallback(
    (workflowId: number) => {
      refreshTasks(workflowId);
      getWorkflowStatus(workflowId)
        .then(setStatus)
        .catch((err) => setLoadError(describeError(err)));
      setLastPolledAt(new Date());
    },
    [refreshTasks]
  );

  // Pure convenience: remember the last-used workflow id so a page reload
  // doesn't force retyping it. This is NOT a workflow list — the backend
  // has no such endpoint — it's one id, client-side only.
  useEffect(() => {
    const savedId = localStorage.getItem(LAST_WORKFLOW_ID_KEY);
    if (!savedId) return;
    getWorkflow(Number(savedId))
      .then(setWorkflow)
      .catch(() => localStorage.removeItem(LAST_WORKFLOW_ID_KEY));
  }, []);

  useEffect(() => {
    if (!workflow) {
      setStatus(null);
      setTasks([]);
      setDependencyEdges(new Map());
      setSelectedTask(null);
      prevFingerprints.current = new Map();
      return;
    }
    localStorage.setItem(LAST_WORKFLOW_ID_KEY, String(workflow.id));
    prevFingerprints.current = new Map();
    refreshAll(workflow.id);
    setDependencyEdges(new Map());
    setSelectedTask(null);
  }, [workflow, refreshAll]);

  // The only place Chronos state changes become visible without an
  // explicit user action. There is no websocket/push mechanism anywhere
  // in the API — polling GET /workflows/{id}/tasks and .../status is the
  // sole way a browser can observe what the outbox/relay/worker/reaper
  // pipeline did in the background.
  useEffect(() => {
    if (!workflow || !polling) return;
    const id = setInterval(() => refreshAll(workflow.id), POLL_INTERVAL_MS);
    return () => clearInterval(id);
  }, [workflow, polling, refreshAll]);

  function handleTaskCreated(task: Task, dependsOnIds: number[]) {
    if (dependsOnIds.length > 0) {
      setDependencyEdges((prev) => new Map(prev).set(task.id, dependsOnIds));
    }
    if (workflow) refreshAll(workflow.id);
  }

  return (
    <main className="app">
      <Header workflow={workflow} onRefresh={() => workflow && refreshAll(workflow.id)} />

      <div className="stack">
        <WorkflowHeader workflow={workflow} onWorkflowChange={setWorkflow} />

        {workflow && (
          <div className="poll-bar">
            <label className="poll-toggle">
              <input type="checkbox" checked={polling} onChange={(e) => setPolling(e.target.checked)} />
              Auto-refresh every {POLL_INTERVAL_MS / 1000}s
            </label>
            <button type="button" className="ghost small" onClick={() => refreshAll(workflow.id)}>
              Refresh now
            </button>
            {lastPolledAt && <span className="faint">last updated {lastPolledAt.toLocaleTimeString()}</span>}
          </div>
        )}

        <MetricCards status={status} />

        <PipelineVisual status={status} />

        {workflow && (
          <TaskCreatePanel workflowId={workflow.id} existingTasks={tasks} onTaskCreated={handleTaskCreated} />
        )}

        {workflow && (
          <TaskList
            tasks={tasks}
            selectedTaskId={selectedTask?.id ?? null}
            changedTaskIds={changedTaskIds}
            onSelectTask={setSelectedTask}
          />
        )}

        {workflow && <DependencyGraph tasks={tasks} dependencyEdges={dependencyEdges} />}

        {loadError && <ErrorBanner error={loadError} onDismiss={() => setLoadError(null)} />}
      </div>

      {selectedTask && <TaskDetailPanel task={selectedTask} onClose={() => setSelectedTask(null)} />}
    </main>
  );
}

export default App;
