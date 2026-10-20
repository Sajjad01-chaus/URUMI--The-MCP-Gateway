import { useState, type FormEvent } from "react";

import { api, type Server, type ServerInput } from "../api";
import { useCurrentUser } from "../auth";
import {
  ConfirmDialog, Empty, ErrorBanner, Modal, PageHeader, RoleBadge, StatusBadge, timeAgo, useAction, useEscape, useLoad,
} from "../ui";

const SLUG_RE = /^[a-z0-9]+(-[a-z0-9]+)*$/;

export default function ServersPage() {
  const user = useCurrentUser();
  const isAdmin = user.role === "admin";
  // Polls so health changes (a killed or recovered upstream) show up without a manual refresh.
  const servers = useLoad(api.servers, 10_000);
  const [selected, setSelected] = useState<Server | null>(null);
  const [editing, setEditing] = useState<Server | "new" | null>(null);
  const [deleting, setDeleting] = useState<Server | null>(null);
  const action = useAction();

  const replace = (updated: Server) =>
    servers.setData((list) => list?.map((s) => (s.id === updated.id ? updated : s)));

  const toggle = (server: Server) =>
    action.run(async () => replace(await api.updateServer(server.id, { enabled: !server.enabled })));

  const test = (server: Server) => action.run(async () => replace(await api.testServer(server.id)));

  return (
    <>
      <PageHeader
        title="Servers"
        subtitle="Upstream MCP servers behind the gateway. Their tools are exposed as <slug>__<tool>."
        actions={isAdmin && <button className="button button-primary" onClick={() => setEditing("new")}>Add server</button>}
      />
      <ErrorBanner message={servers.error ?? action.error} />
      <div className="card">
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Server</th>
                <th>Status</th>
                <th>Tools</th>
                <th>Last check</th>
                {isAdmin && <th>Enabled</th>}
                {isAdmin && <th />}
              </tr>
            </thead>
            <tbody>
              {servers.data?.map((s) => (
                <tr key={s.id} className="clickable" onClick={() => setSelected(s)}>
                  <td>
                    <div className="cell-main">
                      {s.name} <span className="chip">{s.slug}</span>{" "}
                      {s.is_managed && <span className="badge badge-blue">managed</span>}
                    </div>
                    <div className="cell-sub">{s.url}</div>
                  </td>
                  <td>
                    <StatusBadge status={s.status} />
                    {s.last_error && s.status !== "connected" && <div className="cell-sub">{s.last_error}</div>}
                  </td>
                  <td>{s.tools.length}</td>
                  <td className="nowrap muted">{timeAgo(s.last_checked_at)}</td>
                  {isAdmin && (
                    <td onClick={(e) => e.stopPropagation()}>
                      <label className="toggle" title={s.enabled ? "Disable" : "Enable"}>
                        <input type="checkbox" checked={s.enabled} disabled={action.busy} onChange={() => void toggle(s)} aria-label={`Enable ${s.slug}`} />
                        <span />
                      </label>
                    </td>
                  )}
                  {isAdmin && (
                    <td onClick={(e) => e.stopPropagation()}>
                      <div className="row-actions">
                        <button className="button button-small" disabled={action.busy} onClick={() => void test(s)}>Test now</button>
                        {!s.is_managed && <button className="button button-small" onClick={() => setEditing(s)}>Edit</button>}
                        {!s.is_managed && <button className="button button-small button-danger" onClick={() => setDeleting(s)}>Delete</button>}
                      </div>
                    </td>
                  )}
                </tr>
              ))}
            </tbody>
          </table>
          {servers.data?.length === 0 && <Empty>No servers registered yet.</Empty>}
          {!servers.data && !servers.error && <Empty>Loading…</Empty>}
        </div>
      </div>

      {selected && (
        <ServerDrawer server={servers.data?.find((s) => s.id === selected.id) ?? selected} onClose={() => setSelected(null)} />
      )}
      {editing && (
        <ServerForm
          server={editing === "new" ? null : editing}
          onClose={() => setEditing(null)}
          onSaved={() => {
            setEditing(null);
            void servers.reload();
          }}
        />
      )}
      {deleting && (
        <ConfirmDialog
          title={`Delete ${deleting.name}?`}
          message={<>Its <strong>{deleting.tools.length}</strong> tools disappear for everyone and connected clients are told to re-list. Audit history is kept.</>}
          confirmLabel="Delete server"
          danger
          onClose={() => setDeleting(null)}
          onConfirm={async () => {
            await api.deleteServer(deleting.id);
            await servers.reload();
          }}
        />
      )}
    </>
  );
}

function ServerDrawer({ server, onClose }: { server: Server; onClose: () => void }) {
  useEscape(onClose);
  return (
    <>
      <div className="drawer-backdrop" onClick={onClose} />
      <aside className="drawer" aria-label={`${server.name} details`}>
        <div className="modal-header">
          <h2>{server.name}</h2>
          <button className="icon-button" onClick={onClose} aria-label="Close">×</button>
        </div>
        <dl className="key-value">
          <dt>Slug</dt><dd className="mono">{server.slug}</dd>
          <dt>Status</dt><dd><StatusBadge status={server.status} /></dd>
          {server.last_error && <><dt>Last error</dt><dd>{server.last_error}</dd></>}
          <dt>URL</dt><dd className="mono">{server.url}</dd>
          <dt>Auth</dt>
          <dd>
            {server.auth_type === "none" ? "none" : server.auth_type === "bearer" ? "Bearer token" : `Header ${server.auth_header_name}`}
            {server.has_credential && <span className="muted"> · •••• set</span>}
          </dd>
          <dt>Call timeout</dt><dd>{server.timeout_seconds}s</dd>
          <dt>Managed</dt><dd>{server.is_managed ? "Created automatically from Stores" : "Manual"}</dd>
        </dl>
        <h3 style={{ marginTop: 22 }}>Tools ({server.tools.length})</h3>
        {server.tools.length === 0 && <p className="muted">No tools known yet (the server has never answered).</p>}
        {server.tools.map((t) => (
          <div key={t.name} className="tool-item">
            <div className="chips">
              <span className="chip">{t.exposed_name}</span>
              <RoleBadge role={t.min_role} />
              {!t.read_only && <span className="badge badge-amber">write</span>}
            </div>
            {t.description && <p>{t.description}</p>}
          </div>
        ))}
      </aside>
    </>
  );
}

function ServerForm({ server, onClose, onSaved }: { server: Server | null; onClose: () => void; onSaved: () => void }) {
  const [form, setForm] = useState({
    name: server?.name ?? "",
    slug: server?.slug ?? "",
    url: server?.url ?? "",
    auth_type: server?.auth_type ?? ("none" as Server["auth_type"]),
    auth_header_name: server?.auth_header_name ?? "",
    credential: "",
    timeout_seconds: server?.timeout_seconds ?? 20,
  });
  const { busy, error, setError, run } = useAction();
  const set = <K extends keyof typeof form>(key: K, value: (typeof form)[K]) => setForm((f) => ({ ...f, [key]: value }));
  const needsCredential = form.auth_type !== "none";

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!server && !SLUG_RE.test(form.slug)) {
      setError("Slug: lowercase letters, digits and single hyphens (e.g. 'woo', 'store-2').");
      return;
    }
    const body: ServerInput = {
      name: form.name.trim(),
      url: form.url.trim(),
      auth_type: form.auth_type,
      auth_header_name: form.auth_type === "header" ? form.auth_header_name.trim() : null,
      timeout_seconds: Number(form.timeout_seconds),
      // Blank credential on edit = keep the stored one; the gateway never sends it back.
      ...(form.credential ? { credential: form.credential } : {}),
    };
    const saved = await run(() => (server ? api.updateServer(server.id, body) : api.createServer({ ...body, slug: form.slug })));
    if (saved) onSaved();
  };

  return (
    <Modal title={server ? `Edit ${server.name}` : "Add MCP server"} onClose={onClose}>
      <form onSubmit={submit}>
        <ErrorBanner message={error} />
        <div className="field-row">
          <div className="field">
            <label htmlFor="name">Name</label>
            <input id="name" required maxLength={100} value={form.name} onChange={(e) => set("name", e.target.value)} />
          </div>
          <div className="field">
            <label htmlFor="slug">Slug</label>
            <input id="slug" required maxLength={20} disabled={!!server} value={form.slug} onChange={(e) => set("slug", e.target.value.toLowerCase())} />
            <span className="hint">{server ? "Can't change: tool names depend on it." : "Tool prefix, e.g. 'crm' → crm__search"}</span>
          </div>
        </div>
        <div className="field">
          <label htmlFor="url">MCP endpoint URL</label>
          <input id="url" required maxLength={500} placeholder="https://example.com/mcp" value={form.url} onChange={(e) => set("url", e.target.value)} />
          <span className="hint">Streamable HTTP. Private/internal addresses are refused unless allow-listed.</span>
        </div>
        <div className="field-row">
          <div className="field">
            <label htmlFor="auth">Authentication</label>
            <select id="auth" value={form.auth_type} onChange={(e) => set("auth_type", e.target.value as Server["auth_type"])}>
              <option value="none">None</option>
              <option value="bearer">Bearer token</option>
              <option value="header">Custom header</option>
            </select>
          </div>
          <div className="field">
            <label htmlFor="timeout">Call timeout (s)</label>
            <input id="timeout" type="number" min={1} max={120} required value={form.timeout_seconds} onChange={(e) => set("timeout_seconds", Number(e.target.value))} />
          </div>
        </div>
        {form.auth_type === "header" && (
          <div className="field">
            <label htmlFor="header">Header name</label>
            <input id="header" required placeholder="X-API-Key" value={form.auth_header_name} onChange={(e) => set("auth_header_name", e.target.value)} />
          </div>
        )}
        {needsCredential && (
          <div className="field">
            <label htmlFor="credential">Credential</label>
            <input
              id="credential"
              type="password"
              autoComplete="off"
              required={!server?.has_credential}
              placeholder={server?.has_credential ? "•••• set — leave blank to keep" : ""}
              value={form.credential}
              onChange={(e) => set("credential", e.target.value)}
            />
            <span className="hint">Encrypted at rest and never shown again. Users' own keys are never forwarded upstream.</span>
          </div>
        )}
        <div className="modal-actions">
          <button type="button" className="button" onClick={onClose} disabled={busy}>Cancel</button>
          <button className="button button-primary" disabled={busy}>{busy ? "Saving…" : server ? "Save" : "Add server"}</button>
        </div>
      </form>
    </Modal>
  );
}
