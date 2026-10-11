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

/** FastAPI's error bodies come in two shapes: a plain `{detail: "..."}`
 * (raised HTTPException) and a validation `{detail: [{loc, msg, type}]}`
 * (422). This pulls a short, human sentence out of either without ever
 * dumping raw JSON at the user. */
function extractDetail(detail: unknown): string | null {
  if (typeof detail === "string") return detail;
  if (detail && typeof detail === "object" && "detail" in detail) {
    const inner = (detail as { detail: unknown }).detail;
    if (typeof inner === "string") return inner;
    if (Array.isArray(inner) && inner.length > 0) {
      const first = inner[0] as { msg?: string };
      if (first?.msg) return first.msg.replace(/^Value error,\s*/i, "");
    }
  }
  return null;
}

export function describeError(err: unknown): DisplayError {
  if (err instanceof ApiError) {
    if (err.status === 401) {
      return { title: "Signed out", detail: "Your session has expired — please sign in again." };
    }
    if (err.status === 404) {
      return { title: "Not found", detail: extractDetail(err.detail) ?? "That resource doesn't exist." };
    }
    if (err.status === 400 || err.status === 422) {
      return { title: "Couldn't complete that", detail: extractDetail(err.detail) ?? "The request was rejected." };
    }
    if (err.status >= 500) {
      return { title: "Something went wrong", detail: `Chronos ran into a server error (${err.status}). Please try again.` };
    }
    return { title: "Request failed", detail: extractDetail(err.detail) ?? `The API returned ${err.status}.` };
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
