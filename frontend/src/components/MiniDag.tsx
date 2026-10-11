import type { Task } from "../api";
import { topoOrder, edgeActivity } from "../execution";
import { CheckIcon, XIcon, SkipIcon } from "./icons";

/**
 * A compact execution strip for a workflow card: the run's tasks in
 * dependency order as state dots, linked by connectors that light up as
 * work flows through them. Purely a reading of real task states — a
 * finished run shows a settled strip, a live one animates the active link.
 */
export function MiniDag({ tasks, limit = 8 }: { tasks: Task[]; limit?: number }) {
  const ordered = topoOrder(tasks).slice(0, limit);

  return (
    <div className="minidag" aria-hidden="true">
      {ordered.map((t, i) => {
        const prev = i > 0 ? ordered[i - 1] : null;
        const linkClass = prev ? edgeActivity(prev.state, t.state) : "idle";
        return (
          <span key={t.id} style={{ display: "flex", alignItems: "center" }}>
            {i > 0 && <span className={`minidag-link ${linkClass}`} />}
            <span className="minidag-node" data-state={t.state} title={`${t.name}: ${t.state}`}>
              {t.state === "SUCCEEDED" && <CheckIcon />}
              {t.state === "FAILED_TERMINAL" && <XIcon />}
              {t.state === "SKIPPED" && <SkipIcon />}
            </span>
          </span>
        );
      })}
    </div>
  );
}
