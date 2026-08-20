import type { Task } from "../api";
import { StateBadge } from "./StateBadge";
import { EmptyState } from "./EmptyState";

interface TaskListProps {
  tasks: Task[];
  selectedTaskId: number | null;
  changedTaskIds: Set<number>;
  onSelectTask: (task: Task) => void;
}

function formatTime(iso: string | null): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleTimeString();
}

/**
 * Renders GET /workflows/{id}/tasks verbatim, one row per task, sorted
 * by id. No derived state beyond formatting and the attempt-count dots.
 * `changedTaskIds` (computed in App.tsx by diffing consecutive polls) is
 * the only thing driving the brief `.flash` highlight — the row itself
 * never fabricates a transition it didn't actually observe between two
 * real polls.
 */
export function TaskList({ tasks, selectedTaskId, changedTaskIds, onSelectTask }: TaskListProps) {
  if (tasks.length === 0) {
    return (
      <section className="card">
        <div className="card-title">
          <h2>Tasks</h2>
        </div>
        <EmptyState icon="▢" title="No tasks yet" detail="Create your first task above and watch Chronos execute it." />
      </section>
    );
  }

  const sorted = [...tasks].sort((a, b) => a.id - b.id);

  return (
    <section className="card">
      <div className="card-title">
        <h2>Tasks</h2>
        <span className="hint">click a row for details</span>
      </div>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>id</th>
              <th>name</th>
              <th>type</th>
              <th>state</th>
              <th>attempts</th>
              <th>lease</th>
              <th>updated</th>
            </tr>
          </thead>
          <tbody>
            {sorted.map((t) => (
              <tr
                key={t.id}
                className={[
                  t.id === selectedTaskId ? "selected" : "",
                  changedTaskIds.has(t.id) ? "flash" : "",
                ]
                  .filter(Boolean)
                  .join(" ")}
                onClick={() => onSelectTask(t)}
              >
                <td className="faint">#{t.id}</td>
                <td>{t.name}</td>
                <td className="muted">{t.task_type}</td>
                <td>
                  <StateBadge state={t.state} />
                </td>
                <td>
                  {t.attempt_count} / {t.max_attempts}
                  <span className="attempt-bar">
                    {Array.from({ length: t.max_attempts }).map((_, i) => (
                      <span key={i} className={i < t.attempt_count ? "used" : ""} />
                    ))}
                  </span>
                </td>
                <td className="muted">{t.lease_owner ?? "—"}</td>
                <td className="muted">{formatTime(t.updated_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
