interface EmptyStateProps {
  icon?: string;
  title: string;
  detail?: string;
}

export function EmptyState({ icon = "○", title, detail }: EmptyStateProps) {
  return (
    <div className="empty-state">
      <div className="empty-icon">{icon}</div>
      <div className="empty-title">{title}</div>
      {detail && <div>{detail}</div>}
    </div>
  );
}
