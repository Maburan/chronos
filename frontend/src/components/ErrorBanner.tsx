import type { DisplayError } from "../errors";
import { AlertIcon, XIcon } from "./icons";

export function ErrorBanner({ error, onDismiss }: { error: DisplayError; onDismiss?: () => void }) {
  return (
    <div className="banner" role="alert">
      <span className="banner-icon">
        <AlertIcon />
      </span>
      <div className="grow">
        <div className="banner-title">{error.title}</div>
        <div className="banner-detail">{error.detail}</div>
      </div>
      {onDismiss && (
        <button type="button" className="btn ghost icon sm" aria-label="Dismiss" onClick={onDismiss}>
          <XIcon />
        </button>
      )}
    </div>
  );
}
