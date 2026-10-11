export function Skeleton({ height = "1rem", width = "100%" }: { height?: string; width?: string }) {
  return <div className="skeleton" style={{ height, width }} aria-hidden="true" />;
}

export function SkeletonCard() {
  return (
    <div className="card">
      <Skeleton height="1.1rem" width="40%" />
      <div style={{ marginTop: "var(--space-4)", display: "flex", flexDirection: "column", gap: "0.6rem" }}>
        <Skeleton height="0.9rem" />
        <Skeleton height="0.9rem" width="80%" />
        <Skeleton height="0.9rem" width="60%" />
      </div>
    </div>
  );
}
