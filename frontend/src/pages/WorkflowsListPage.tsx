import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { listWorkflows } from "../api";
import { describeError, type DisplayError } from "../errors";
import { ErrorBanner } from "../components/ErrorBanner";
import { EmptyState } from "../components/EmptyState";
import { Skeleton } from "../components/Skeleton";
import { MiniDag } from "../components/MiniDag";
import { PlusIcon, SparkIcon, SearchIcon, ChevronRightIcon, LayersIcon } from "../components/icons";
import { fetchWorkflowOverviews, type WorkflowOverview } from "../workflowOverview";

const SEARCH_THRESHOLD = 6;

function formatDate(iso: string): string {
  return new Date(iso).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
}

export function WorkflowsListPage() {
  const [items, setItems] = useState<WorkflowOverview[] | null>(null);
  const [error, setError] = useState<DisplayError | null>(null);
  const [query, setQuery] = useState("");

  useEffect(() => {
    let cancelled = false;
    listWorkflows()
      .then(fetchWorkflowOverviews)
      .then((overviews) => !cancelled && setItems(overviews))
      .catch((err) => !cancelled && setError(describeError(err)));
    return () => {
      cancelled = true;
    };
  }, []);

  const sorted = useMemo(
    () =>
      items
        ? [...items].sort(
            (a, b) => new Date(b.workflow.created_at).getTime() - new Date(a.workflow.created_at).getTime()
          )
        : [],
    [items]
  );

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return sorted;
    return sorted.filter((i) => i.workflow.name.toLowerCase().includes(q));
  }, [sorted, query]);

  const showSearch = (items?.length ?? 0) > SEARCH_THRESHOLD;

  return (
    <div className="container stack">
      <div className="page-head">
        <div className="page-title-group">
          <span className="eyebrow">Library</span>
          <h1>Workflows</h1>
        </div>
        <Link to="/workflows/new" className="page-actions">
          <button type="button" className="btn primary">
            <PlusIcon /> New workflow
          </button>
        </Link>
      </div>

      {error && <ErrorBanner error={error} onDismiss={() => setError(null)} />}

      {items === null ? (
        <div className="list gap">
          {Array.from({ length: 4 }).map((_, i) => (
            <Skeleton key={i} height="66px" />
          ))}
        </div>
      ) : items.length === 0 ? (
        <section className="card">
          <EmptyState
            icon={<SparkIcon />}
            title="No workflows yet"
            detail="Create your first workflow to start automating a sequence of steps."
            action={
              <Link to="/workflows/new">
                <button type="button" className="btn primary">
                  <PlusIcon /> New workflow
                </button>
              </Link>
            }
          />
        </section>
      ) : (
        <>
          {showSearch && (
            <div className="picker-search" style={{ maxWidth: 320 }}>
              <SearchIcon />
              <input
                type="search"
                placeholder="Filter workflows…"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                aria-label="Filter workflows by name"
              />
            </div>
          )}

          {filtered.length === 0 ? (
            <section className="card">
              <EmptyState title={`No workflows match “${query}”`} detail="Try a different search." />
            </section>
          ) : (
            <div className="list gap">
              {filtered.map((o) => (
                <Link key={o.workflow.id} to={`/workflows/${o.workflow.id}`} className="list-row">
                  <span className="list-row-icon">
                    <LayersIcon width={16} height={16} />
                  </span>
                  <div className="list-row-main">
                    <span className="list-row-title">{o.workflow.name}</span>
                    {o.tasks.length > 0 ? (
                      <div className="minidag-meta">
                        <MiniDag tasks={o.tasks} />
                        <span className="tnum">
                          {o.counts.settled}/{o.counts.total}
                        </span>
                        <span>· {o.counts.percent}%</span>
                        <span>· {formatDate(o.workflow.created_at)}</span>
                      </div>
                    ) : (
                      <span className="list-row-meta">No runs yet · Created {formatDate(o.workflow.created_at)}</span>
                    )}
                  </div>
                  <span className={`badge ${o.display.badgeClass}`}>
                    {o.display.tone === "running" && <span className="dot" />}
                    {o.display.label}
                  </span>
                  <ChevronRightIcon className="list-row-chevron" width={16} height={16} />
                </Link>
              ))}
            </div>
          )}
        </>
      )}
    </div>
  );
}
