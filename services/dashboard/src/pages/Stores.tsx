import { useEffect, useState, type FormEvent } from "react";

import { api, type Store } from "../api";
import { Empty, ErrorBanner, Modal, PageHeader, StatusBadge, formatTime, useAction, useLoad } from "../ui";

const SLUG_RE = /^[a-z0-9]+(-[a-z0-9]+)*$/;

function suggestedSlug(name: string): string {
  return name.toLowerCase().trim().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 20);
}

export default function StoresPage() {
  const [view, setView] = useState<"active" | "deleted">("active");
  const [creating, setCreating] = useState(false);
  const [deleting, setDeleting] = useState<Store | null>(null);
  const stores = useLoad(() => api.stores(view), 3_000);
  const action = useAction();

  useEffect(() => {
    void stores.reload();
  }, [view, stores.reload]);

  const retry = (store: Store) => action.run(async () => {
    await api.retryStore(store.id);
    await stores.reload();
  });

  return (
    <>
      <PageHeader
        title="Stores"
        subtitle="Create isolated WooCommerce stores. Each ready store adds its own prefixed tools to the gateway."
        actions={view === "active" && (
          <button className="button button-primary" onClick={() => setCreating(true)}>Create store</button>
        )}
      />
      <ErrorBanner message={stores.error ?? action.error} />
      <div className="page-actions store-tabs" role="group" aria-label="Store view">
        <button className={`button ${view === "active" ? "button-primary" : ""}`} onClick={() => setView("active")}>Active</button>
        <button className={`button ${view === "deleted" ? "button-primary" : ""}`} onClick={() => setView("deleted")}>History</button>
      </div>
      <div className="card">
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Store</th>
                <th>Status</th>
                <th>Owner</th>
                <th>Created</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {stores.data?.map((store) => (
                <tr key={store.id}>
                  <td>
                    <div className="cell-main">
                      {store.name} <span className="chip">{store.slug}</span>{" "}
                      {store.built_in && <span className="badge badge-violet">built-in</span>}
                    </div>
                    <div className="cell-sub">{store.reason}</div>
                  </td>
                  <td><StatusBadge status={store.status} /></td>
                  <td>{store.owner_email ?? (store.built_in ? "Platform" : "—")}</td>
                  <td className="nowrap muted">{formatTime(store.created_at)}</td>
                  <td>
                    <div className="row-actions">
                      {store.status === "ready" && (
                        <a className="button button-small" href={store.url} target="_blank" rel="noreferrer">Open store</a>
                      )}
                      {store.status === "failed" && (
                        <button className="button button-small" disabled={action.busy} onClick={() => void retry(store)}>Retry</button>
                      )}
                      {!store.built_in && store.status !== "deleting" && store.status !== "deleted" && (
                        <button className="button button-small button-danger" onClick={() => setDeleting(store)}>Delete</button>
                      )}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {stores.data?.length === 0 && <Empty>{view === "active" ? "No stores yet." : "No deleted stores."}</Empty>}
          {!stores.data && !stores.error && <Empty>Loading…</Empty>}
        </div>
      </div>

      {creating && <CreateStoreDialog onClose={() => setCreating(false)} onCreated={stores.reload} />}
      {deleting && (
        <DeleteStoreDialog
          store={deleting}
          onClose={() => setDeleting(null)}
          onDeleted={stores.reload}
        />
      )}
    </>
  );
}

function CreateStoreDialog({ onClose, onCreated }: { onClose: () => void; onCreated: () => Promise<void> }) {
  const [name, setName] = useState("");
  const [slug, setSlug] = useState("");
  const [slugEdited, setSlugEdited] = useState(false);
  const { busy, error, setError, run } = useAction();

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!SLUG_RE.test(slug)) {
      setError("Slug must contain lowercase letters, numbers and single hyphens only.");
      return;
    }
    const created = await run(() => api.createStore(name.trim(), slug));
    if (created) {
      await onCreated();
      onClose();
    }
  };

  return (
    <Modal title="Create WooCommerce store" onClose={onClose}>
      <form onSubmit={submit}>
        <ErrorBanner message={error} />
        <div className="field">
          <label htmlFor="store-name">Store name</label>
          <input
            id="store-name"
            required
            maxLength={100}
            value={name}
            onChange={(event) => {
              setName(event.target.value);
              if (!slugEdited) setSlug(suggestedSlug(event.target.value));
            }}
          />
        </div>
        <div className="field">
          <label htmlFor="store-slug">Slug</label>
          <input
            id="store-slug"
            required
            maxLength={20}
            value={slug}
            onChange={(event) => {
              setSlugEdited(true);
              setSlug(event.target.value.toLowerCase());
            }}
          />
          <span className="hint">Permanent tool prefix: {slug || "slug"}__list_products</span>
        </div>
        <div className="banner banner-info">
          Provisioning normally takes a few minutes. You can leave this page; status updates automatically.
        </div>
        <div className="modal-actions">
          <button type="button" className="button" onClick={onClose} disabled={busy}>Cancel</button>
          <button className="button button-primary" disabled={busy}>{busy ? "Creating…" : "Create store"}</button>
        </div>
      </form>
    </Modal>
  );
}

function DeleteStoreDialog({ store, onClose, onDeleted }: {
  store: Store;
  onClose: () => void;
  onDeleted: () => Promise<void>;
}) {
  const [confirmation, setConfirmation] = useState("");
  const { busy, error, run } = useAction();
  const matches = confirmation === store.name;

  return (
    <Modal title={`Delete ${store.name}?`} onClose={onClose}>
      <p className="muted">
        This removes the store, its database and its MCP tools. Audit history is kept. Type the exact store name to continue.
      </p>
      <div className="field">
        <label htmlFor="delete-store-name">Type <strong>{store.name}</strong></label>
        <input id="delete-store-name" value={confirmation} onChange={(event) => setConfirmation(event.target.value)} autoComplete="off" />
      </div>
      <ErrorBanner message={error} />
      <div className="modal-actions">
        <button className="button" onClick={onClose} disabled={busy}>Cancel</button>
        <button
          className="button button-danger"
          disabled={busy || !matches}
          onClick={() => void run(async () => {
            await api.deleteStore(store.id);
            await onDeleted();
            onClose();
          })}
        >
          {busy ? "Deleting…" : "Delete store"}
        </button>
      </div>
    </Modal>
  );
}
