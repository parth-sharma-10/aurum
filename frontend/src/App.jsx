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

const pct = (c) => (c == null ? "--" : `${(c * 100).toFixed(1)}%`);
const grams = (g) => (g == null ? "--" : `${g.toFixed(1)} g`);
const num = (v, digits = 4) => (v == null ? "--" : Number(v).toFixed(digits));

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
          <strong>Detected and confirmed.</strong> Place it on the pan, then
          press <strong>Measure &amp; route</strong> to weigh it, grade it and
          fire the paddle.
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
        note={`${item.detection_count} observations · confidence is the mean over all of them`}
        state={item.class_name ? "good" : "bad"}
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

      {metals.length > 0 && (
        <table className="metals">
          <thead>
            <tr>
              <th>Metal</th>
              <th>Contained</th>
              <th>Basis</th>
              <th>Evidence</th>
            </tr>
          </thead>
          <tbody>
            {metals.map(([metal, amount, kind]) => (
              <tr key={metal} className={kind === "precious" ? "precious" : ""}>
                <td className="mono">{metal}</td>
                <td className="mono">{num(amount.grams, 6)} g</td>
                <td className="muted small">{amount.calculation}</td>
                <td className="mono small">{amount.evidence.join(", ")}</td>
              </tr>
            ))}
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
        title="Estimated value"
        value={
          pmdi?.pmdi_value != null ? (
            <span className="mono big">
              {num(pmdi.pmdi_value, 2)} {pmdi.currency}
            </span>
          ) : graded ? (
            <span className="badge-c">NO PRICE SOURCE</span>
          ) : (
            pending()
          )
        }
        note={
          graded
            ? (pmdi?.reason ??
              "Aurum ships no market data feed; a price with no source is worse than none.")
            : null
        }
        state={pmdi?.pmdi_value != null ? "good" : graded ? "warn" : "neutral"}
      />

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
  const [error, setError] = useState(null);
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
    try {
      const out = await call(path, "POST");
      setLastResult(out);
      setError(out.error ? `${out.error}: ${out.reason}` : null);
      await load();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(null);
    }
  };

  const running = state?.running;
  const board = state?.board ?? {};
  const actuation = state?.actuation ?? {};
  const cal = state?.calibration ?? {};
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
        <button
          className="primary"
          disabled={busy}
          onClick={() => act("measure", "/session/measure")}
        >
          {busy === "measure" ? "Measuring…" : "Measure & route"}
        </button>
        <button disabled={busy} onClick={() => act("stop", "/session/stop")}>
          Stop
        </button>
        <span className="controls-note">
          The operator carries the component between stages and says{" "}
          <em>when</em>. The class comes from the model, the mass from the cell,
          the bin from the decision engine.
        </span>
      </div>

      {state?.mock_mass?.enabled && (
        <div className="notice">
          <strong>MOCK MASS — the mass is assumed, not measured.</strong> The
          load cell cannot supply one, so every figure derived from it is
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
        </div>
      )}
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
          <p className="section-note">{state?.conveyor?.note}</p>
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
              <th>Bin</th>
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
                  <span className={BIN_CLASS[i.decision.decision]}>
                    {i.decision.decision}
                  </span>
                </td>
                <td className="mono small">{i.actuation?.servo ?? "—"}</td>
                <td className="muted small">{i.decision.reason_code}</td>
              </tr>
            ))}
            {processed.length === 0 && (
              <tr>
                <td colSpan={8} className="muted">
                  Nothing routed yet.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </section>

      <footer className="foot">
        Aurum identifies components and estimates contained metal from cited
        composition. It does not assay anything. Mechanical conveying and
        singulation are the next hardware stage.
      </footer>
    </div>
  );
}
