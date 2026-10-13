import { Component, type ErrorInfo, type ReactNode } from "react";

interface State {
  error: Error | null;
}

/** A crash in one page shows a message instead of blanking the whole dashboard. */
export default class ErrorBoundary extends Component<{ children: ReactNode }, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    console.error("Dashboard crashed:", error, info.componentStack);
  }

  render() {
    if (!this.state.error) return this.props.children;
    return (
      <div className="auth-page">
        <div className="auth-card">
          <h1>Something went wrong</h1>
          <p className="muted">This page hit an unexpected error. Reloading usually fixes it; your data is safe on the server.</p>
          <pre className="small" style={{ whiteSpace: "pre-wrap" }}>{this.state.error.message}</pre>
          <button className="button button-primary" onClick={() => window.location.reload()}>Reload</button>
        </div>
      </div>
    );
  }
}
