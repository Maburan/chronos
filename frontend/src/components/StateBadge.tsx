import type { TaskState } from "../api";
import { STATE_BADGE_CLASS, STATE_ICON, STATE_LABEL } from "../stateStyle";
import { useAdvancedMode } from "../advancedMode";

/**
 * Renders one TaskState as a color-coded badge. Color is never the only
 * signal -- an icon plus a plain-language label always accompanies it
 * (matters for colorblind users, and for anyone who doesn't know the
 * backend's enum vocabulary). The raw enum is always available via the
 * native `title` tooltip, and shown inline too once Advanced mode is on.
 */
export function StateBadge({ state }: { state: TaskState }) {
  const { advanced } = useAdvancedMode();
  return (
    <span className={`badge ${STATE_BADGE_CLASS[state]}`} title={state}>
      <span aria-hidden="true">{STATE_ICON[state]}</span>
      {STATE_LABEL[state]}
      {advanced && <span className="faint"> ({state})</span>}
    </span>
  );
}
