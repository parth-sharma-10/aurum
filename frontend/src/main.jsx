import { Component, StrictMode } from "react";
import { createRoot } from "react-dom/client";

import App from "./App.jsx";
import "./index.css";

/**
 * The last line of defence for the screen itself.
 *
 * Every panel renders straight from a backend snapshot, and a field arriving
 * in a shape nobody anticipated throws during render. Without this React
 * unmounts the whole tree and the operator is left looking at a blank page -
 * the one state that says nothing at all about the machine.
 */
class ScreenGuard extends Component {
  state = { error: null };

  static getDerivedStateFromError(error) {
    return { error };
  }

  render() {
    if (!this.state.error) return this.props.children;
    return (
      <div className="shell">
        <div className="banner is-bad">
          <span aria-hidden="true">⚠</span> The dashboard hit an error drawing the screen:{" "}
          {String(this.state.error?.message ?? this.state.error)}. The machine itself is not
          affected.
        </div>
        <button className="is-primary" onClick={() => this.setState({ error: null })}>
          Try again
        </button>
      </div>
    );
  }
}

createRoot(document.getElementById("root")).render(
  <StrictMode>
    <ScreenGuard>
      <App />
    </ScreenGuard>
  </StrictMode>,
);
