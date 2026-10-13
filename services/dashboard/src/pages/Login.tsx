import { useRef, useState, type FormEvent } from "react";
import { Navigate, useLocation, useNavigate } from "react-router-dom";

import { useAuth } from "../auth";
import { ErrorBanner, useAction } from "../ui";

export default function LoginPage() {
  const { user, login } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const from = (location.state as { from?: string } | null)?.from ?? "/servers";
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const { busy, error, run } = useAction();
  const passwordRef = useRef<HTMLInputElement>(null);

  if (user) return <Navigate to={from} replace />;

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const ok = await run(async () => {
      await login(email, password);
      return true;
    });
    if (ok) {
      navigate(from, { replace: true });
      return;
    }
    // Failed attempt: keep the email, clear the password and ask for it again.
    setPassword("");
    passwordRef.current?.focus();
  };

  return (
    <div className="auth-page">
      <form className="auth-card" onSubmit={submit}>
        <div className="brand">
          <div className="brand-mark">U</div>
          <span>Urumi MCP Gateway</span>
        </div>
        <h1>Sign in</h1>
        <p className="muted">Use the account your admin invited you with.</p>
        <ErrorBanner message={error} />
        <div className="field">
          <label htmlFor="email">Email</label>
          <input id="email" type="email" autoComplete="username" required value={email} onChange={(e) => setEmail(e.target.value)} />
        </div>
        <div className="field">
          <label htmlFor="password">Password</label>
          <input id="password" ref={passwordRef} type="password" autoComplete="current-password" required value={password} onChange={(e) => setPassword(e.target.value)} />
        </div>
        <button className="button button-primary" disabled={busy}>{busy ? "Signing in…" : "Sign in"}</button>
      </form>
    </div>
  );
}
