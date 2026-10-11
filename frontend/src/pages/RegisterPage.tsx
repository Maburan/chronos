import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useAuth } from "../auth/AuthContext";
import { describeError } from "../errors";
import { ErrorBanner } from "../components/ErrorBanner";

const MIN_PASSWORD_LENGTH = 8;

export function RegisterPage() {
  const { register } = useAuth();
  const navigate = useNavigate();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ReturnType<typeof describeError> | null>(null);

  const passwordTooShort = password.length > 0 && password.length < MIN_PASSWORD_LENGTH;

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (password.length < MIN_PASSWORD_LENGTH) return;
    setBusy(true);
    setError(null);
    try {
      await register(email.trim(), password);
      navigate("/", { replace: true });
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
          <h1>Create your account</h1>
          <p>Start building and running workflows in minutes.</p>
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
              autoComplete="new-password"
              placeholder="At least 8 characters"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
            />
            {passwordTooShort && (
              <span className="field-help" style={{ color: "var(--c-failed)" }}>
                Password must be at least {MIN_PASSWORD_LENGTH} characters.
              </span>
            )}
          </div>
          <button
            type="submit"
            className="btn primary lg block"
            disabled={busy || !email.trim() || password.length < MIN_PASSWORD_LENGTH}
          >
            {busy ? "Creating account…" : "Create account"}
          </button>
        </form>
        {error && (
          <div style={{ marginTop: "var(--s-4)" }}>
            <ErrorBanner error={error} onDismiss={() => setError(null)} />
          </div>
        )}
        <p className="auth-switch">
          Already have an account? <Link to="/login">Sign in</Link>
        </p>
      </section>
    </div>
  );
}
