import { Fragment, useCallback, useEffect, useState } from "react";

import { api, type AuditRow } from "../api";
import { useCurrentUser } from "../auth";
import { Empty, ErrorBanner, OutcomeBadge, PageHeader, formatTime, messageOf, useLoad } from "../ui";

const PAGE_SIZE = 50;
const OUTCOMES = ["success", "error", "timeout", "denied", "unavailable"];

interface Filters {
  user: string;
  server: string;
  tool: string;
  outcome: string;
  source: string;
}

export default function AuditPage() {
  const me = useCurrentUser();
  const isAdmin = me.role === "admin";
  const servers = useLoad(api.servers);
  const [filters, setFilters] = useState<Filters>({ user: "", server: "", tool: "", outcome: "", source: "" });
  const [rows, setRows] = useState<AuditRow[] | null>(null);
  const [cursor, setCursor] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<number | null>(null);
  const [live, setLive] = useState(true);

  const loadFirst = useCallback(async () => {
    try {
      const page = await api.audit({ ...filters, limit: PAGE_SIZE });
      setRows(page.items);
      setCursor(page.next_cursor);
      setError(null);
    } catch (err) {
      setError(messageOf(err));
    }
  }, [filters]);

  useEffect(() => {
    void loadFirst();
    if (!live) return;
    const timer = window.setInterval(() => void loadFirst(), 5000);
    return () => window.clearInterval(timer);
  }, [loadFirst, live]);

  const loadMore = async () => {
    if (cursor === null) return;
    try {
      const page = await api.audit({ ...filters, limit: PAGE_SIZE, cursor });
      setRows((current) => [...(current ?? []), ...page.items]);
      setCursor(page.next_cursor);
      // Auto-refresh would replace the extra pages, so pause it while browsing history.
      setLive(false);
    } catch (err) {
      setError(messageOf(err));
    }
  };

  const set = (key: keyof Filters, value: string) => setFilters((f) => ({ ...f, [key]: value }));

  return (
    <>
      <PageHeader
        title="Audit log"
        subtitle={isAdmin ? "Every tool call through the gateway, by everyone, including denied and failed ones." : "Your own tool calls through the gateway."}
        actions={
          <label className="small" style={{ display: "flex", gap: 6, alignItems: "center" }}>
            <input type="checkbox" checked={live} onChange={(e) => setLive(e.target.checked)} style={{ width: "auto" }} />
            Auto-refresh
          </label>
        }
      />
      <div className="card">
        <div className="filters">
          {isAdmin && (
            <div className="field">
              <label htmlFor="f-user">User email</label>
              <input id="f-user" type="search" value={filters.user} onChange={(e) => set("user", e.target.value)} />
            </div>
          )}
          <div className="field">
            <label htmlFor="f-server">Server</label>
            <select id="f-server" value={filters.server} onChange={(e) => set("server", e.target.value)}>
              <option value="">All</option>
              {servers.data?.map((s) => <option key={s.slug} value={s.slug}>{s.slug}</option>)}
            </select>
          </div>
          <div className="field">
            <label htmlFor="f-tool">Tool (exact)</label>
            <input id="f-tool" type="search" placeholder="woo__list_orders" value={filters.tool} onChange={(e) => set("tool", e.target.value)} />
          </div>
          <div className="field">
            <label htmlFor="f-outcome">Outcome</label>
            <select id="f-outcome" value={filters.outcome} onChange={(e) => set("outcome", e.target.value)}>
              <option value="">All</option>
              {OUTCOMES.map((o) => <option key={o}>{o}</option>)}
            </select>
          </div>
          <div className="field">
            <label htmlFor="f-source">Source</label>
            <select id="f-source" value={filters.source} onChange={(e) => set("source", e.target.value)}>
              <option value="">All</option>
              <option value="mcp">mcp</option>
              <option value="chat">chat</option>
            </select>
          </div>
        </div>
        <ErrorBanner message={error} />
        <div className="table-wrap">
          <table>
            <thead>
              <tr><th>Time</th>{isAdmin && <th>User</th>}<th>Tool</th><th>Outcome</th><th>Duration</th><th>Source</th></tr>
            </thead>
            <tbody>
              {rows?.map((r) => (
                <Fragment key={r.id}>
                  <tr className="clickable" onClick={() => setExpanded(expanded === r.id ? null : r.id)}>
                    <td className="nowrap muted">{formatTime(r.ts)}</td>
                    {isAdmin && <td>{r.user_email}</td>}
                    <td className="mono">{r.exposed_name}</td>
                    <td><OutcomeBadge outcome={r.outcome} /></td>
                    <td className="nowrap">{r.duration_ms} ms</td>
                    <td><span className="badge badge-gray">{r.source}</span></td>
                  </tr>
                  {expanded === r.id && (
                    <tr className="detail-row">
                      <td colSpan={isAdmin ? 6 : 5}>
                        <dl className="key-value">
                          {r.error_message && <><dt>Error</dt><dd>{r.error_message}</dd></>}
                          <dt>Arguments</dt><dd className="mono">{r.args_preview ? JSON.stringify(r.args_preview) : "—"}</dd>
                          <dt>Server</dt><dd className="mono">{r.server_slug}</dd>
                          <dt>Client</dt><dd>{r.client_name || "—"}</dd>
                          <dt>Via</dt><dd>{r.api_key_id ? "API key" : "chat session"}</dd>
                        </dl>
                      </td>
                    </tr>
                  )}
                </Fragment>
              ))}
            </tbody>
          </table>
          {rows?.length === 0 && <Empty>No calls match these filters.</Empty>}
          {rows === null && !error && <Empty>Loading…</Empty>}
        </div>
        {cursor !== null && (
          <div className="card-body" style={{ textAlign: "center" }}>
            <button className="button" onClick={() => void loadMore()}>Load older entries</button>
          </div>
        )}
      </div>
    </>
  );
}
