import { api, type Permission, type Role } from "../api";
import { Empty, ErrorBanner, PageHeader, RoleBadge, useAction, useLoad } from "../ui";

export default function PermissionsPage() {
  const permissions = useLoad(api.permissions);
  const action = useAction();

  const change = (p: Permission, value: string) =>
    action.run(async () => {
      const updated = await api.setPermission(p.server_slug, p.tool, value === "default" ? null : (value as Role));
      permissions.setData((list) => list?.map((x) => (x.exposed_name === updated.exposed_name ? updated : x)));
    });

  return (
    <>
      <PageHeader
        title="Tool permissions"
        subtitle="Minimum role per tool. Defaults come from the server's annotations: read-only → member, anything else → admin."
      />
      <div className="banner banner-info">
        Annotations come from the upstream server and can be wrong. Override any tool here; the gateway enforces it on both
        <code> tools/list</code> and <code>tools/call</code>, for API keys and chat alike.
      </div>
      <ErrorBanner message={permissions.error ?? action.error} />
      <div className="card">
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Tool</th>
                <th>Annotation</th>
                <th>Default</th>
                <th>Minimum role</th>
              </tr>
            </thead>
            <tbody>
              {permissions.data?.map((p) => (
                <tr key={p.exposed_name}>
                  <td className="mono">{p.exposed_name}</td>
                  <td>{p.read_only ? <span className="badge badge-gray">read-only</span> : <span className="badge badge-amber">write</span>}</td>
                  <td><RoleBadge role={p.default_role} /></td>
                  <td>
                    <select
                      value={p.override ?? "default"}
                      disabled={action.busy}
                      onChange={(e) => void change(p, e.target.value)}
                      aria-label={`Minimum role for ${p.exposed_name}`}
                      style={{ maxWidth: 200 }}
                    >
                      <option value="default">Default ({p.default_role})</option>
                      <option value="member">member</option>
                      <option value="admin">admin</option>
                    </select>
                    {p.override && <span className="cell-sub"> overridden</span>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {permissions.data?.length === 0 && <Empty>No tools known yet.</Empty>}
        </div>
      </div>
    </>
  );
}
