import type { ReactNode } from "react";
import { Navigate, useLocation } from "react-router-dom";
import { useAuth } from "../auth/AuthContext";

/** Redirects to /login (remembering the attempted path) unless a user is
 * authenticated. Waits for the initial GET /auth/me check (AuthContext's
 * `loading`) before deciding, so a page reload with a valid stored token
 * doesn't flash a redirect to /login before the token's been verified. */
export function ProtectedRoute({ children }: { children: ReactNode }) {
  const { user, loading } = useAuth();
  const location = useLocation();

  if (loading) {
    return (
      <div className="auth-wrap">
        <div className="empty">
          <div className="empty-icon">
            <span className="health-dot checking" style={{ width: 10, height: 10 }} />
          </div>
          <div className="empty-title">Loading Chronos…</div>
        </div>
      </div>
    );
  }

  if (!user) {
    return <Navigate to="/login" state={{ from: location.pathname }} replace />;
  }

  return <>{children}</>;
}
