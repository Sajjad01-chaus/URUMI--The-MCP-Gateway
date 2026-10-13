import { useMemo, useState } from "react";

import { api, type CatalogTool } from "../api";
import { useCurrentUser } from "../auth";
import { Empty, ErrorBanner, PageHeader, RoleBadge, useLoad } from "../ui";

export default function ToolsPage() {
  const user = useCurrentUser();
  const tools = useLoad(api.tools);
  const [search, setSearch] = useState("");

  const groups = useMemo(() => {
    const needle = search.trim().toLowerCase();
    const byServer = new Map<string, CatalogTool[]>();
    for (const t of tools.data ?? []) {
      if (needle && !`${t.exposed_name} ${t.description}`.toLowerCase().includes(needle)) continue;
      byServer.set(t.server_name, [...(byServer.get(t.server_name) ?? []), t]);
    }
    return [...byServer.entries()];
  }, [tools.data, search]);

  return (
    <>
      <PageHeader
        title="Tools"
        subtitle={`Exactly what your MCP client sees as ${user.role}: tools of connected servers that your role may call.`}
        actions={<input type="search" placeholder="Search tools…" value={search} onChange={(e) => setSearch(e.target.value)} aria-label="Search tools" />}
      />
      <ErrorBanner message={tools.error} />
      {!tools.data && !tools.error && <Empty>Loading…</Empty>}
      {tools.data && groups.length === 0 && <div className="card"><Empty>No tools match.</Empty></div>}
      {groups.map(([serverName, list]) => (
        <div key={serverName} className="card server-group">
          <div className="card-body">
            <h2>{serverName} <span className="muted small">· {list.length} tools</span></h2>
            {list.map((t) => {
              const params = Object.entries(t.input_schema.properties ?? {});
              const required = new Set(t.input_schema.required ?? []);
              return (
                <div key={t.exposed_name} className="tool-item">
                  <div className="chips">
                    <span className="chip">{t.exposed_name}</span>
                    <RoleBadge role={t.min_role} />
                    {t.read_only ? <span className="badge badge-gray">read-only</span> : <span className="badge badge-amber">write</span>}
                  </div>
                  {t.description && <p>{t.description}</p>}
                  {params.length > 0 && (
                    <p className="small">
                      Parameters:{" "}
                      {params.map(([name, schema]) => (
                        <span key={name} className="chip" title={schema.description} style={{ marginRight: 4 }}>
                          {name}{required.has(name) ? "*" : ""}: {schema.type ?? "any"}
                        </span>
                      ))}
                    </p>
                  )}
                </div>
              );
            })}
          </div>
        </div>
      ))}
    </>
  );
}
