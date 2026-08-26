/**
 * Operator mode: the screen somebody runs the machine from.
 *
 * The whole screen answers three questions, in this order and never out of it:
 *
 *     WHERE ARE WE?     the state banner and the hardware dots
 *     WHAT IS HAPPENING? the current object and the process strip
 *     WHAT SHOULD I DO?  the action line, and "How to fix" on anything broken
 *
 * Nothing here is clickable in the normal cycle. The load cell starts the
 * measurement, the model supplies the class, the decision engine picks the bin
 * and the scheduler decides when the paddle fires. An operator places an object
 * and takes it away again; every button on this screen exists for the case
 * where something has already gone wrong.
 *
 * No raw enum reaches this file's output - `plain()` in status.js owns that -
 * and no state is carried by colour alone. Every dot, chip and row prints its
 * word beside its mark, because a red circle and a green circle are the same
 * circle to a colour-blind operator across a workshop.
 */

import { useEffect, useRef, useState } from "react";

import {
  BIN_CLASS,
  CLASS_COLOR,
  HELP,
  activity,
  clock,
  grams,
  num,
  objectValue,
  pct,
  plain,
  refusal,
  short,
  stages,
  subsystems,
  worstSubsystem,
} from "./status.js";

/** The mark that goes with a level. Never the only signal - the word is too. */
const MARK = { ready: "●", warning: "⚠", offline: "○", fault: "✕", busy: "●", good: "✓" };

// -- masthead --------------------------------------------------------------

export function Masthead({ state, machine, mode, onMode, onEstop, busy }) {
  const board = state?.board ?? {};
  const cal = state?.calibration ?? {};
  const simulated = state?.hardware?.mode === "SIMULATION";
  const dots = [
    ["Camera", Boolean(state?.running && !state?.camera?.error)],
    ["Load Cell", Boolean((board.connected || simulated) && cal.verified)],
    ["Arduino", Boolean(board.connected || simulated)],
    ["Servos", Boolean(board.servo_config_applied || simulated)],
  ];
  return (
    <header className="masthead">
      <div className="masthead-brand">
        <h1 className="wordmark">AURUM</h1>
        <p className="masthead-sub">Automatic identification &amp; sorting</p>
      </div>

      <div className="masthead-dots">
        {dots.map(([label, ok]) => (
          <span key={label} className={`hw-dot ${ok ? "is-ready" : "is-off"}`}>
            <span className="hw-mark" aria-hidden="true">
              {ok ? MARK.ready : MARK.offline}
            </span>
            <span className="hw-label">{label}</span>
            <span className="hw-word">{ok ? "Ready" : "Off"}</span>
          </span>
        ))}
      </div>

      <div className="masthead-right">
        <span className={`mode-badge ${simulated ? "is-sim" : "is-real"}`}>
          {simulated ? "SIMULATION" : "PHYSICAL"}
        </span>
        <div className="mode-switch" role="group" aria-label="Interface mode">
          <button
            className={mode === "operator" ? "is-on" : ""}
            onClick={() => onMode("operator")}
            aria-pressed={mode === "operator"}
          >
            Operator
          </button>
          <button
            className={mode === "advanced" ? "is-on" : ""}
            onClick={() => onMode("advanced")}
            aria-pressed={mode === "advanced"}
          >
            Maintenance
          </button>
        </div>
        <button
          className="estop"
          onClick={onEstop}
          title="Stops all physical movement. Stays stopped until a human resets it."
        >
          {busy === "estop" ? "Stopping…" : "EMERGENCY STOP"}
        </button>
      </div>

      <div className={`masthead-state tone-${machine.tone}`}>
        <span className="masthead-state-mark" aria-hidden="true">
          {MARK[machine.tone] ?? "●"}
        </span>
        {machine.key.replace(/_/g, " ")}
      </div>
    </header>
  );
}

// -- the dominant state ----------------------------------------------------

/** What the machine is doing, big enough to read from across the room. */
export function MachineStatus({ machine, extra }) {
  return (
    <section className={`status-hero tone-${machine.tone}`}>
      <p className="status-eyebrow">Machine status</p>
      <h2 className="status-title">{machine.title}</h2>
      <p className="status-detail">{machine.detail}</p>
      {machine.action && (
        <p className="status-action">
          <span className="status-action-label">What to do</span>
          {machine.action}
        </p>
      )}
      {extra}
    </section>
  );
}

// -- errors, and how to fix them -------------------------------------------

/**
 * Impossible to miss and still calm.
 *
 * One sentence saying what cannot happen, one saying why, and a way to open
 * the checklist. Not a red screen: an operator who is startled six times an
 * hour stops reading the seventh.
 */
export function ActionRequired({ row, onHelp, onReset, faultActive, busy }) {
  if (!row && !faultActive) return null;
  return (
    <section className="action-required" role="alert">
      <span className="action-mark" aria-hidden="true">
        ⚠
      </span>
      <div className="action-body">
        <h3 className="action-title">Action required</h3>
        <p className="action-text">
          {faultActive
            ? "Aurum has stopped all physical movement until somebody checks the machine."
            : `Aurum cannot start automatic sorting. ${row.label}: ${row.headline.toLowerCase()}.`}
        </p>
      </div>
      <div className="action-buttons">
        {faultActive ? (
          <>
            <button onClick={() => onHelp("fault")}>How to fix</button>
            <button className="is-primary" disabled={busy} onClick={onReset}>
              {busy === "fault" ? "Resetting…" : "Reset"}
            </button>
          </>
        ) : (
          <button className="is-primary" onClick={() => onHelp(row.key)}>
            How to fix
          </button>
        )}
      </div>
    </section>
  );
}

/**
 * The checklist for one subsystem, as a drawer.
 *
 * Steps are ordered by how often each one is the answer, and every one of them
 * is something the operator can physically do. "Check the logs" is not a step.
 */
export function HelpDrawer({ topic, row, onClose, onRetry, busy }) {
  const help = HELP[topic];
  const ref = useRef(null);
  useEffect(() => {
    ref.current?.focus();
    const key = (e) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", key);
    return () => window.removeEventListener("keydown", key);
  }, [onClose]);
  if (!help) return null;

  return (
    <div className="drawer-scrim" onClick={onClose}>
      <aside
        className="drawer"
        role="dialog"
        aria-label={`How to fix: ${help.title}`}
        tabIndex={-1}
        ref={ref}
        onClick={(e) => e.stopPropagation()}
      >
        <header className="drawer-head">
          <h3>{help.title}</h3>
          <button className="drawer-close" onClick={onClose} aria-label="Close">
            ✕
          </button>
        </header>

        {row && (
          <p className={`drawer-status level-${row.level}`}>
            <span aria-hidden="true">{MARK[row.level]}</span> {row.headline}
            {row.detail && <span className="drawer-status-detail"> — {row.detail}</span>}
          </p>
        )}

        <p className="drawer-need">{help.need}</p>

        <h4 className="drawer-sub">Check these, in order</h4>
        <ol className="drawer-steps">
          {help.steps.map((s) => (
            <li key={s}>{s}</li>
          ))}
        </ol>

        <div className="drawer-actions">
          <button className="is-primary" disabled={busy} onClick={onRetry}>
            {busy === "retry" ? "Checking…" : "Check again"}
          </button>
        </div>

        {row?.technical && (
          <details className="drawer-technical">
            <summary>Technical details</summary>
            <dl>
              {row.technical.map(([k, v]) => (
                <div key={k}>
                  <dt>{k}</dt>
                  <dd className="mono">{String(v)}</dd>
                </div>
              ))}
            </dl>
          </details>
        )}
      </aside>
    </div>
  );
}

// -- camera ----------------------------------------------------------------

export function CameraPanel({ state, api, onHelp }) {
  const err = state?.camera?.error;
  const running = state?.running;
  return (
    <section className="card camera-card">
      <div className="card-head">
        <h2 className="card-title">Live camera</h2>
        <span className={`inline-state ${err || !running ? "level-fault" : "level-ready"}`}>
          <span aria-hidden="true">{err || !running ? MARK.fault : MARK.ready}</span>
          {err ? "Not working" : running ? "Ready" : "Not started"}
        </span>
      </div>

      {running && !err ? (
        <img className="camera-feed" src={`${api}/session/stream`} alt="Live camera with detection overlay" />
      ) : (
        <div className="camera-feed is-empty">
          <p className="camera-empty-title">Camera offline</p>
          <p className="camera-empty-text">Aurum cannot identify objects without it.</p>
          <button className="is-primary" onClick={() => onHelp("camera")}>
            How to fix
          </button>
        </div>
      )}
    </section>
  );
}

// -- the object in hand ----------------------------------------------------

/** The six figures an operator acts on. No ppm, no reason codes, no ids in full. */
export function CurrentObject({ item, machine }) {
  const d = item?.decision;
  const identified = Boolean(item?.class_name);
  // Reason-specific, never one message for every refusal. A 10.8 g PCB against
  // a 20 g floor is a mass anomaly, and telling that operator to hold it
  // steadier for the camera sends them to the wrong end of the machine.
  const refused = refusal(d);
  const value = objectValue(item?.valuation);

  if (!item) {
    return (
      <section className="card object-card">
        <div className="card-head">
          <h2 className="card-title">Current object</h2>
        </div>
        <div className="object-empty">
          <p className="object-empty-title">Nothing being handled</p>
          <p className="object-empty-text">{machine.action}</p>
        </div>
      </section>
    );
  }

  const bin = d ? (d.physical_bin ?? d.decision) : null;

  return (
    <section className="card object-card">
      <div className="card-head">
        <h2 className="card-title">Current object</h2>
        <span className="object-id mono">{short(item.item_id)}</span>
      </div>

      {refused && (
        <div className="object-warning">
          <p className="object-warning-title">
            <span aria-hidden="true">⚠</span> {refused.title}
          </p>
          <p className="object-warning-what">{refused.what}</p>
          <p className="object-warning-todo">{refused.todo}</p>
        </div>
      )}

      <div className="object-hero">
        <span className="object-class" style={{ "--swatch": CLASS_COLOR[item.class_name] }}>
          {identified ? item.class_name : "Identifying…"}
        </span>
        {bin && (
          <span className={`bin-badge ${BIN_CLASS[bin] ?? "badge-c"}`}>
            <span className="bin-badge-label">Destination</span>
            <span className="bin-badge-value">BIN {bin}</span>
          </span>
        )}
      </div>

      <dl className="object-figures">
        <div>
          <dt>Weight</dt>
          <dd className="mono">{grams(item.weight_g)}</dd>
          <dd className="object-sub">{plain(item.weight_status) ?? "Not weighed"}</dd>
        </div>
        <div>
          <dt>Confidence</dt>
          <dd className="mono">{pct(item.confidence)}</dd>
          <dd className="object-sub">{identified ? "Camera match" : "—"}</dd>
        </div>
        <div>
          <dt>Value</dt>
          {/* `total_value`, not `total.value` - the earlier spelling rendered
              "--" on every object that had ever been graded. And it falls back
              to the precious half, because printing "--" over a real 94.96
              rupees of recoverable metal throws the answer away for the sake of
              the part that could not be priced. */}
          <dd className="mono">{value.text}</dd>
          <dd className="object-sub">{value.note ?? "—"}</dd>
        </div>
        <div>
          <dt>Outcome</dt>
          <dd>{machine.title}</dd>
          {/* An object routed to C is finished, not "in progress": no paddle
              was ever going to move for it, so there is no outcome field. */}
          <dd className="object-sub">
            {plain(item.actuation?.outcome) ??
              plain(item.actuation?.route?.status) ??
              "In progress"}
          </dd>
        </div>
      </dl>
    </section>
  );
}

// -- the process -----------------------------------------------------------

/** Seven stages. Click one to see what it actually found. */
export function Process({ item, pending }) {
  const [open, setOpen] = useState(null);
  const rows = stages(item, pending);
  return (
    <section className="card process-card">
      <div className="card-head">
        <h2 className="card-title">Process</h2>
        <span className="card-note">Every object goes through all seven, automatically</span>
      </div>
      <ol className="process-strip">
        {rows.map((s) => (
          <li key={s.key} className={`process-step is-${s.state}`}>
            <button
              onClick={() => setOpen(open === s.key ? null : s.key)}
              aria-expanded={open === s.key}
              aria-current={s.state === "active"}
            >
              <span className="process-mark" aria-hidden="true">
                {s.state === "done" ? "✓" : s.state === "failed" ? "✕" : s.state === "active" ? "●" : "○"}
              </span>
              <span className="process-label">{s.label}</span>
            </button>
            {open === s.key && <p className="process-detail">{s.detail}</p>}
          </li>
        ))}
      </ol>
    </section>
  );
}

// -- next sort -------------------------------------------------------------

export function NextSort({ routing, subsystemRows, onHelp }) {
  const next = (routing?.pending ?? [])[0];
  const conveyor = subsystemRows.find((r) => r.key === "conveyor");

  if (!next) {
    const blocked = conveyor && conveyor.level !== "ready";
    return (
      <section className="card next-card">
        <div className="card-head">
          <h2 className="card-title">Next sort</h2>
        </div>
        {blocked ? (
          <div className="next-empty">
            <p className="next-empty-title">Sorting timing unavailable</p>
            <p className="next-empty-text">{conveyor.detail}</p>
            <button className="is-primary" onClick={() => onHelp("conveyor")}>
              How to fix
            </button>
          </div>
        ) : (
          <div className="next-empty">
            <p className="next-empty-title">Nothing queued</p>
            <p className="next-empty-text">No object is on its way to a bin.</p>
          </div>
        )}
      </section>
    );
  }

  const left = next.seconds_remaining ?? 0;
  const total = next.geometry?.travel_time_s || 1;
  const progress = Math.max(0, Math.min(1, 1 - left / total));

  return (
    <section className="card next-card is-live">
      <div className="card-head">
        <h2 className="card-title">Next sort</h2>
        {routing.simulated && <span className="card-note">Simulated belt timing</span>}
      </div>
      <div className="next-body">
        <span className={`bin-badge is-large ${BIN_CLASS[next.decision] ?? "badge-c"}`}>
          <span className="bin-badge-value">BIN {next.decision}</span>
        </span>
        <div className="next-count">
          <span className="next-seconds mono">{Math.max(0, left).toFixed(1)}</span>
          <span className="next-unit">seconds</span>
        </div>
        <div className="next-meta">
          <span>{next.servo?.replace("SERVO_", "Paddle ") ?? "—"}</span>
          <span className="muted">{plain(next.status)}</span>
        </div>
      </div>
      <div
        className="next-progress"
        role="progressbar"
        aria-valuenow={Math.round(progress * 100)}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-label="Time until the paddle fires"
      >
        <span style={{ width: `${progress * 100}%` }} />
      </div>
    </section>
  );
}

// -- bins ------------------------------------------------------------------

export function Bins({ items, current }) {
  const routed = (items ?? []).filter((i) => i.decision);
  const of = (b) => routed.filter((i) => (i.decision.physical_bin ?? i.decision.decision) === b);
  const cards = [
    ["A", "Bin A", "High-value recovery"],
    ["B", "Bin B", "Smelting stream"],
    ["C", "Bin C", "Everything else"],
  ];
  return (
    <section className="card bins-card">
      <div className="card-head">
        <h2 className="card-title">Sorting bins</h2>
        <span className="card-note">{routed.length} sorted this run</span>
      </div>
      <div className="bin-grid">
        {cards.map(([key, label, note]) => {
          const rows = of(key);
          const mass = rows.reduce((t, i) => t + (i.weight_g ?? 0), 0);
          return (
            <div key={key} className={`bin-tile ${current === key ? "is-target" : ""}`}>
              <span className="bin-tile-name">{label}</span>
              <span className="bin-tile-count mono">{rows.length}</span>
              <span className="bin-tile-unit">object{rows.length === 1 ? "" : "s"}</span>
              <span className="bin-tile-mass mono">{rows.length ? grams(mass) : "—"}</span>
              {/* True while the object is on its way AND after it has landed,
                  which is the whole time this bin is the interesting one. */}
              <span className="bin-tile-note">
                {current === key ? "This object's bin" : note}
              </span>
            </div>
          );
        })}
      </div>
    </section>
  );
}

// -- system health ---------------------------------------------------------

export function SystemHealth({ rows, onHelp }) {
  return (
    <section className="card health-card">
      <div className="card-head">
        <h2 className="card-title">System health</h2>
      </div>
      <ul className="health-list">
        {rows.map((r) => (
          <li key={r.key} className={`health-row level-${r.level}`}>
            <span className="health-mark" aria-hidden="true">
              {MARK[r.level]}
            </span>
            <span className="health-label">{r.label}</span>
            <span className="health-state">{r.headline}</span>
            <span className="health-detail">{r.detail}</span>
            {r.level !== "ready" && HELP[r.key] && (
              <button className="health-fix" onClick={() => onHelp(r.key)}>
                How to fix
              </button>
            )}
          </li>
        ))}
      </ul>
    </section>
  );
}

// -- what happened ---------------------------------------------------------

export function Activity({ state }) {
  const events = activity(state);
  return (
    <section className="card activity-card">
      <div className="card-head">
        <h2 className="card-title">Recent activity</h2>
      </div>
      {events.length === 0 ? (
        <p className="activity-empty">Nothing has happened yet this run.</p>
      ) : (
        <ul className="activity-list">
          {events.map((e, i) => (
            <li key={i} className={e.ok ? "is-ok" : "is-bad"}>
              <span className="activity-mark" aria-hidden="true">
                {e.ok ? "✓" : "⚠"}
              </span>
              <span className="activity-time mono">{clock(e.at)}</span>
              <span className="activity-text">{e.text}</span>
              {e.technical && <span className="activity-tech">{e.technical}</span>}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

// -- startup ---------------------------------------------------------------

/**
 * The first ten seconds.
 *
 * A wall of UNAVAILABLE is the worst possible first impression, and it is also
 * misleading: at that moment most of it is simply "not started yet", which is
 * a different thing from "broken".
 */
export function Startup({ startup, onRetry, onHelp, busy }) {
  return (
    <div className="startup">
      <div className="startup-inner">
        <h1 className="wordmark startup-wordmark">AURUM</h1>
        <p className="startup-sub">Automatic identification &amp; sorting</p>

        <h2 className={`startup-title ${startup.phase === "failed" ? "is-failed" : ""}`}>
          {startup.phase === "failed" ? "Aurum needs attention" : "Starting Aurum"}
        </h2>
        <p className="startup-reason">{startup.reason}</p>

        <ul className="startup-checks">
          {startup.checks.map((c) => (
            <li key={c.name} className={`is-${c.state}`}>
              <span className="startup-mark" aria-hidden="true">
                {c.state === "ok" ? "✓" : c.state === "busy" ? "●" : c.state === "bad" ? "✕" : "○"}
              </span>
              <span className="startup-name">{c.name}</span>
              <span className="startup-detail">{c.detail}</span>
            </li>
          ))}
        </ul>

        {startup.phase === "failed" && (
          <div className="startup-actions">
            {startup.topic && (
              <button onClick={() => onHelp(startup.topic)}>How to fix</button>
            )}
            <button className="is-primary" disabled={busy} onClick={onRetry}>
              {busy === "retry" ? "Retrying…" : "Retry"}
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
