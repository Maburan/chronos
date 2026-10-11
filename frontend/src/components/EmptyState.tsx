import type { ReactNode } from "react";
import { InboxIcon } from "./icons";

interface EmptyStateProps {
  icon?: ReactNode;
  title: string;
  detail?: string;
  action?: ReactNode;
}

export function EmptyState({ icon, title, detail, action }: EmptyStateProps) {
  return (
    <div className="empty">
      <div className="empty-icon" aria-hidden="true">
        {icon ?? <InboxIcon />}
      </div>
      <div className="empty-title">{title}</div>
      {detail && <p className="empty-detail">{detail}</p>}
      {action && <div className="empty-action">{action}</div>}
    </div>
  );
}
