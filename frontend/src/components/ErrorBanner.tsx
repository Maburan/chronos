import type { DisplayError } from "../errors";

export function ErrorBanner({ error, onDismiss }: { error: DisplayError; onDismiss?: () => void }) {
  return (
    <div className="error-banner" role="alert">
      <div style={{ flex: 1 }}>
        <div className="error-title">{error.title}</div>
        <div className="muted">{error.detail}</div>
      </div>
      {onDismiss && (
        <button type="button" className="ghost small" onClick={onDismiss}>
          Dismiss
        </button>
      )}
    </div>
  );
}
