import { useEffect, useState } from "react";
import { Link, NavLink } from "react-router-dom";
import { getReadyz } from "../api";
import { useAuth } from "../auth/AuthContext";
import { AdvancedModeToggle } from "../advancedMode";
import { ThemeToggle } from "../theme";
import { HomeIcon, LayersIcon, LogOutIcon } from "./icons";

type HealthState = "checking" | "ok" | "down";
const HEALTH_POLL_MS = 5000;

const HEALTH_COPY: Record<HealthState, string> = {
  checking: "Checking",
  ok: "Operational",
  down: "Unreachable",
};

/**
 * The single application bar: brand, primary nav, and the account area,
 * all on one compact 56px row (replacing the old two-row Header+NavBar).
 *
 * The health pill polls GET /readyz (Postgres reachability — the one
 * dependency the API itself has, per api/health.py), so it reports the
 * honest "is Chronos ready to work" signal, never a fabricated
 * multi-service status. Nav/account only render for an authenticated
 * user; logged-out (auth pages) shows just the brand + health.
 */
export function TopBar() {
  const { user, logout } = useAuth();
  const [health, setHealth] = useState<HealthState>("checking");

  useEffect(() => {
    let cancelled = false;
    const check = () =>
      getReadyz()
        .then(() => !cancelled && setHealth("ok"))
        .catch(() => !cancelled && setHealth("down"));
    check();
    const id = setInterval(check, HEALTH_POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, []);

  return (
    <header className="topbar">
      <div className="topbar-inner">
        <Link to="/" className="brand" aria-label="Chronos home">
          <div className="brand-mark">C</div>
          <span className="brand-word">Chronos</span>
        </Link>

        {user && (
          <nav className="topbar-nav" aria-label="Primary">
            <NavLink to="/" end className={({ isActive }) => (isActive ? "nav-link active" : "nav-link")}>
              <HomeIcon />
              Dashboard
            </NavLink>
            <NavLink to="/workflows" className={({ isActive }) => (isActive ? "nav-link active" : "nav-link")}>
              <LayersIcon />
              Workflows
            </NavLink>
          </nav>
        )}

        <div className="topbar-right">
          <span className="health" title="GET /readyz — Postgres reachability">
            <span className={`health-dot ${health}`} />
            {HEALTH_COPY[health]}
          </span>

          <ThemeToggle />

          {user ? (
            <>
              <span className="topbar-divider" />
              <AdvancedModeToggle />
              <span className="topbar-divider" />
              <span className="user-chip" title={user.email}>
                <span className="user-avatar">{user.email.slice(0, 1)}</span>
                <span className="user-email">{user.email}</span>
              </span>
              <button type="button" className="btn ghost icon sm" aria-label="Log out" title="Log out" onClick={() => void logout()}>
                <LogOutIcon />
              </button>
            </>
          ) : (
            <>
              <span className="topbar-divider" />
              <Link to="/login" className="nav-link">
                Sign in
              </Link>
              <Link to="/register">
                <button type="button" className="btn primary sm">
                  Get started
                </button>
              </Link>
            </>
          )}
        </div>
      </div>
    </header>
  );
}
