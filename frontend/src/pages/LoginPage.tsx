import { useState } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { useAuth } from "../auth/AuthContext";
import { describeError } from "../errors";
import { ErrorBanner } from "../components/ErrorBanner";

export function LoginPage() {
  const { login } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ReturnType<typeof describeError> | null>(null);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await login(email.trim(), password);
      const redirectTo = (location.state as { from?: string } | null)?.from ?? "/";
      navigate(redirectTo, { replace: true });
    } catch (err) {
      setError(describeError(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="auth-wrap">
      <section className="card auth-card">
        <div className="brand">
          <div className="brand-mark">C</div>
          <span className="brand-word">Chronos</span>
        </div>
        <div className="auth-head">
          <h1>Welcome back</h1>
          <p>Sign in to orchestrate your workflows.</p>
        </div>
        <form onSubmit={handleSubmit}>
          <div className="field">
            <span className="field-label">Email</span>
            <input
              type="email"
              autoComplete="username"
              placeholder="you@company.com"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              required
            />
          </div>
          <div className="field">
            <span className="field-label">Password</span>
            <input
              type="password"
              autoComplete="current-password"
              placeholder="••••••••"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
            />
          </div>
          <button type="submit" className="btn primary lg block" disabled={busy || !email.trim() || !password}>
            {busy ? "Signing in…" : "Sign in"}
          </button>
        </form>
        {error && (
          <div style={{ marginTop: "var(--s-4)" }}>
            <ErrorBanner error={error} onDismiss={() => setError(null)} />
          </div>
        )}
        <p className="auth-switch">
          New to Chronos? <Link to="/register">Create an account</Link>
        </p>
      </section>
    </div>
  );
}
