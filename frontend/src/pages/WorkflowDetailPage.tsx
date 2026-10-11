import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import {
  ApiError,
  createWorkflowRun,
  getRunStatus,
  getWorkflow,
  getWorkflowDefinition,
  listRunTasks,
  listWorkflowRuns,
  type RunStatus,
  type Task,
  type Workflow,
  type WorkflowDefinition,
  type WorkflowRun,
} from "../api";
import { describeError, type DisplayError } from "../errors";
import { ExecutionGraph } from "../components/execution/ExecutionGraph";
import { TaskInspectorContent } from "../components/TaskInspectorContent";
import { ErrorBanner } from "../components/ErrorBanner";
import { useAdvancedMode } from "../advancedMode";
import { useToast } from "../components/toast";
import { deriveWorkflowStatus, statusMeterSegments } from "../workflowStatus";
import { PlayIcon, PencilIcon, RefreshIcon, SparkIcon, InboxIcon, ArrowLeftIcon } from "../components/icons";

const POLL_INTERVAL_MS = 2000;
const CHANGE_FLASH_MS = 1400;

function fingerprint(t: Task): string {
  return `${t.state}|${t.attempt_count}|${t.lease_owner ?? ""}`;
}

function formatTime(iso: string): string {
  return new Date(iso).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

function formatElapsed(ms: number): string {
  if (ms < 0) ms = 0;
  const s = ms / 1000;
  if (s < 60) return `${s.toFixed(1)}s`;
  const m = Math.floor(s / 60);
  return `${m}m ${String(Math.floor(s % 60)).padStart(2, "0")}s`;
}

/**
 * The workflow's home, composed as an immersive canvas: the run's
 * dependency graph fills the viewport and the page's chrome floats over
 * it. "Run workflow" materializes the current definition into a fresh set
 * of tasks (POST /workflows/{id}/runs) and live-polls that run — the graph
 * animates as work moves through it. Selecting a node floats a contextual
 * inspector over the canvas without hiding the graph.
 */
export function WorkflowDetailPage() {
  const { id } = useParams<{ id: string }>();
  const workflowId = Number(id);
  const { advanced } = useAdvancedMode();
  const toast = useToast();

  const [workflow, setWorkflow] = useState<Workflow | null>(null);
  const [definition, setDefinition] = useState<WorkflowDefinition | null>(null);
  const [runs, setRuns] = useState<WorkflowRun[] | null>(null);
  const [selectedRunId, setSelectedRunId] = useState<number | null>(null);
  const [runStatus, setRunStatus] = useState<RunStatus | null>(null);
  const [runTasks, setRunTasks] = useState<Task[]>([]);

  const [notFound, setNotFound] = useState(false);
  const [loadError, setLoadError] = useState<DisplayError | null>(null);
  const [starting, setStarting] = useState(false);
  const [polling, setPolling] = useState(true);
  const [selectedTask, setSelectedTask] = useState<Task | null>(null);
  const [nowTick, setNowTick] = useState(() => Date.now());

  const prevFingerprints = useRef<Map<number, string>>(new Map());
  const flashTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  // changed-task flashing is retained for future use by the graph; unused counter avoided

  useEffect(() => {
    if (!Number.isFinite(workflowId)) return;
    setNotFound(false);
    setWorkflow(null);
    setDefinition(null);
    setRuns(null);
    setSelectedRunId(null);
    setRunStatus(null);
    setRunTasks([]);
    setSelectedTask(null);
    prevFingerprints.current = new Map();

    getWorkflow(workflowId)
      .then(setWorkflow)
      .catch((err) => {
        if (err instanceof ApiError && err.status === 404) setNotFound(true);
        else setLoadError(describeError(err));
      });
    getWorkflowDefinition(workflowId).then(setDefinition).catch(() => setDefinition({ workflow_id: workflowId, tasks: [] }));
    listWorkflowRuns(workflowId)
      .then((rs) => {
        const sorted = [...rs].sort((a, b) => b.id - a.id);
        setRuns(sorted);
        setSelectedRunId(sorted[0]?.id ?? null);
      })
      .catch((err) => setLoadError(describeError(err)));
  }, [workflowId]);

  const applyTasks = useCallback((next: Task[]) => {
    prevFingerprints.current = new Map(next.map((t) => [t.id, fingerprint(t)]));
    setRunTasks(next);
    setSelectedTask((sel) => (sel ? next.find((t) => t.id === sel.id) ?? sel : sel));
    if (flashTimer.current) clearTimeout(flashTimer.current);
    flashTimer.current = setTimeout(() => {}, CHANGE_FLASH_MS);
  }, []);

  const refreshRun = useCallback(
    (runId: number) => {
      getRunStatus(workflowId, runId).then(setRunStatus).catch((err) => setLoadError(describeError(err)));
      listRunTasks(workflowId, runId).then(applyTasks).catch((err) => setLoadError(describeError(err)));
    },
    [workflowId, applyTasks]
  );

  useEffect(() => {
    if (selectedRunId == null) return;
    prevFingerprints.current = new Map();
    refreshRun(selectedRunId);
  }, [selectedRunId, refreshRun]);

  useEffect(() => {
    if (selectedRunId == null || !polling || runStatus?.is_complete) return;
    const timer = setInterval(() => refreshRun(selectedRunId), POLL_INTERVAL_MS);
    return () => clearInterval(timer);
  }, [selectedRunId, polling, runStatus?.is_complete, refreshRun]);

  // per-second elapsed ticker, only while a run is actually in progress
  useEffect(() => {
    if (runStatus?.is_complete || selectedRunId == null) return;
    const t = setInterval(() => setNowTick(Date.now()), 1000);
    return () => clearInterval(t);
  }, [runStatus?.is_complete, selectedRunId]);

  async function handleRun() {
    setStarting(true);
    setLoadError(null);
    try {
      const run = await createWorkflowRun(workflowId);
      const updated = await listWorkflowRuns(workflowId);
      setRuns([...updated].sort((a, b) => b.id - a.id));
      setSelectedRunId(run.id);
      setSelectedTask(null);
      setPolling(true);
      toast.success("Run started");
    } catch (err) {
      const e = describeError(err);
      setLoadError(e);
      toast.error(e.title);
    } finally {
      setStarting(false);
    }
  }

  const stepCount = definition?.tasks.length ?? 0;
  const isDraft = definition != null && stepCount === 0;
  const selectedRun = runs?.find((r) => r.id === selectedRunId) ?? null;
  const display = deriveWorkflowStatus(runStatus);
  const segments = runStatus && runStatus.total_tasks > 0 ? statusMeterSegments(runStatus) : [];
  const settled = runStatus ? runStatus.succeeded + runStatus.failed_terminal + runStatus.skipped : 0;

  const elapsed = useMemo(() => {
    if (!selectedRun) return null;
    const started = new Date(selectedRun.created_at).getTime();
    if (runStatus?.is_complete && runTasks.length > 0) {
      const finished = Math.max(...runTasks.map((t) => new Date(t.updated_at).getTime()));
      return formatElapsed(finished - started);
    }
    if (!runStatus?.is_complete) return formatElapsed(nowTick - started);
    return null;
  }, [selectedRun, runStatus?.is_complete, runTasks, nowTick]);

  if (notFound) {
    return (
      <div className="canvas-page">
        <div className="canvas-prompt">
          <div className="float canvas-prompt-card">
            <div className="empty-icon">
              <InboxIcon />
            </div>
            <div className="empty-title">Workflow not found</div>
            <p className="empty-detail">It may belong to another account, or no longer exists.</p>
            <Link to="/workflows" className="empty-action">
              <button type="button" className="btn">
                <ArrowLeftIcon /> Back to workflows
              </button>
            </Link>
          </div>
        </div>
      </div>
    );
  }

  const hasRun = !!runs && runs.length > 0 && runTasks.length > 0;

  return (
    <div className="canvas-page">
      {/* the canvas itself — the page */}
      {hasRun ? (
        <ExecutionGraph key={selectedRunId} tasks={runTasks} onSelectTask={setSelectedTask} />
      ) : (
        <>
          <div className="exec-graph" />
          <div className="canvas-prompt">
            <div className="float canvas-prompt-card">
              <div className="empty-icon">{isDraft ? <SparkIcon /> : <PlayIcon />}</div>
              <div className="empty-title">{isDraft ? "No steps yet" : runs === null ? "Loading…" : "Ready to run"}</div>
              <p className="empty-detail">
                {isDraft
                  ? "Design this workflow by adding and connecting steps in the builder, then run it."
                  : "This workflow hasn't been run yet. Start a run to materialize its steps and watch them execute in real time."}
              </p>
              <div className="empty-action">
                {isDraft ? (
                  <Link to={`/workflows/${workflowId}/builder`}>
                    <button type="button" className="btn primary">
                      <PencilIcon /> Open builder
                    </button>
                  </Link>
                ) : (
                  runs !== null && (
                    <button type="button" className="btn primary" onClick={handleRun} disabled={starting}>
                      <PlayIcon /> {starting ? "Starting…" : "Run workflow"}
                    </button>
                  )
                )}
              </div>
            </div>
          </div>
        </>
      )}

      {/* floating context header */}
      <div className="canvas-header">
        <div className="float canvas-crumb">
          <div className="crumb-path">
            <Link to="/workflows">Workflows</Link>
            <span className="sep">/</span>
            {advanced && workflow && <span className="mono faint">#{workflow.id}</span>}
          </div>
          <div className="crumb-title">{workflow?.name ?? "…"}</div>
          <div className="canvas-status">
            <span className={`live-status`} style={{ color: `var(--c-${display.tone === "completed" ? "succeeded" : display.tone === "failed" ? "failed" : display.tone === "running" ? "running" : "pending"})` }}>
              <span className={`status-orb ${display.tone}`} />
              {display.label}
            </span>
            {runStatus && runStatus.total_tasks > 0 && (
              <>
                <span className="sep">·</span>
                <span className="tnum">
                  {settled}/{runStatus.total_tasks} tasks
                </span>
              </>
            )}
            {elapsed && (
              <>
                <span className="sep">·</span>
                <span className="tnum">{elapsed}</span>
              </>
            )}
          </div>
          {segments.length > 0 && (
            <div className="progress-rail">
              {segments.map((s, i) => (
                <span key={i} className={`seg ${s.kind}`} style={{ width: `${s.pct}%` }} />
              ))}
            </div>
          )}
        </div>

        <div className="canvas-actions">
          <button
            type="button"
            className={`btn sm${polling ? " " : ""}`}
            onClick={() => setPolling((p) => !p)}
            title={polling ? "Live updates on" : "Live updates paused"}
          >
            <span className={`status-orb ${polling && !runStatus?.is_complete ? "running" : "draft"}`} style={{ width: 7, height: 7 }} />
            Live
          </button>
          <Link to={`/workflows/${workflowId}/builder`}>
            <button type="button" className="btn sm">
              <PencilIcon /> Edit
            </button>
          </Link>
          <button
            type="button"
            className="btn primary sm"
            onClick={handleRun}
            disabled={starting || isDraft}
            title={isDraft ? "Add steps in the builder before running" : "Run this workflow now"}
          >
            <PlayIcon /> {starting ? "Starting…" : "Run"}
          </button>
        </div>
      </div>

      {/* floating run selector */}
      {runs && runs.length > 0 && (
        <div className="float canvas-runbar">
          {runs.length > 1 ? (
            <select
              className="input run-select"
              value={selectedRunId ?? undefined}
              onChange={(e) => {
                setSelectedRunId(Number(e.target.value));
                setSelectedTask(null);
              }}
              aria-label="Select a run"
            >
              {runs.map((r, i) => (
                <option key={r.id} value={r.id}>
                  {i === 0 ? "Latest · " : ""}Run #{r.id} · {formatTime(r.created_at)}
                </option>
              ))}
            </select>
          ) : (
            selectedRun && <span className="run-id mono" style={{ fontSize: "var(--fs-12)", color: "var(--text-2)" }}>Run #{selectedRun.id}</span>
          )}
          <button
            type="button"
            className="btn ghost icon sm"
            aria-label="Refresh"
            title="Refresh"
            onClick={() => selectedRunId != null && refreshRun(selectedRunId)}
          >
            <RefreshIcon />
          </button>
        </div>
      )}

      {/* floating contextual inspector */}
      {selectedTask && (
        <div className="float canvas-inspector">
          <TaskInspectorContent task={selectedTask} onClose={() => setSelectedTask(null)} />
        </div>
      )}

      {loadError && (
        <div style={{ position: "absolute", bottom: 16, left: "50%", transform: "translateX(-50%)", zIndex: 20, width: "min(520px, calc(100% - 32px))" }}>
          <div className="float" style={{ padding: 2 }}>
            <ErrorBanner error={loadError} onDismiss={() => setLoadError(null)} />
          </div>
        </div>
      )}
    </div>
  );
}

