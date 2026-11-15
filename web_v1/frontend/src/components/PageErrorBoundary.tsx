import { Component, type ReactNode } from "react";
import { useLocation } from "react-router-dom";
import { RotateCcw } from "lucide-react";

interface State { error: Error | null }

class Boundary extends Component<{ resetKey: string; children: ReactNode }, State> {
  state: State = { error: null };
  static getDerivedStateFromError(error: Error): State { return { error }; }
  componentDidCatch(error: Error) { console.error("Page failed", error); }
  componentDidUpdate(prev: { resetKey: string }) {
    if (prev.resetKey !== this.props.resetKey && this.state.error) this.setState({ error: null });
  }
  render() {
    const { error } = this.state;
    if (!error) return this.props.children;
    // A page file that fails to load usually means the app was updated; a reload fetches the new one.
    const stale = /dynamically imported module|Failed to fetch|Loading chunk/i.test(error.message);
    return (
      <section className="panel section empty-state" role="alert">
        <h2>{stale ? "This page needs a refresh" : "This page ran into a problem"}</h2>
        <p className="muted">{stale ? "A newer version of the app is available, or the connection dropped." : "Nothing was changed. Try again, or reload the page if it keeps happening."}</p>
        <div className="cc-actions">
          {!stale && <button type="button" className="btn primary" onClick={() => this.setState({ error: null })}><RotateCcw size={16} aria-hidden="true" />Try again</button>}
          <button type="button" className={`btn${stale ? " primary" : ""}`} onClick={() => window.location.reload()}>Reload the page</button>
        </div>
      </section>
    );
  }
}

/** Keeps a failing page from blanking the whole app; moving to another page clears the error. */
export function PageErrorBoundary({ children }: { children: ReactNode }) {
  const { pathname } = useLocation();
  return <Boundary resetKey={pathname}>{children}</Boundary>;
}
