import { useState, type FormEvent } from "react";
import { Link } from "react-router-dom";

import { api, type ApiKey } from "../api";
import { useCurrentUser } from "../auth";
import { ConfirmDialog, CopyButton, Empty, ErrorBanner, Modal, PageHeader, formatTime, timeAgo, useAction, useLoad } from "../ui";

export default function KeysPage() {
  const user = useCurrentUser();
  const isAdmin = user.role === "admin";
  const keys = useLoad(api.keys);
  const [name, setName] = useState("");
  const [newKey, setNewKey] = useState<string | null>(null);
  const [revoking, setRevoking] = useState<ApiKey | null>(null);
  const [showRevoked, setShowRevoked] = useState(false);
  const create = useAction();

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const result = await create.run(() => api.createKey(name.trim()));
    if (result) {
      setNewKey(result.key);
      setName("");
      void keys.reload();
    }
  };

  const visible = (keys.data ?? []).filter((k) => showRevoked || !k.revoked_at);

  return (
    <>
      <PageHeader
        title="API keys"
        subtitle={isAdmin ? "Keys for MCP clients. As admin you see and can revoke everyone's keys." : "Keys for your MCP clients (Claude Code, Cursor, Inspector…)."}
      />
      <div className="card">
        <div className="card-body">
          <h2 style={{ marginBottom: 12 }}>Create a key</h2>
          <ErrorBanner message={create.error} />
          <form className="inline-form" onSubmit={submit}>
            <div className="field" style={{ flex: "1 1 260px" }}>
              <label htmlFor="key-name">Name</label>
              <input id="key-name" required maxLength={100} placeholder="e.g. laptop – Claude Code" value={name} onChange={(e) => setName(e.target.value)} />
            </div>
            <button className="button button-primary" disabled={create.busy}>Create key</button>
          </form>
        </div>
      </div>

      <div className="card">
        <div className="filters">
          <label className="small" style={{ display: "flex", gap: 6, alignItems: "center" }}>
            <input type="checkbox" checked={showRevoked} onChange={(e) => setShowRevoked(e.target.checked)} style={{ width: "auto" }} />
            Show revoked keys
          </label>
        </div>
        <ErrorBanner message={keys.error} />
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Name</th>
                {isAdmin && <th>Owner</th>}
                <th>Key</th>
                <th>Created</th>
                <th>Last used</th>
                <th>Status</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {visible.map((k) => (
                <tr key={k.id}>
                  <td className="cell-main">{k.name}</td>
                  {isAdmin && <td>{k.user_email}</td>}
                  <td className="mono">{k.prefix}…</td>
                  <td className="muted nowrap">{formatTime(k.created_at)}</td>
                  <td className="muted nowrap">{timeAgo(k.last_used_at)}</td>
                  <td>{k.revoked_at ? <span className="badge badge-gray">revoked</span> : <span className="badge badge-green">active</span>}</td>
                  <td>
                    <div className="row-actions">
                      {!k.revoked_at && <button className="button button-small button-danger" onClick={() => setRevoking(k)}>Revoke</button>}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {keys.data && visible.length === 0 && <Empty>No active keys. Create one above to connect a client.</Empty>}
        </div>
      </div>

      {newKey && (
        <Modal title="Your new API key" onClose={() => setNewKey(null)}>
          <div className="banner banner-warn">Copy it now. It is stored only as a hash and can't be shown again.</div>
          <div className="secret-box">
            <code>{newKey}</code>
            <CopyButton text={newKey} />
          </div>
          <p className="muted small">
            Next: <Link to="/connect" onClick={() => setNewKey(null)}>connect a client</Link> with this key.
          </p>
          <div className="modal-actions">
            <button className="button button-primary" onClick={() => setNewKey(null)}>I've saved it</button>
          </div>
        </Modal>
      )}
      {revoking && (
        <ConfirmDialog
          title={`Revoke "${revoking.name}"?`}
          message={<>Clients using <span className="mono">{revoking.prefix}…</span>{isAdmin && revoking.user_email !== user.email ? ` (${revoking.user_email})` : ""} get 401 on their very next request. This can't be undone.</>}
          confirmLabel="Revoke key"
          danger
          onClose={() => setRevoking(null)}
          onConfirm={async () => {
            await api.revokeKey(revoking.id);
            await keys.reload();
          }}
        />
      )}
    </>
  );
}
