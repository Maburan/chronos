import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { listWorkflows } from "../api";
import { describeError, type DisplayError } from "../errors";
import { ErrorBanner } from "../components/ErrorBanner";
import { EmptyState } from "../components/EmptyState";
import { Skeleton } from "../components/Skeleton";
import { MiniDag } from "../components/MiniDag";
import { PipelineHero, type SystemPulse } from "../components/PipelineHero";
import { PlusIcon, SparkIcon, ChevronRightIcon } from "../components/icons";
import { fetchWorkflowOverviews, type WorkflowOverview } from "../workflowOverview";

const RECENT_COUNT = 8;
const POLL_MS = 2500;

/**
 * The orchestration overview. It leads with a live pipeline hero driven by
 * real aggregate task state, then spotlights whatever is genuinely running
 * and lists everything else. It polls on an interval so the hero and the
 * spotlights stay live; every dot, strip, and count is a plain reading of
 * the API — if nothing is running, nothing pretends to be.
 */
export function DashboardPage() {
  const [items, setItems] = useState<WorkflowOverview[] | null>(null);
  const [error, setError] = useState<DisplayError | null>(null);
  const loaded = useRef(false);

  useEffect(() => {
    let cancelled = false;
    const load = () =>
      listWorkflows()
        .then(fetchWorkflowOverviews)
        .then((overviews) => {
          if (cancelled) return;
          setItems(overviews);
          loaded.current = true;
          setError(null);
        })
        .catch((err) => {
          // Only surface an error on the first load; a transient poll
          // failure shouldn't blow away a working view.
          if (!cancelled && !loaded.current) setError(describeError(err));
        });
    load();
    const id = setInterval(load, POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, []);

  if (error) {
    return (
      <div className="container">
        <ErrorBanner error={error} />
      </div>
    );
  }

  if (items === null) {
    return (
      <div className="container dash">
        <div className="hero glass hero-skeleton">
          <Skeleton height="1.9rem" width="60%" />
          <div style={{ marginTop: 12 }}>
            <Skeleton height="0.9rem" width="40%" />
          </div>
          <div style={{ marginTop: 28 }}>
            <Skeleton height="96px" />
          </div>
        </div>
        <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
          <Skeleton height="46px" />
          <Skeleton height="46px" />
          <Skeleton height="46px" />
        </div>
      </div>
    );
  }

  const pulse = aggregatePulse(items);
  const active = items.filter((i) => i.display.tone === "running");
  const recent = [...items].sort(
    (a, b) => new Date(b.workflow.created_at).getTime() - new Date(a.workflow.created_at).getTime()
  );

  return (
    <div className="container dash">
      <PipelineHero pulse={pulse} />

      {items.length === 0 ? (
        <EmptyState
          icon={<SparkIcon />}
          title="Build your first workflow"
          detail="A workflow is a sequence of steps Chronos runs, retries, and recovers for you — automatically."
          action={
            <Link to="/workflows/new">
              <button type="button" className="btn primary">
                <PlusIcon /> New workflow
              </button>
            </Link>
          }
        />
      ) : (
        <>
          {active.length > 0 && (
            <div className="dash-section">
              <div className="section-head">
                Active
                <span className="line" />
                <span className="count">{active.length}</span>
              </div>
              {active.map((o) => {
                const runningTask = o.tasks.find((t) => t.state === "RUNNING");
                return (
                  <Link key={o.workflow.id} to={`/workflows/${o.workflow.id}`} className="spotlight">
                    <div className="spotlight-main">
                      <div className="spotlight-name">{o.workflow.name}</div>
                      <MiniDag tasks={o.tasks} limit={14} />
                      <div className="spotlight-sub">
                        {runningTask ? (
                          <>
                            <strong>{runningTask.name}</strong> is running
                          </>
                        ) : (
                          <>
                            {o.counts.settled}/{o.counts.total} tasks complete
                          </>
                        )}
                      </div>
                    </div>
                    <div className="spotlight-side">
                      <span className="spotlight-pct tnum">{o.counts.percent}%</span>
                      <span className="badge badge-running">
                        <span className="dot" />
                        Running
                      </span>
                    </div>
                  </Link>
                );
              })}
            </div>
          )}

          <div className="dash-section">
            <div className="section-head">
              Recent
              <span className="line" />
            </div>
            <div className="recent">
              {recent.slice(0, RECENT_COUNT).map((o) => (
                <Link key={o.workflow.id} to={`/workflows/${o.workflow.id}`} className="recent-row">
                  <span className="r-name">{o.workflow.name}</span>
                  <span className="r-strip">{o.tasks.length > 0 && <MiniDag tasks={o.tasks} limit={12} />}</span>
                  <span className="r-status">
                    <span className={`status-orb ${o.display.tone}`} />
                    {o.display.label}
                  </span>
                  <ChevronRightIcon className="list-row-chevron" width={16} height={16} />
                </Link>
              ))}
            </div>
          </div>
        </>
      )}
    </div>
  );
}

/** Sum real per-workflow task counts into one system-wide live pulse. */
function aggregatePulse(items: WorkflowOverview[]): SystemPulse {
  const p: SystemPulse = {
    running: 0,
    ready: 0,
    pending: 0,
    succeeded: 0,
    failed: 0,
    skipped: 0,
    total: 0,
    workflowCount: items.length,
  };
  for (const o of items) {
    p.running += o.counts.running;
    p.ready += o.counts.ready;
    p.pending += o.counts.pending;
    p.succeeded += o.counts.succeeded;
    p.failed += o.counts.failed_terminal;
    p.skipped += o.counts.skipped;
    p.total += o.counts.total;
  }
  return p;
}
