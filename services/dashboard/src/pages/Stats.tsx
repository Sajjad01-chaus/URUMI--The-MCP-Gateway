import { useState } from "react";

import { api, type StatsWindow } from "../api";
import { Empty, ErrorBanner, PageHeader, StatusBadge, timeAgo, useLoad } from "../ui";

export default function StatsPage() {
  const [window, setWindow] = useState<StatsWindow>("1h");
  return (
    <>
      <PageHeader
        title="Stats"
        subtitle="Audit-backed traffic and latency; refreshes every 10 seconds."
        actions={(
          <select aria-label="Stats window" value={window} onChange={(event) => setWindow(event.target.value as StatsWindow)}>
            <option value="15m">Last 15 minutes</option>
            <option value="1h">Last hour</option>
            <option value="24h">Last 24 hours</option>
          </select>
        )}
      />
      <StatsView key={window} window={window} />
    </>
  );
}

function StatsView({ window }: { window: StatsWindow }) {
  const stats = useLoad(() => api.stats(window), 10_000);
  const maxP95 = Math.max(1, ...(stats.data?.servers.map((server) => server.p95_ms) ?? []));

  if (!stats.data && !stats.error) return <Empty>Loading stats…</Empty>;
  return (
    <>
      <ErrorBanner message={stats.error} />
      {stats.data && (
        <>
          <div className="stats-summary">
            <Summary label="Calls" value={stats.data.total_calls} />
            <Summary label="Errors" value={stats.data.total_errors} />
            <Summary
              label="Error rate"
              value={`${stats.data.total_calls ? ((stats.data.total_errors / stats.data.total_calls) * 100).toFixed(1) : "0.0"}%`}
            />
            <Summary label="Servers" value={stats.data.servers.length} />
          </div>

          <div className="card">
            <div className="card-header"><h2>Servers</h2></div>
            <div className="table-wrap">
              <table>
                <thead><tr><th>Server</th><th>Status</th><th>Calls</th><th>Errors</th><th>p50</th><th>p95 latency</th></tr></thead>
                <tbody>
                  {stats.data.servers.map((server) => (
                    <tr key={server.slug}>
                      <td><div className="cell-main">{server.name} <span className="chip">{server.slug}</span></div>{server.last_error && <div className="cell-sub">{server.last_error}</div>}</td>
                      <td><StatusBadge status={server.status} /><div className="cell-sub">since {timeAgo(server.status_since)}</div></td>
                      <td>{server.calls}</td><td>{server.error_rate.toFixed(1)}%</td><td>{server.p50_ms} ms</td>
                      <td><div className="latency-bar"><span style={{ width: `${(server.p95_ms / maxP95) * 100}%` }} /></div><span className="small muted">{server.p95_ms} ms</span></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>

          <div className="stats-columns">
            <RankedTools title="Top tools" rows={stats.data.top_tools} />
            <RankedTools title="Most errors" rows={stats.data.top_error_tools} />
          </div>

          <div className="card">
            <div className="card-header"><h2>Calls by user and source</h2></div>
            <div className="table-wrap"><table><thead><tr><th>User</th><th>Total</th><th>MCP</th><th>Chat</th></tr></thead><tbody>
              {stats.data.users.map((user) => <tr key={user.email}><td>{user.email}</td><td>{user.total}</td><td>{user.mcp}</td><td>{user.chat}</td></tr>)}
            </tbody></table></div>
            {stats.data.users.length === 0 && <Empty>No calls in this window.</Empty>}
          </div>
        </>
      )}
    </>
  );
}

function Summary({ label, value }: { label: string; value: string | number }) {
  return <div className="card card-body"><div className="stats-value">{value}</div><div className="muted">{label}</div></div>;
}

function RankedTools({ title, rows }: { title: string; rows: Array<{ name: string; calls: number; errors: number }> }) {
  return <div className="card"><div className="card-header"><h2>{title}</h2></div><div className="table-wrap"><table><thead><tr><th>Tool</th><th>Calls</th><th>Errors</th></tr></thead><tbody>
    {rows.map((row) => <tr key={row.name}><td><span className="chip">{row.name}</span></td><td>{row.calls}</td><td>{row.errors}</td></tr>)}
  </tbody></table></div>{rows.length === 0 && <Empty>No calls in this window.</Empty>}</div>;
}
