import { useCallback, useEffect, useState } from "react";

const API = import.meta.env.VITE_AURUM_API ?? "http://127.0.0.1:8000";

// The chain is meant to be watched, so it is polled fast enough that a judge
// sees the mass and the bin land rather than a table that refreshes later.
const POLL_MS = 400;

const CLASS_COLOR = {
  PCB: "var(--cls-pcb)",
  RAM: "var(--cls-ram)",
  CPU: "var(--cls-cpu)",
  Connector: "var(--cls-connector)",
};

const BIN_CLASS = { A: "badge-a", B: "badge-b", C: "badge-c" };

/** The automatic cycle, in the operator's words rather than the enum's. */
const PAN_STATE = {
  WAITING_FOR_OBJECT: ["Waiting for an object", "neutral"],
  OBJECT_PRESENT: ["Object detected on the pan", "warn"],
  WEIGHING: ["Measuring…", "warn"],
  WEIGHT_STABLE: ["Weight stable", "good"],
  PROCESSING: ["Estimating and deciding", "warn"],
  ROUTING: ["Routing", "warn"],
  WAITING_FOR_CLEAR: ["Remove the object", "warn"],
};

/**
 * The state machine, front and centre.
 *
 * This is what replaced the "Measure & route" button. The operator reads the
 * machine here rather than driving it: nothing on this panel is clickable.
 */
function PanBanner({ pan, automatic }) {
  if (!pan) return null;
  const [label, tone] = PAN_STATE[pan.state] ?? [pan.state, "neutral"];
  return (
    <div className={`glass-panel pan pan-${tone}`}>
      <div className="pan-head">
        <span className="field-label">System</span>
        <span className={automatic ? "badge-b" : "badge-c"}>
          {automatic ? "AUTOMATIC" : "MANUAL — pan machine not running"}
        </span>
      </div>
      <div className="pan-state">{label}</div>
      <div className="pan-meta mono">
        {pan.grams == null ? "— g" : `${pan.grams.toFixed(1)} g`} ·{" "}
        {pan.cycles_completed} handled · {pan.seconds_in_state}s in state
      </div>
      {pan.reason && <div className="stage-note">{pan.reason}</div>}
    </div>
  );
}

/** What the camera found ON this object. Absence is never shown as zero. */
function Inventory({ components }) {
  const entries = Object.entries(components ?? {});
  if (!entries.length) return <span className="muted">nothing detected</span>;
  return (
    <span className="inventory">
      {entries.map(([cls, n]) => (
        <span
          key={cls}
          className="chip"
          style={{ "--swatch": CLASS_COLOR[cls] }}
        >
          {cls} × {n}
        </span>
      ))}
    </span>
  );
}

const pct = (c) => (c == null ? "--" : `${(c * 100).toFixed(1)}%`);
const grams = (g) => (g == null ? "--" : `${g.toFixed(1)} g`);
const num = (v, digits = 4) => (v == null ? "--" : Number(v).toFixed(digits));

/** Money, or an explicit absence. Never renders a missing figure as zero. */
const money = (v, currency) =>
  v == null
    ? null
    : `${currency === "INR" ? "₹" : `${currency ?? ""} `}${Number(v).toLocaleString(
        undefined,
        { minimumFractionDigits: 2, maximumFractionDigits: 2 },
      )}`;

/** Milligrams where that reads better than a long decimal of grams. */
const metalMass = (grams) =>
  grams == null
    ? "--"
    : grams < 1
      ? `${(grams * 1000).toFixed(2)} mg`
      : `${grams.toFixed(3)} g`;

async function call(path, method = "GET") {
  const res = await fetch(`${API}${path}`, { method });
  if (!res.ok) throw new Error(`${path} -> HTTP ${res.status}`);
  return res.json();
}

/** A status pill that never reads green unless the thing is actually true. */
function Pill({ ok, label, detail }) {
  return (
    <span className={ok ? "badge-b" : "badge-c"} title={detail ?? ""}>
      {ok && <span className="dot" />}
      {label}
    </span>
  );
}

/** One row of the chain. `state` drives the colour, never the value's presence. */
function Stage({ n, title, value, note, state = "neutral" }) {
  return (
    <div className={`stage stage-${state}`}>
      <div className="stage-n">{n}</div>
      <div className="stage-body">
        <div className="stage-title">{title}</div>
        <div className="stage-value">{value}</div>
        {note && <div className="stage-note">{note}</div>}
      </div>
    </div>
  );
}

/**
 * The mass, with the one distinction the whole estimate rests on.
 *
 * MEASURED means a settled reading on a calibration verified against a second
 * known mass. STABLE means it settled on a factor nobody checked, and it is
 * shown differently because a concentration estimate refuses it.
 */
function massState(status) {
  if (status === "MEASURED") return "good";
  if (status === "STABLE" || status === "UNSTABLE") return "warn";
  return "bad";
}

function metalRows(pmdi) {
  if (!pmdi?.available) return [];
  return [
    ...Object.entries(pmdi.precious_metals ?? {}).map(([m, a]) => [
      m,
      a,
      "precious",
    ]),
    ...Object.entries(pmdi.base_metals ?? {}).map(([m, a]) => [m, a, "base"]),
    ...Object.entries(pmdi.other_metals ?? {}).map(([m, a]) => [m, a, "other"]),
  ];
}

/**
 * One provenance word, rendered so its meaning cannot be mistaken.
 *
 * The whole dashboard rests on this distinction. MEASURED, CALIBRATED, LIVE
 * and VALIDATED are things the machine established. SIMULATED, REFERENCE and
 * MANUAL are real numbers that are not measurements of THIS object right now.
 * STALE, UNAVAILABLE, UNKNOWN, UNCALIBRATED and FAILED are absences. A colour
 * per group, and never a bare number without one of these beside it.
 */
const STATUS_CLASS = {
  MEASURED: "badge-b",
  CALIBRATED: "badge-b",
  VALIDATED: "badge-b",
  LIVE: "badge-b",
  ACKED: "badge-b",
  SIMULATED: "badge-a",
  REFERENCE: "badge-a",
  MANUAL: "badge-a",
  TEST: "badge-a",
  ESTIMATED: "badge-a",
  APPROXIMATE: "badge-a",
  STALE: "badge-c",
  UNSTABLE: "badge-c",
  PARTIAL: "badge-c",
  UNKNOWN: "badge-c",
  UNCALIBRATED: "badge-c",
  UNAVAILABLE: "badge-none",
  NONE: "badge-none",
  FAILED: "badge-bad",
  ERROR: "badge-bad",
  TIMED_OUT: "badge-bad",
};

function Status({ value, title }) {
  if (value == null) return <span className="badge-none">--</span>;
  const text = String(value);
  return (
    <span className={STATUS_CLASS[text] ?? "badge-none"} title={title ?? ""}>
      {text}
    </span>
  );
}

function Row({ label, children }) {
  return (
    <div className="field-row">
      <span className="field-label">{label}</span>
      <span className="field-value">{children}</span>
    </div>
  );
}

const seconds = (v) => (v == null ? "--" : `${v.toFixed(2)} s`);

/**
 * A secondary panel: folded away, never removed.
 *
 * `headline` is what the closed panel still says out loud — the one fact worth
 * seeing without opening it. Everything the panel used to print stays in the
 * DOM, which is the point: this dashboard's whole claim is that it shows its
 * working, so the fix for a crowded screen is disclosure, not deletion.
 */
function Panel({ title, headline, children }) {
  return (
    <details className="glass-panel panel">
      <summary>
        <span className="section-title">{title}</span>
        {headline != null && <span className="panel-headline">{headline}</span>}
      </summary>
      <div className="panel-body">{children}</div>
    </details>
  );
}

/**
 * The belt. `NONE` is a fact about this machine, not a missing setting: there
 * is no conveyor, the operator carries the object, and routing is immediate.
 */
function ConveyorPanel({ conveyor }) {
  const speed = conveyor?.speed ?? {};
  const geometry = conveyor?.geometry ?? {};
  return (
    <Panel
      title="Conveyor"
      headline={<Status value={conveyor?.present ? conveyor.mode : "NONE"} />}
    >
      <Row label="Mode">
        <Status value={conveyor?.present ? conveyor.mode : "NONE"} />
      </Row>
      <Row label="Speed source">{conveyor?.speed_source ?? "--"}</Row>
      <Row label="Speed">
        {speed.m_s == null ? "--" : `${speed.m_s.toFixed(2)} m/s`}{" "}
        <Status value={speed.status} title={speed.reason} />
      </Row>
      <Row label="Encoder">
        {conveyor?.encoder
          ? `${conveyor.encoder.updates} samples, ${
              conveyor.encoder.healthy ? "healthy" : "not reporting"
            }`
          : "not fitted"}
      </Row>
      <Row label="Camera to servo A">
        {geometry.camera_to_servo_a_cm == null
          ? "UNMEASURED"
          : `${geometry.camera_to_servo_a_cm} cm`}
      </Row>
      <Row label="ETA to servo A">{seconds(conveyor?.eta_to_servo_a_s)}</Row>
      <Row label="ETA to servo B">{seconds(conveyor?.eta_to_servo_b_s)}</Row>
      <p className="panel-note">{conveyor?.note}</p>
    </Panel>
  );
}

/** Metal prices, each with whether it may be presented as current. */
function PricingPanel({ pricing }) {
  const metals = pricing?.metals ?? {};
  const order = ["Au", "Ag", "Pd", "Cu"];
  const headline = metals.Au?.status;
  return (
    <Panel title="Metal prices" headline={<Status value={headline} />}>
      <Row label="Provider">{pricing?.provider ?? "--"}</Row>
      {order
        .filter((m) => metals[m])
        .map((metal) => {
          const quote = metals[metal];
          return (
            <Row key={metal} label={`${metal} per gram`}>
              {quote.price_per_gram == null
                ? "--"
                : money(quote.price_per_gram, quote.currency)}{" "}
              <Status value={quote.status} title={quote.reason} />
            </Row>
          );
        })}
      <p className="panel-note">
        LIVE is a market feed answering now. REFERENCE is a real published price
        being used after its date. STALE is a feed that should have been current
        and was not. Nothing here is ever a number without a source.
      </p>
    </Panel>
  );
}

/** The board, the paddles, and whether a fault is holding the machine. */
function HardwarePanel({ hardware, board, actuation, onReset, busy }) {
  const fault = hardware?.fault ?? {};
  const servo = hardware?.servo ?? {};
  const last = actuation?.last_command;
  return (
    <Panel
      title="Hardware"
      headline={
        fault.active ? (
          <Status value="FAILED" title={fault.code} />
        ) : (
          <Status
            value={hardware?.mode === "SIMULATION" ? "SIMULATED" : "LIVE"}
          />
        )
      }
    >
      <Row label="Mode">
        <Status
          value={
            hardware?.mode == null
              ? null
              : hardware.mode === "SIMULATION"
                ? "SIMULATED"
                : "MEASURED"
          }
          title={`HARDWARE_MODE=${hardware?.mode}`}
        />
      </Row>
      <Row label="Arduino">
        {board?.connected ? board.port : "not connected"}{" "}
        <Status
          value={
            !board?.connected
              ? "UNAVAILABLE"
              : hardware?.mode === "SIMULATION"
                ? "SIMULATED"
                : "LIVE"
          }
        />
      </Row>
      <Row label="Actuation">
        <Status value={hardware?.actuation_enabled ? "LIVE" : "NONE"} />
      </Row>
      <Row label="Servo A / B">
        {servo.rest_angle_deg == null
          ? "--"
          : `rest ${servo.rest_angle_deg}, push ${servo.push_angle_deg}, hold ${servo.actuation_ms} ms`}
      </Row>
      <Row label="Last command">
        {last ? `${last.servo ?? last.target} ` : "none "}
        <Status value={last?.state} title={last?.reason} />
      </Row>
      <Row label="Hardware fault">
        {fault.active ? (
          <>
            <Status value="FAILED" title={fault.reason} /> {fault.code}
          </>
        ) : (
          <Status value="LIVE" title="No latched fault." />
        )}
      </Row>
      {/* The reason itself is on the top banner; repeating it here made the
          same sentence appear twice on screen and once more as a tooltip. */}
      {fault.active && (
        <button disabled={busy} onClick={onReset}>
          {busy === "fault" ? "Resetting..." : "Reset hardware fault"}
        </button>
      )}
      {/* The ACK caveat lives on the servo stage of the chain, next to the
          badge it is warning about, rather than here where nothing shows it. */}
    </Panel>
  );
}

/** What went wrong this run. A recorded failure is not a crash. */
function UpcomingQueue({ routing }) {
  // Null means there is no belt, which is the shipped configuration. An empty
  // queue panel on a machine that can never queue anything is furniture.
  if (!routing) return null;
  const pending = routing.pending ?? [];
  return (
    <section className="glass-panel">
      <h2 className="section-title">Upcoming</h2>
      <p className="section-note">
        Items whose moment has been computed but has not arrived. A scheduled
        route is a time, not a movement.
        {routing.simulated && (
          <span className="badge-c"> SIMULATED GEOMETRY</span>
        )}
      </p>
      <table className="ledger">
        <thead>
          <tr>
            <th>Item</th>
            <th>Class</th>
            <th>Bin</th>
            <th>Servo</th>
            <th>Fires in</th>
          </tr>
        </thead>
        <tbody>
          {pending.map((r) => (
            <tr key={r.item_id}>
              <td className="mono small">{r.item_id}</td>
              <td>{r.component_class ?? "—"}</td>
              <td>
                <span className={BIN_CLASS[r.decision] ?? "badge-c"}>
                  {r.decision}
                </span>
              </td>
              <td className="mono small">{r.servo ?? "—"}</td>
              <td className="mono">{seconds(r.seconds_remaining)}</td>
            </tr>
          ))}
          {pending.length === 0 && (
            <tr>
              <td colSpan={5} className="muted">
                Nothing waiting.
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </section>
  );
}

function ErrorsPanel({ errors }) {
  const recent = errors?.recent ?? [];
  if (!recent.length) return null;
  const codes = Object.entries(errors.by_code ?? {})
    .map(([code, n]) => `${code} ${n}`)
    .join(" · ");
  return (
    <Panel
      title="Recorded failures"
      headline={<span className="mono small">{errors.count} this run</span>}
    >
      <p className="section-note">{codes}</p>
      <ul className="errors-list">
        {recent.slice(0, 8).map((e, i) => (
          <li key={i}>
            <Status value="FAILED" />{" "}
            <span className="mono small">{e.error_code}</span>{" "}
            <span className="muted small">{e.stage}</span>
            {e.item_id && <span className="mono small"> {e.item_id}</span>}
            <div className="muted small">{e.message}</div>
          </li>
        ))}
      </ul>
      <p className="panel-note">{errors.note}</p>
    </Panel>
  );
}

/** The chain for one item, stage by stage, in the order a judge watches it. */
function ItemChain({ item }) {
  if (!item) {
    return (
      <div className="notice neutral">
        No confirmed item. Hold a component in front of the camera until it is
        <strong> CONFIRMED</strong> — an object seen once is not yet something
        to weigh.
      </div>
    );
  }

  const v = item.valuation;
  const pmdi = v?.pmdi;
  const d = item.decision;
  const act = item.actuation;
  const metals = metalRows(pmdi);

  // Nothing after the camera has run until the operator presses the button.
  // Those stages are PENDING, not failed: rendering them red made a perfectly
  // healthy detection look like a stack of errors.
  const graded = Boolean(d);
  const pending = (label = "waiting") => <span className="muted">{label}</span>;

  return (
    <>
      <div className="item-head">
        <div>
          <div className="field-label">Item</div>
          <div className="item-id">{item.item_id}</div>
        </div>
        {d && (
          <div className="bin-block">
            <div className="field-label">Destination</div>
            <span className={`${BIN_CLASS[d.decision]} bin-big`}>
              BIN {d.decision}
            </span>
          </div>
        )}
      </div>

      {!graded && (
        <div className="notice">
          <strong>Detected and confirmed.</strong> Place it on the pan. The load
          cell starts the measurement by itself — there is nothing to press.
        </div>
      )}

      <Stage
        n="1"
        title="Vision"
        value={
          <>
            <span
              className="chip"
              style={{ "--swatch": CLASS_COLOR[item.class_name] }}
            >
              {item.class_name ?? "unknown"}
            </span>
            <span className="mono"> {pct(item.confidence)}</span>
          </>
        }
        note={`${item.detection_count} observations · confidence is the weakest member's mean`}
        state={item.class_name ? "good" : "bad"}
      />

      <Stage
        n="1b"
        title={item.is_assembly ? "Assembly — components on this object" : "Single component"}
        value={<Inventory components={item.components} />}
        note={
          item.is_assembly
            ? "One physical object, one id, one mass. Only components actually detected are listed — nothing is recorded as absent."
            : "Not sitting on a board, so it is its own object."
        }
        state="neutral"
      />

      <Stage
        n="2"
        title="Mass — HX711"
        value={
          graded ? (
            <>
              <span className="mono big">{grams(item.weight_g)}</span>{" "}
              <span
                className={
                  massState(item.weight_status) === "good"
                    ? "badge-b"
                    : "badge-c"
                }
              >
                {item.weight_status}
              </span>
            </>
          ) : (
            pending("not weighed yet")
          )
        }
        note={graded ? item.weight_reading?.reason : null}
        state={graded ? massState(item.weight_status) : "neutral"}
      />

      <Stage
        n="3"
        title="Material evidence"
        value={
          pmdi?.available ? (
            <span className="mono">{pmdi.evidence_sources.join(", ")}</span>
          ) : graded ? (
            <span className="muted">unavailable</span>
          ) : (
            pending()
          )
        }
        note={
          pmdi?.available
            ? `cited composition, confidence ${pmdi.confidence ?? "--"} — contained, not recoverable`
            : graded
              ? pmdi?.reason
              : null
        }
        state={pmdi?.available ? "good" : graded ? "bad" : "neutral"}
      />

      {pmdi?.completeness && pmdi.completeness !== "COMPLETE" && (
        <div className="notice">
          <strong>
            {pmdi.completeness === "PARTIAL_ESTIMATE"
              ? "PARTIAL ESTIMATE"
              : "INSUFFICIENT EVIDENCE"}
          </strong>{" "}
          — the figures below cover only part of this object.
          <table className="metals">
            <tbody>
              {(pmdi.valued ?? []).map((v) => (
                <tr key={`v-${v.component}`}>
                  <td className="mono">
                    {v.component} × {v.count}
                  </td>
                  <td>
                    <span className="badge-b">VALUED</span>
                  </td>
                  <td className="muted small">{v.metals.join(", ")}</td>
                </tr>
              ))}
              {(pmdi.not_valued ?? []).map((n) => (
                <tr key={`n-${n.component}`}>
                  <td className="mono">
                    {n.component} × {n.count}
                  </td>
                  <td>
                    <span className="badge-c">NOT VALUED</span>
                  </td>
                  <td className="muted small">{n.reason}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {metals.length > 0 && (
        <table className="metals">
          <thead>
            <tr>
              <th>Metal</th>
              <th>Contained</th>
              <th>Price</th>
              <th>Value</th>
              <th>Basis</th>
              <th>Evidence</th>
            </tr>
          </thead>
          <tbody>
            {metals.map(([metal, amount, kind]) => {
              const price = v?.prices?.[metal];
              const cash =
                price?.price_per_gram == null
                  ? null
                  : money(amount.grams * price.price_per_gram, price.currency);
              return (
                <tr key={metal} className={kind === "precious" ? "precious" : ""}>
                  <td className="mono">{metal}</td>
                  <td className="mono">{metalMass(amount.grams)}</td>
                  <td className="mono small">
                    {price?.price_per_gram == null ? (
                      <span className="badge-c">NO PRICE</span>
                    ) : (
                      `${money(price.price_per_gram, price.currency)}/g`
                    )}
                  </td>
                  <td className="mono">
                    {cash ?? <span className="muted">not priced</span>}
                  </td>
                  <td className="muted small">{amount.calculation}</td>
                  <td className="mono small">{amount.evidence.join(", ")}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}

      <Stage
        n="4"
        title="PMDI"
        value={
          pmdi?.available ? (
            <>
              <span className="mono big">
                {num(pmdi.precious_mass_fraction_ppm, 1)}
              </span>
              <span className="muted"> ppm precious</span>
              <span className="mono">
                {" "}
                · {num(pmdi.precious_mass_g, 6)} g of {num(pmdi.mass_g, 1)} g
              </span>
            </>
          ) : graded ? (
            <span className="muted">unavailable</span>
          ) : (
            pending()
          )
        }
        note="PMDI = (sum(C_type x Y_estimated)) x P_spot — the ppm figure needs no price"
        state={pmdi?.available ? "good" : graded ? "bad" : "neutral"}
      />

      <Stage
        n="5"
        title="Estimated CONTAINED value"
        value={
          v?.contained_value != null ? (
            <>
              <span className="mono big">
                {money(v.contained_value, v.currency)}
              </span>
              <span className="muted"> contained</span>
            </>
          ) : graded ? (
            <span className="badge-c">NO PRICE SOURCE</span>
          ) : (
            pending()
          )
        }
        note={
          graded
            ? (v?.reason ??
              "What the cited assays say is PRESENT — not what a process would recover, and not what a recycler would pay.")
            : null
        }
        state={v?.contained_value != null ? "good" : graded ? "warn" : "neutral"}
      />

      {graded && v?.recoverable_value && (
        <Stage
          n="5b"
          title="Estimated RECOVERABLE value"
          value={
            v.recoverable_value.available ? (
              <span className="mono big">
                {money(v.recoverable_value.value, v.recoverable_value.currency)}
              </span>
            ) : (
              <span className="badge-c">NOT SUPPORTED BY CURRENT EVIDENCE</span>
            )
          }
          note={v.recoverable_value.reason}
          state={v.recoverable_value.available ? "good" : "warn"}
        />
      )}

      {/* The per-metal price table lives in the Metal prices panel, which shows
          the same figures, statuses and sources for the whole run rather than
          repeating them under every item. The status that matters to THIS
          valuation is already on stage 5 beside the number it produced. */}

      <Stage
        n="6"
        title="Decision"
        value={
          d ? (
            <>
              <span className={BIN_CLASS[d.decision]}>BIN {d.decision}</span>
              <span className="mono"> {d.reason_code}</span>
            </>
          ) : (
            pending("not yet graded")
          )
        }
        note={d?.reason}
        state={d ? "good" : "neutral"}
      />

      <Stage
        n="7"
        title="Actuator"
        value={
          act ? (
            act.commanded ? (
              <>
                <span className="mono big">{act.servo}</span>{" "}
                <span className={act.state === "ACKED" ? "badge-b" : "badge-c"}>
                  {act.state}
                </span>
              </>
            ) : (
              <span className="badge-c">NO SERVO</span>
            )
          ) : (
            pending("no command yet")
          )
        }
        note={act?.reason}
        state={
          act?.state === "ACKED" ? "good" : act?.commanded ? "bad" : "neutral"
        }
      />

      {act?.state === "ACKED" && (
        <div className="notice neutral">
          <strong>ACKED</strong> means the board reports it completed the
          stroke. It is not evidence that the servo physically moved — watch the
          paddle, not this badge.
        </div>
      )}
    </>
  );
}

export default function App() {
  const [state, setState] = useState(null);
  const [health, setHealth] = useState(null);
  // Two kinds of error, and they must not share a slot. `error` is the backend
  // being unreachable, and the poll below owns it: it appears and clears on its
  // own. `actionError` is the backend refusing something the operator asked
  // for, with a reason worth reading, so it survives until the next action.
  //
  // They were one state until 2026-08-26, and act() set it and then immediately
  // called load(), which cleared it on success. Every refusal the backend
  // explains carefully - ALREADY_PROCESSED, NO_ITEM, UNKNOWN_ITEM - rendered as
  // a button that did nothing at all.
  const [error, setError] = useState(null);
  const [actionError, setActionError] = useState(null);
  const [busy, setBusy] = useState(null);
  const [lastResult, setLastResult] = useState(null);

  const load = useCallback(async () => {
    try {
      setState(await call("/session"));
      setError(null);
    } catch (e) {
      setError(e.message);
    }
  }, []);

  useEffect(() => {
    call("/health")
      .then(setHealth)
      .catch(() => {});
  }, []);

  useEffect(() => {
    load();
    const id = setInterval(load, POLL_MS);
    return () => clearInterval(id);
  }, [load]);

  const act = async (label, path) => {
    setBusy(label);
    setActionError(null);
    try {
      const out = await call(path, "POST");
      setLastResult(out);
      // A refusal is the machine explaining itself, not a crash. Keep it.
      setActionError(out.error ? `${out.error} — ${out.reason}` : null);
      await load();
    } catch (e) {
      setActionError(e.message);
    } finally {
      setBusy(null);
    }
  };

  const reset = async () => {
    await act("reset", "/track/reset");
    setLastResult(null);
  };

  const running = state?.running;
  const board = state?.board ?? {};
  const actuation = state?.actuation ?? {};
  const cal = state?.calibration ?? {};
  const hardware = state?.hardware ?? {};
  const fault = hardware.fault ?? {};
  const processed = (state?.items ?? []).filter((i) => i.decision);

  return (
    <div className="shell">
      <header className="glass-panel masthead">
        <div>
          <h1 className="wordmark">AURUM</h1>
          <p className="tagline">
            Identification · Measurement · Recovery routing
          </p>
        </div>
        <div className="pills">
          <Pill
            ok={running}
            label={running ? "CAMERA LIVE" : "CAMERA OFF"}
            detail={state?.camera?.error}
          />
          <Pill
            ok={board.connected}
            label={board.connected ? "BOARD LINKED" : "NO BOARD"}
            detail={board.last_error}
          />
          <Pill
            ok={cal.verified}
            label={cal.verified ? "CALIBRATED" : "NOT CALIBRATED"}
            detail="MEASURED needs a factor verified against a second known mass"
          />
          <Pill
            ok={actuation.actuation_enabled}
            label={
              actuation.actuation_enabled ? "ACTUATION ON" : "ACTUATION OFF"
            }
          />
          {state?.mock_mass?.enabled && (
            <span className="badge-c" title={state.mock_mass.note}>
              MOCK MASS
            </span>
          )}
        </div>
      </header>

      <div className="glass-panel controls">
        <button disabled={busy} onClick={() => act("camera", "/session/start")}>
          {busy === "camera" ? "Starting…" : "Start camera"}
        </button>
        <button
          disabled={busy}
          onClick={() => act("board", "/session/board/connect")}
        >
          {busy === "board" ? "Connecting…" : "Connect board"}
        </button>
        <button disabled={busy} onClick={() => act("stop", "/session/stop")}>
          Stop
        </button>
        <button disabled={busy} onClick={reset}>
          {busy === "reset" ? "Resetting…" : "New item / reset run"}
        </button>
        {/* Deliberately not disabled while another action is in flight: the
            one button that must work when the machine is busy is this one. */}
        <button
          className="estop"
          onClick={() => act("estop", "/hardware/estop")}
          title="Latches the machine. Every servo command is refused until a human resets it."
        >
          {busy === "estop" ? "Stopping…" : "EMERGENCY STOP"}
        </button>
        <span className="controls-note">
          Place the object on the pan and wait. The load cell starts the
          measurement, the model gives the classes, the decision engine gives
          the bin. Nobody here says which bin. Swapped the object? Press
          <strong> New item</strong> — one physical item gets one physical
          action, so the machine will not route the same one twice.
        </span>
      </div>

      <details className="glass-panel controls">
        <summary>Developer controls</summary>
        <p className="controls-note">
          Not the normal path. The load cell triggers a measurement on its own;
          these exist for a bench with no working cell or a mass that will not
          settle.
        </p>
        <button
          disabled={busy}
          onClick={() => act("measure", "/session/measure")}
        >
          {busy === "measure" ? "Measuring…" : "Measure & route now (manual)"}
        </button>
      </details>

      {actionError && <div className="notice bad">{actionError}</div>}

      {/* The masthead badge is the standing signal and every derived figure is
          stamped SIMULATED at the point it is shown, so the full explanation
          folds away rather than occupying four lines of every screenshot. */}
      {state?.mock_mass?.enabled && (
        <details className="notice">
          <summary>
            <strong>MOCK MASS — the mass is assumed, not measured.</strong>
          </summary>
          The load cell cannot supply one, so every figure derived from it is
          stamped SIMULATED. The class, the cited composition and the bin are
          real; the mass is not.
          {state.mock_mass.per_class && (
            <span className="mono small">
              {" "}
              Assumed per class:{" "}
              {Object.entries(state.mock_mass.per_class)
                .map(([cls, g]) => `${cls} ${g} g`)
                .join(" · ")}
            </span>
          )}
        </details>
      )}
      {fault.active && (
        <div className="notice bad">
          <strong>
            {fault.code === "EMERGENCY_STOP"
              ? "EMERGENCY STOP — the machine is latched."
              : `HARDWARE FAULT LATCHED — ${fault.code}.`}
          </strong>{" "}
          {fault.reason}{" "}
          {fault.code === "EMERGENCY_STOP"
            ? "Every servo command is refused. Clearing this is a statement that somebody has looked at the rig."
            : "No servo will move until it is reset. Nothing clears this on its own: a command that went unacknowledged may have left a paddle half out, and the machine does not know where it is."}
          {/* The reset lives in the maintenance panel too. It is repeated here
              because a latched machine is exactly when nobody wants to go
              looking for the button that unlatches it. */}
          <button
            className="inline-action"
            disabled={busy}
            onClick={() => act("fault", "/hardware/fault/reset")}
          >
            {busy === "fault" ? "Resetting…" : "Reset fault"}
          </button>
        </div>
      )}
      <PanBanner pan={state?.pan} automatic={state?.automatic} />

      {error && <div className="notice bad">{error}</div>}
      {state?.camera?.error && (
        <div className="notice bad">Camera: {state.camera.error}</div>
      )}

      <div className="split">
        <section className="glass-panel feed">
          <h2 className="section-title">Camera</h2>
          <p className="section-note">
            {state?.camera?.source ?? "not started"} ·{" "}
            {state?.frames_processed ?? 0} frames
          </p>
          {running ? (
            <img
              className="stream"
              src={`${API}/session/stream`}
              alt="Live detection feed"
            />
          ) : (
            <div className="stream placeholder">Camera not started</div>
          )}
          {/* The belt's own note is in the Conveyor panel. It was printed here
              too, so the same three sentences appeared twice on one screen. */}
        </section>

        <section className="glass-panel chain">
          <h2 className="section-title">Current item</h2>
          <p className="section-note">
            Model {health?.model_version ?? "--"} ·{" "}
            {state?.confirmed_count ?? 0} confirmed in view
          </p>
          <ItemChain
            item={lastResult?.item_id ? lastResult : state?.current_item}
          />
        </section>
      </div>

      {/* Reference, not narrative: below the thing being watched, and folded.
          Each summary still carries the one state worth seeing at a glance. */}
      <div className="systems">
        <ConveyorPanel conveyor={state?.conveyor} />
        <PricingPanel pricing={state?.pricing} />
        <HardwarePanel
          hardware={hardware}
          board={board}
          actuation={actuation}
          busy={busy}
          onReset={() => act("fault", "/hardware/fault/reset")}
        />
      </div>

      <section className="glass-panel">
        <h2 className="section-title">Routed this run</h2>
        <p className="section-note">
          One physical item, one identity, one movement. {processed.length}{" "}
          processed.
        </p>
        <table className="ledger">
          <thead>
            <tr>
              <th>Item</th>
              <th>Class</th>
              <th>Conf.</th>
              <th>Mass</th>
              <th>ppm</th>
              <th>Decision</th>
              <th>Servo</th>
              <th>Why</th>
            </tr>
          </thead>
          <tbody>
            {processed.map((i) => (
              <tr key={i.item_id}>
                <td className="mono small">{i.item_id}</td>
                <td>
                  <span
                    className="chip"
                    style={{ "--swatch": CLASS_COLOR[i.class_name] }}
                  >
                    {i.class_name}
                  </span>
                </td>
                <td className="mono">{pct(i.confidence)}</td>
                <td className="mono">
                  {grams(i.weight_g)}{" "}
                  <span className="muted small">{i.weight_status}</span>
                </td>
                <td className="mono">
                  {num(i.valuation?.pmdi?.precious_mass_fraction_ppm, 1)}
                </td>
                <td>
                  <span
                    className={BIN_CLASS[i.decision.decision] ?? "badge-c"}
                    title={i.decision.decision_note ?? i.decision.reason}
                  >
                    {i.decision.decision}
                  </span>
                  {/* The grade and the bin the paddle actually used are the
                      same value except on a physical fallback. Shown only when
                      they diverge, which is the one case worth the column. */}
                  {i.decision.physical_bin &&
                    i.decision.physical_bin !== i.decision.decision && (
                      <span className="muted small">
                        {" "}
                        → {i.decision.physical_bin}
                      </span>
                    )}
                </td>
                <td className="mono small">{i.actuation?.servo ?? "—"}</td>
                <td className="muted small">{i.decision.reason_code}</td>
              </tr>
            ))}
            {processed.length === 0 && (
              <tr>
                <td colSpan={9} className="muted">
                  Nothing routed yet.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </section>

      <UpcomingQueue routing={state?.routing} />

      <ErrorsPanel errors={state?.errors} />

      {state?.epr && (
        <Panel
          title="EPR record"
          headline={<span className="mono small">{state.epr.session_id}</span>}
        >
          <p className="section-note">
            Every item's whole trail — detected, classified, weighed, valued,
            binned, actuated — is written to the EPR ledger with the provenance
            below stamped on each event.
            <span className="mono small"> GET /epr/&lt;item_id&gt;</span>
          </p>
          <div className="trail">
            <span>DETECTED</span>
            <span>CLASSIFIED</span>
            <span>WEIGHED</span>
            <span>COMPOSITION</span>
            <span>PMDI</span>
            <span>VALUE</span>
            <span>BIN</span>
            <span>SERVO</span>
            <span>SORT RESULT</span>
          </div>
          <div className="field-grid" style={{ marginTop: 16 }}>
            <div>
              <div className="field-label">Vision model</div>
              <div className="field-value">
                {state.epr.provenance?.vision_model_version ?? "--"}
              </div>
            </div>
            <div>
              <div className="field-label">Composition DB</div>
              <div className="field-value">
                schema {state.epr.provenance?.composition_db_schema ?? "--"},{" "}
                {state.epr.provenance?.composition_db_evidence_count ?? 0} sources
              </div>
            </div>
            <div>
              <div className="field-label">Price provider</div>
              <div className="field-value">
                {state.epr.provenance?.price_provider ?? "--"}
              </div>
            </div>
            <div>
              <div className="field-label">Calibration</div>
              <div className="field-value">
                <Status
                  value={
                    state.epr.provenance?.calibration?.verified
                      ? "CALIBRATED"
                      : "UNCALIBRATED"
                  }
                />
              </div>
            </div>
            <div>
              <div className="field-label">Hardware mode</div>
              <div className="field-value">
                {state.epr.provenance?.hardware_mode ?? "--"}
              </div>
            </div>
            <div>
              <div className="field-label">Software</div>
              <div className="field-value">
                {state.epr.provenance?.software_version ?? "--"} · PMDI{" "}
                {state.epr.provenance?.pmdi_version ?? "--"}
              </div>
            </div>
          </div>
        </Panel>
      )}

      <footer className="foot">
        Aurum identifies components and estimates contained metal from cited
        composition. It does not assay anything. Mechanical conveying and
        singulation are the next hardware stage.
      </footer>
    </div>
  );
}
