import { useState, type FormEvent } from "react";
import { useNavigate, useParams } from "react-router-dom";

import { api } from "../api";
import { useAuth } from "../auth";
import { ErrorBanner, RoleBadge, formatTime, useAction, useLoad } from "../ui";

const MIN_PASSWORD = 10;

export default function AcceptInvitePage() {
  const { token = "" } = useParams();
  const { setUser } = useAuth();
  const navigate = useNavigate();
  const invite = useLoad(() => api.viewInvite(token));
  const [name, setName] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const { busy, error, setError, run } = useAction();

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (password !== confirm) {
      setError("The two passwords don't match.");
      return;
    }
    await run(async () => {
      setUser(await api.acceptInvite(token, name, password));
      navigate("/connect", { replace: true });
    });
  };

  return (
    <div className="auth-page">
      <form className="auth-card" onSubmit={submit}>
        <div className="brand">
          <div className="brand-mark">U</div>
          <span>Urumi MCP Gateway</span>
        </div>
        <h1>Join the team</h1>
        {invite.error && <ErrorBanner message={invite.error} />}
        {!invite.data && !invite.error && <p className="muted">Checking your invite…</p>}
        {invite.data && (
          <>
            <p className="muted">
              You're invited as <strong>{invite.data.email}</strong> <RoleBadge role={invite.data.role} />. This link
              expires {formatTime(invite.data.expires_at)}.
            </p>
            <ErrorBanner message={error} />
            <div className="field">
              <label htmlFor="name">Your name</label>
              <input id="name" required autoComplete="name" value={name} onChange={(e) => setName(e.target.value)} />
            </div>
            <div className="field">
              <label htmlFor="password">Password</label>
              <input id="password" type="password" required minLength={MIN_PASSWORD} autoComplete="new-password" value={password} onChange={(e) => setPassword(e.target.value)} />
              <span className="hint">At least {MIN_PASSWORD} characters.</span>
            </div>
            <div className="field">
              <label htmlFor="confirm">Confirm password</label>
              <input id="confirm" type="password" required autoComplete="new-password" value={confirm} onChange={(e) => setConfirm(e.target.value)} />
            </div>
            <button className="button button-primary" disabled={busy}>{busy ? "Creating account…" : "Create account"}</button>
          </>
        )}
      </form>
    </div>
  );
}
