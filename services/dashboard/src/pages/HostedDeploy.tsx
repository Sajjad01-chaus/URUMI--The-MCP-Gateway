import { useState, type FormEvent } from "react";

import { api, type HostedServerInput } from "../api";
import { ErrorBanner, Modal, useAction } from "../ui";

const SLUG_RE = /^[a-z0-9]+(-[a-z0-9]+)*$/;

function envLines(value: string): Record<string, string> {
  const result: Record<string, string> = {};
  for (const [index, raw] of value.split("\n").entries()) {
    const line = raw.trim();
    if (!line) continue;
    const separator = line.indexOf("=");
    if (separator < 1) throw new Error(`Environment line ${index + 1} must be KEY=value.`);
    const key = line.slice(0, separator).trim();
    if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(key)) throw new Error(`Invalid environment name '${key}'.`);
    result[key] = line.slice(separator + 1);
  }
  return result;
}

export function HostedDeployDialog({ onClose, onCreated }: {
  onClose: () => void;
  onCreated: () => Promise<void>;
}) {
  const [form, setForm] = useState({
    name: "",
    slug: "",
    image: "urumi/fx-mcp:dev",
    port: 8000,
    path: "/mcp",
    env: "",
    secretEnv: "",
    authType: "none" as HostedServerInput["auth_type"],
    authHeaderName: "",
    credentialEnv: "",
    internetEgress: false,
    size: "small" as HostedServerInput["size"],
  });
  const { busy, error, setError, run } = useAction();
  const set = <K extends keyof typeof form>(key: K, value: (typeof form)[K]) =>
    setForm((current) => ({ ...current, [key]: value }));

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!SLUG_RE.test(form.slug)) {
      setError("Slug must contain lowercase letters, numbers and single hyphens only.");
      return;
    }
    let env: Record<string, string>;
    let secretEnv: Record<string, string>;
    try {
      env = envLines(form.env);
      secretEnv = envLines(form.secretEnv);
    } catch (parseError) {
      setError(parseError instanceof Error ? parseError.message : "Invalid environment variables.");
      return;
    }
    const created = await run(() => api.deployHostedServer({
      name: form.name.trim(),
      slug: form.slug,
      image: form.image.trim(),
      port: Number(form.port),
      path: form.path.trim(),
      env,
      secret_env: secretEnv,
      auth_type: form.authType,
      auth_header_name: form.authType === "header" ? form.authHeaderName.trim() : null,
      credential_env: form.authType === "none" ? null : form.credentialEnv.trim(),
      internet_egress: form.internetEgress,
      size: form.size,
    }));
    if (created) {
      await onCreated();
      onClose();
    }
  };

  return (
    <Modal title="Deploy MCP from image" onClose={onClose}>
      <form onSubmit={submit}>
        <ErrorBanner message={error} />
        <div className="field-row">
          <div className="field">
            <label htmlFor="hosted-name">Name</label>
            <input id="hosted-name" required maxLength={100} value={form.name} onChange={(e) => set("name", e.target.value)} />
          </div>
          <div className="field">
            <label htmlFor="hosted-slug">Slug</label>
            <input id="hosted-slug" required maxLength={20} value={form.slug} onChange={(e) => set("slug", e.target.value.toLowerCase())} />
          </div>
        </div>
        <div className="field">
          <label htmlFor="hosted-image">Container image</label>
          <input id="hosted-image" required value={form.image} onChange={(e) => set("image", e.target.value)} />
          <span className="hint">An allow-listed registry and an explicit non-latest tag are required.</span>
        </div>
        <div className="field-row">
          <div className="field">
            <label htmlFor="hosted-port">Port</label>
            <input id="hosted-port" type="number" min={1024} max={65535} required value={form.port} onChange={(e) => set("port", Number(e.target.value))} />
          </div>
          <div className="field">
            <label htmlFor="hosted-path">MCP path</label>
            <input id="hosted-path" required value={form.path} onChange={(e) => set("path", e.target.value)} />
          </div>
          <div className="field">
            <label htmlFor="hosted-size">Size</label>
            <select id="hosted-size" value={form.size} onChange={(e) => set("size", e.target.value as HostedServerInput["size"])}>
              <option value="small">Small</option>
              <option value="medium">Medium</option>
            </select>
          </div>
        </div>
        <div className="field">
          <label htmlFor="hosted-env">Plain environment</label>
          <textarea id="hosted-env" rows={3} placeholder="LOG_LEVEL=info" value={form.env} onChange={(e) => set("env", e.target.value)} />
        </div>
        <div className="field">
          <label htmlFor="hosted-secret-env">Secret environment</label>
          <textarea id="hosted-secret-env" rows={3} placeholder="API_TOKEN=write-only-value" value={form.secretEnv} onChange={(e) => set("secretEnv", e.target.value)} />
          <span className="hint">Encrypted at rest and never returned after deployment.</span>
        </div>
        <div className="field-row">
          <div className="field">
            <label htmlFor="hosted-auth">Gateway authentication</label>
            <select id="hosted-auth" value={form.authType} onChange={(e) => set("authType", e.target.value as HostedServerInput["auth_type"])}>
              <option value="none">None</option>
              <option value="bearer">Bearer token</option>
              <option value="header">Custom header</option>
            </select>
          </div>
          {form.authType !== "none" && (
            <div className="field">
              <label htmlFor="hosted-credential-env">Credential env name</label>
              <input id="hosted-credential-env" required value={form.credentialEnv} onChange={(e) => set("credentialEnv", e.target.value)} />
            </div>
          )}
          {form.authType === "header" && (
            <div className="field">
              <label htmlFor="hosted-auth-header">Header name</label>
              <input id="hosted-auth-header" required value={form.authHeaderName} onChange={(e) => set("authHeaderName", e.target.value)} />
            </div>
          )}
        </div>
        <label className="checkbox-row">
          <input type="checkbox" checked={form.internetEgress} onChange={(e) => set("internetEgress", e.target.checked)} />
          Allow public internet egress; private networks, metadata and the cluster remain blocked
        </label>
        <div className="modal-actions">
          <button type="button" className="button" onClick={onClose} disabled={busy}>Cancel</button>
          <button className="button button-primary" disabled={busy}>{busy ? "Deploying…" : "Deploy image"}</button>
        </div>
      </form>
    </Modal>
  );
}
