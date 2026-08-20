import type { TaskState } from "../api";
import { STATE_BADGE_CLASS } from "../stateStyle";

/** Renders one TaskState as a color-coded badge. Used anywhere a task's
 * state needs to be shown — the badge itself has no state, purely a
 * presentational read of the value passed in. */
export function StateBadge({ state }: { state: TaskState }) {
  return <span className={`badge ${STATE_BADGE_CLASS[state]}`}>{state}</span>;
}
