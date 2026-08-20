import { ApiError } from "./api";

/**
 * Turns a caught error (from any api.ts call) into a short, human title +
 * detail pair. Every component that catches an API error routes it
 * through here instead of doing its own ad-hoc string formatting, so
 * "workflow not found" / "can't reach the API" / "task creation failed"
 * read consistently everywhere they appear (ErrorBanner, inline form
 * errors). Never swallows an error — always returns something to show.
 */
export interface DisplayError {
  title: string;
  detail: string;
}

export function describeError(err: unknown): DisplayError {
  if (err instanceof ApiError) {
    if (err.status === 404) {
      return { title: "Not found", detail: typeof err.detail === "object" ? JSON.stringify(err.detail) : String(err.detail) };
    }
    if (err.status === 400) {
      return {
        title: "Request rejected",
        detail:
          typeof err.detail === "object" && err.detail !== null && "detail" in (err.detail as object)
            ? String((err.detail as { detail: unknown }).detail)
            : JSON.stringify(err.detail),
      };
    }
    if (err.status >= 500) {
      return { title: "Backend error", detail: `Chronos API returned ${err.status}` };
    }
    return { title: `API error ${err.status}`, detail: JSON.stringify(err.detail) };
  }
  if (err instanceof TypeError) {
    // fetch() throws a bare TypeError ("Failed to fetch") when the
    // network request itself never completed — the API process is down,
    // unreachable, or CORS rejected it outright.
    return {
      title: "Cannot reach Chronos API",
      detail: "The API may be down, or CORS is blocking this origin. Check that uvicorn is running and reachable.",
    };
  }
  return { title: "Unexpected error", detail: err instanceof Error ? err.message : String(err) };
}
