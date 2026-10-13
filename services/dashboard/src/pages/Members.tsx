import { useState, type FormEvent } from "react";

import { api, type Invite, type Role, type User } from "../api";
import { useCurrentUser } from "../auth";
import {
  ConfirmDialog, CopyButton, Empty, ErrorBanner, PageHeader, RoleBadge, formatTime, timeAgo, useAction, useLoad,
} from "../ui";

export default function MembersPage() {
  const me = useCurrentUser();
  const members = useLoad(api.members);
  const invites = useLoad(api.invites);
  const [email, setEmail] = useState("");
  const [role, setRole] = useState<Role>("member");
  const [created, setCreated] = useState<Invite | null>(null);
  const [disabling, setDisabling] = useState<User | null>(null);
  const inviteAction = useAction();
  const memberAction = useAction();

  const invite = async (e: FormEvent) => {
    e.preventDefault();
    const result = await inviteAction.run(() => api.createInvite(email, role));
    if (result) {
      setCreated(result);
      setEmail("");
      void invites.reload();
    }
  };

  const update = (user: User, patch: Partial<Pick<User, "role" | "status">>) =>
    memberAction.run(async () => {
      const updated = await api.updateMember(user.id, patch);
      members.setData((list) => list?.map((m) => (m.id === updated.id ? { ...updated, active_keys: m.active_keys } : m)));
    });

  return (
    <>
      <PageHeader title="Members" subtitle="Invite teammates and manage their roles. Disabling someone blocks their session and all their API keys at once." />

      <div className="card">
        <div className="card-body">
          <h2 style={{ marginBottom: 12 }}>Invite a teammate</h2>
          <ErrorBanner message={inviteAction.error} />
          <form className="inline-form" onSubmit={invite}>
            <div className="field" style={{ flex: "1 1 240px" }}>
              <label htmlFor="invite-email">Email</label>
              <input id="invite-email" type="email" required value={email} onChange={(e) => setEmail(e.target.value)} />
            </div>
            <div className="field">
              <label htmlFor="invite-role">Role</label>
              <select id="invite-role" value={role} onChange={(e) => setRole(e.target.value as Role)}>
                <option value="member">member</option>
                <option value="admin">admin</option>
              </select>
            </div>
            <button className="button button-primary" disabled={inviteAction.busy}>Create invite link</button>
          </form>
          {created?.link && (
            <div style={{ marginTop: 14 }}>
              <div className="banner banner-warn">
                Send this link to <strong>{created.email}</strong> yourself. It works once, expires {formatTime(created.expires_at)},
                and can't be shown again.
              </div>
              <div className="secret-box">
                <code>{created.link}</code>
                <CopyButton text={created.link} />
              </div>
            </div>
          )}
        </div>
      </div>

      {(invites.data?.length ?? 0) > 0 && (
        <div className="card">
          <div className="card-body"><h2>Pending invites</h2></div>
          <div className="table-wrap">
            <table>
              <thead><tr><th>Email</th><th>Role</th><th>Expires</th><th /></tr></thead>
              <tbody>
                {invites.data?.map((i) => (
                  <tr key={i.id}>
                    <td>{i.email}</td>
                    <td><RoleBadge role={i.role} /></td>
                    <td className="muted">{formatTime(i.expires_at)}</td>
                    <td>
                      <div className="row-actions">
                        <button
                          className="button button-small button-danger"
                          onClick={() => void inviteAction.run(async () => { await api.cancelInvite(i.id); await invites.reload(); })}
                        >
                          Cancel
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      <div className="card">
        <ErrorBanner message={members.error ?? memberAction.error} />
        <div className="table-wrap">
          <table>
            <thead>
              <tr><th>Member</th><th>Role</th><th>Status</th><th>API keys</th><th>Last sign-in</th><th /></tr>
            </thead>
            <tbody>
              {members.data?.map((m) => {
                const self = m.id === me.id;
                return (
                  <tr key={m.id}>
                    <td>
                      <div className="cell-main">{m.name || "—"}{self && <span className="muted"> (you)</span>}</div>
                      <div className="cell-sub">{m.email}</div>
                    </td>
                    <td>
                      <select
                        value={m.role}
                        disabled={self || memberAction.busy}
                        onChange={(e) => void update(m, { role: e.target.value as Role })}
                        aria-label={`Role of ${m.email}`}
                        style={{ maxWidth: 130 }}
                      >
                        <option value="member">member</option>
                        <option value="admin">admin</option>
                      </select>
                    </td>
                    <td>{m.status === "active" ? <span className="badge badge-green">active</span> : <span className="badge badge-gray">disabled</span>}</td>
                    <td>{m.active_keys}</td>
                    <td className="muted nowrap">{timeAgo(m.last_login_at)}</td>
                    <td>
                      <div className="row-actions">
                        {!self && m.status === "active" && (
                          <button className="button button-small button-danger" onClick={() => setDisabling(m)}>Disable</button>
                        )}
                        {!self && m.status === "disabled" && (
                          <button className="button button-small" disabled={memberAction.busy} onClick={() => void update(m, { status: "active" })}>Enable</button>
                        )}
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          {!members.data && !members.error && <Empty>Loading…</Empty>}
        </div>
      </div>

      {disabling && (
        <ConfirmDialog
          title={`Disable ${disabling.email}?`}
          message="Their dashboard session and every API key they own stop working on the next request. You can enable them again later."
          confirmLabel="Disable member"
          danger
          onClose={() => setDisabling(null)}
          onConfirm={async () => {
            const updated = await api.updateMember(disabling.id, { status: "disabled" });
            members.setData((list) => list?.map((m) => (m.id === updated.id ? { ...updated, active_keys: m.active_keys } : m)));
          }}
        />
      )}
    </>
  );
}
