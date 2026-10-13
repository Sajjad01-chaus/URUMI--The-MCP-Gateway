import { useState } from "react";
import { Link } from "react-router-dom";

import { api } from "../api";
import { CodeBlock, ErrorBanner, PageHeader, useLoad } from "../ui";

const KEY = "<YOUR_API_KEY>";

const clients = (url: string) => [
  {
    id: "claude",
    label: "Claude Code",
    steps: "Run once in a terminal, then check with /mcp inside Claude Code.",
    code: `claude mcp add --transport http urumi ${url} --header "Authorization: Bearer ${KEY}"`,
  },
  {
    id: "cursor",
    label: "Cursor",
    steps: "Add to ~/.cursor/mcp.json (or .cursor/mcp.json in a project).",
    code: JSON.stringify({ mcpServers: { urumi: { url, headers: { Authorization: `Bearer ${KEY}` } } } }, null, 2),
  },
  {
    id: "vscode",
    label: "VS Code",
    steps: "Add to .vscode/mcp.json, then start the server from the MCP view.",
    code: JSON.stringify({ servers: { urumi: { type: "http", url, headers: { Authorization: `Bearer ${KEY}` } } } }, null, 2),
  },
  {
    id: "inspector",
    label: "MCP Inspector",
    steps: "Start the Inspector, choose transport “Streamable HTTP”, paste the URL, and add the header under Authentication / Headers.",
    code: `npx @modelcontextprotocol/inspector\n\nURL:     ${url}\nHeader:  Authorization: Bearer ${KEY}`,
  },
];

export default function ConnectPage() {
  const meta = useLoad(api.meta);
  const [active, setActive] = useState("claude");
  const url = meta.data?.mcp_url ?? "…";
  const client = clients(url).find((c) => c.id === active) ?? clients(url)[0];

  return (
    <>
      <PageHeader title="Connect a client" subtitle="Point any MCP client at the gateway with your API key; it sees every tool your role allows." />
      <ErrorBanner message={meta.error} />
      <div className="card">
        <div className="card-body">
          <h3>Gateway endpoint</h3>
          <CodeBlock code={url} />
          <p className="muted small" style={{ marginBottom: 0 }}>
            Replace <code>{KEY}</code> with a key from <Link to="/keys">API keys</Link>. Tool names look like{" "}
            <code>woo__list_orders</code>: the prefix says which system the tool acts on.
          </p>
        </div>
      </div>
      <div className="card">
        <div className="filters" role="tablist">
          {clients(url).map((c) => (
            <button
              key={c.id}
              role="tab"
              aria-selected={c.id === active}
              className={`button button-small ${c.id === active ? "button-primary" : ""}`}
              onClick={() => setActive(c.id)}
            >
              {c.label}
            </button>
          ))}
        </div>
        <div className="card-body">
          <p className="muted" style={{ marginTop: 0 }}>{client.steps}</p>
          <CodeBlock code={client.code} />
        </div>
      </div>
    </>
  );
}
