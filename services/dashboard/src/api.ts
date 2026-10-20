export type Role = "member" | "admin";

export interface User {
  id: string;
  email: string;
  name: string;
  role: Role;
  status: "active" | "disabled";
  created_at: string;
  last_login_at: string | null;
  active_keys: number;
}

export interface ServerTool {
  name: string;
  exposed_name: string;
  description: string;
  min_role: Role;
  read_only: boolean;
}

export interface Server {
  id: string;
  slug: string;
  name: string;
  url: string;
  enabled: boolean;
  status: "unknown" | "connected" | "unreachable" | "disabled";
  auth_type: "none" | "bearer" | "header";
  auth_header_name: string | null;
  has_credential: boolean;
  timeout_seconds: number;
  is_managed: boolean;
  managed_kind: "store" | "hosted_mcp" | null;
  version: string;
  last_error: string | null;
  last_checked_at: string | null;
  tools: ServerTool[];
}

export type StoreStatus = "provisioning" | "ready" | "failed" | "deleting" | "deleted";

export interface Store {
  id: string;
  name: string;
  slug: string;
  owner_email: string | null;
  status: StoreStatus;
  reason: string;
  url: string;
  created_at: string | null;
  ready_at: string | null;
  built_in: boolean;
}

export type HostedStatus = "provisioning" | "ready" | "failed" | "deleting" | "deleted";

export interface HostedServer {
  id: string;
  name: string;
  slug: string;
  image: string;
  port: number;
  path: string;
  env: Record<string, string>;
  secret_env_names: string[];
  auth_type: Server["auth_type"];
  auth_header_name: string | null;
  credential_env: string | null;
  internet_egress: boolean;
  size: "small" | "medium";
  status: HostedStatus;
  reason: string;
  created_at: string | null;
  ready_at: string | null;
}

export interface HostedServerInput {
  name: string;
  slug: string;
  image: string;
  port: number;
  path: string;
  env: Record<string, string>;
  secret_env: Record<string, string>;
  auth_type: Server["auth_type"];
  auth_header_name: string | null;
  credential_env: string | null;
  internet_egress: boolean;
  size: "small" | "medium";
}

export interface ServerInput {
  name: string;
  slug?: string;
  url: string;
  auth_type: Server["auth_type"];
  auth_header_name: string | null;
  credential?: string;
  timeout_seconds: number;
  enabled?: boolean;
}

export interface CatalogTool {
  exposed_name: string;
  server_slug: string;
  server_name: string;
  tool: string;
  description: string;
  min_role: Role;
  read_only: boolean;
  input_schema: { properties?: Record<string, { type?: string; description?: string }>; required?: string[] };
}

export interface Permission {
  server_slug: string;
  tool: string;
  exposed_name: string;
  read_only: boolean;
  default_role: Role;
  override: Role | null;
  min_role: Role;
}

export interface Invite {
  id: string;
  email: string;
  role: Role;
  expires_at: string;
  link: string | null;
}

export interface ApiKey {
  id: string;
  user_email: string;
  name: string;
  prefix: string;
  created_at: string;
  last_used_at: string | null;
  revoked_at: string | null;
}

export interface AuditRow {
  id: number;
  ts: string;
  user_email: string;
  api_key_id: string | null;
  source: "mcp" | "chat";
  server_slug: string;
  tool_name: string;
  exposed_name: string;
  duration_ms: number;
  outcome: "success" | "error" | "timeout" | "denied" | "unavailable";
  error_message: string | null;
  client_name: string | null;
  args_preview: Record<string, unknown> | null;
}

export interface AuditPage {
  items: AuditRow[];
  next_cursor: number | null;
}

export type StatsWindow = "15m" | "1h" | "24h";

export interface GatewayStats {
  window: StatsWindow;
  generated_at: string;
  total_calls: number;
  total_errors: number;
  servers: Array<{
    slug: string;
    name: string;
    status: Server["status"];
    status_since: string | null;
    calls: number;
    error_rate: number;
    p50_ms: number;
    p95_ms: number;
    last_error: string | null;
  }>;
  top_tools: Array<{ name: string; calls: number; errors: number }>;
  top_error_tools: Array<{ name: string; calls: number; errors: number }>;
  users: Array<{ email: string; total: number; mcp: number; chat: number }>;
}

export interface ChatMessage {
  role: "user" | "assistant";
  content: string;
}

export interface ChatToolCall {
  name: string;
  arguments: Record<string, unknown> | null;
  result: string;
  is_error: boolean;
  duration_ms: number;
}

export type StopReason =
  | "answered" | "max_iterations" | "max_tool_calls" | "timeout" | "llm_error" | "malformed_tool_call" | "error";

export interface ChatTurn {
  answer: string;
  tool_calls: ChatToolCall[];
  stopped_reason: StopReason;
}

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

let onUnauthorized: () => void = () => {};

/** The auth provider registers this so any 401 sends the user back to the login page. */
export function setUnauthorizedHandler(handler: () => void): void {
  onUnauthorized = handler;
}

function errorMessage(body: unknown, fallback: string): string {
  if (body && typeof body === "object" && "detail" in body) {
    const detail = (body as { detail: unknown }).detail;
    if (typeof detail === "string") return detail;
    // FastAPI validation errors: [{loc: [...], msg: "..."}]
    if (Array.isArray(detail)) {
      return detail
        .map((d: { loc?: unknown[]; msg?: string }) => `${d.loc?.slice(-1)[0] ?? "field"}: ${d.msg ?? "invalid"}`)
        .join("; ");
    }
  }
  return fallback;
}

async function request<T>(method: string, path: string, body?: unknown, quiet401 = false): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`/api${path}`, {
      method,
      credentials: "same-origin",
      headers: {
        Accept: "application/json",
        // CSRF guard: the gateway refuses cookie-authenticated writes without this header.
        "X-Requested-With": "dashboard",
        ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
      },
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
  } catch {
    throw new ApiError(0, "Cannot reach the gateway. Check your connection and try again.");
  }
  if (response.status === 204) return undefined as T;
  const data: unknown = await response.json().catch(() => null);
  if (!response.ok) {
    if (response.status === 401 && !quiet401) onUnauthorized();
    throw new ApiError(response.status, errorMessage(data, `${response.status} ${response.statusText}`));
  }
  return data as T;
}

const q = (params: Record<string, string | number | undefined | null>) => {
  const search = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== null && v !== "") search.set(k, String(v));
  const s = search.toString();
  return s ? `?${s}` : "";
};

export const api = {
  me: () => request<User>("GET", "/auth/me", undefined, true),
  login: (email: string, password: string) => request<User>("POST", "/auth/login", { email, password }, true),
  logout: () => request<void>("POST", "/auth/logout"),
  meta: () => request<{ mcp_url: string; gateway_version: string }>("GET", "/meta"),

  servers: () => request<Server[]>("GET", "/servers"),
  createServer: (data: ServerInput) => request<Server>("POST", "/servers", data),
  updateServer: (id: string, data: Partial<ServerInput>) => request<Server>("PATCH", `/servers/${id}`, data),
  deleteServer: (id: string) => request<void>("DELETE", `/servers/${id}`),
  testServer: (id: string) => request<Server>("POST", `/servers/${id}/test`),

  stores: (include: "active" | "deleted" = "active") => request<Store[]>("GET", `/stores${q({ include })}`),
  createStore: (name: string, slug: string) => request<Store>("POST", "/stores", { name, slug }),
  retryStore: (id: string) => request<Store>("POST", `/stores/${id}/retry`),
  deleteStore: (id: string) => request<Store>("DELETE", `/stores/${id}`),

  hostedServers: (include: "active" | "deleted" = "active") =>
    request<HostedServer[]>("GET", `/hosted-servers${q({ include })}`),
  deployHostedServer: (body: HostedServerInput) => request<HostedServer>("POST", "/hosted-servers", body),
  retryHostedServer: (id: string) => request<HostedServer>("POST", `/hosted-servers/${id}/retry`),
  deleteHostedServer: (id: string) => request<HostedServer>("DELETE", `/hosted-servers/${id}`),

  tools: () => request<CatalogTool[]>("GET", "/tools"),
  permissions: () => request<Permission[]>("GET", "/permissions"),
  setPermission: (slug: string, tool: string, minRole: Role | null) =>
    request<Permission>("PUT", `/permissions/${encodeURIComponent(slug)}/${encodeURIComponent(tool)}`, { min_role: minRole }),

  members: () => request<User[]>("GET", "/members"),
  updateMember: (id: string, patch: Partial<Pick<User, "role" | "status">>) => request<User>("PATCH", `/members/${id}`, patch),
  invites: () => request<Invite[]>("GET", "/invites"),
  createInvite: (email: string, role: Role) => request<Invite>("POST", "/invites", { email, role }),
  cancelInvite: (id: string) => request<void>("DELETE", `/invites/${id}`),
  viewInvite: (token: string) => request<Invite>("GET", `/invites/${encodeURIComponent(token)}`, undefined, true),
  acceptInvite: (token: string, name: string, password: string) =>
    request<User>("POST", `/invites/${encodeURIComponent(token)}/accept`, { name, password }, true),

  keys: () => request<ApiKey[]>("GET", "/keys"),
  createKey: (name: string) => request<ApiKey & { key: string }>("POST", "/keys", { name }),
  revokeKey: (id: string) => request<void>("DELETE", `/keys/${id}`),

  chat: (messages: ChatMessage[]) => request<ChatTurn>("POST", "/chat", { messages }),

  audit: (filters: Record<string, string | number | undefined | null>) => request<AuditPage>("GET", `/audit${q(filters)}`),
  stats: (window: StatsWindow) => request<GatewayStats>("GET", `/stats${q({ window })}`),
};
