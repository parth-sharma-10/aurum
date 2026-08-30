/**
 * The shell: one poll, one startup sequence, two modes.
 *
 * `/session` is the only thing polled. Everything both screens render is
 * derived from that one snapshot in `status.js`, so the operator view and the
 * maintenance view cannot disagree about the machine - they are two readings of
 * the same document. `/ready` is fetched once at start-up and again on Retry,
 * which is a question asked twice, not a second polling loop.
 *
 * Start-up connects the camera and the board by itself. That is the point of an
 * automatic machine: the operator places an object, and nothing before that
 * moment should require a decision from them.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  API,
  BIN_CLASS,
  CLASS_COLOR,
  POLL_MS,
  call,
  grams,
  machineState,
  num,
  pct,
  subsystems,
  worstSubsystem,
} from "./status.js";
import {
  ConveyorPanel,
  ErrorsPanel,
  HardwarePanel,
  ItemChain,
  PanBanner,
  Panel,
  PricingPanel,
  Status,
  UpcomingQueue,
} from "./Advanced.jsx";
import {
  ActionRequired,
  Activity,
  Bins,
  CameraPanel,
  CurrentObject,
  HelpDrawer,
  MachineStatus,
  Masthead,
  NextSort,
  Process,
  Startup,
  SystemHealth,
} from "./Operator.jsx";

const STEP = (name, state, detail) => ({ name, state, detail });

export default function App() {
  const [state, setState] = useState(null);
  const [health, setHealth] = useState(null);
  // Start-up opens a serial port and resets the board. It must happen once,
  // whatever React does with the effect: StrictMode double-invokes it in
  // development, which fired two concurrent connects at one Arduino and made
  // each of them eat the other's acknowledgement.
  const started = useRef(false);
  const [error, setError] = useState(null);
  const [actionError, setActionError] = useState(null);
  const [busy, setBusy] = useState(null);
  const [mode, setMode] = useState("operator");
  const [help, setHelp] = useState(null);
  const [startup, setStartup] = useState({
    phase: "checking",
    reason: "Checking systems…",
    checks: [],
    topic: null,
  });

  const load = useCallback(async () => {
    try {
      setState(await call("/session"));
      setError(null);
    } catch (e) {
      setError(e.message);
    }
  }, []);

  /**
   * Bring the machine up, saying what it is doing while it does it.
   *
   * Sequential on purpose. Connecting the board resets the Arduino and parks
   * both paddles, and doing that while the camera is still opening would stack
   * two slow blocking operations on each other for no gain.
   */
  const runStartup = useCallback(async () => {
    const checks = [
      STEP("Aurum service", "busy", "Connecting…"),
      STEP("Camera", "pending", "Waiting"),
      STEP("Arduino", "pending", "Waiting"),
      STEP("Sorting paddles", "pending", "Waiting"),
    ];
    const put = (i, s, d) => {
      checks[i] = STEP(checks[i].name, s, d);
      setStartup((p) => ({ ...p, checks: [...checks] }));
    };
    setStartup({ phase: "checking", reason: "Checking systems…", checks, topic: null });

    try {
      await call("/ready");
      put(0, "ok", "Running");
    } catch {
      put(0, "bad", "Not reachable");
      setStartup((p) => ({
        ...p,
        phase: "failed",
        reason: "The browser cannot reach the Aurum backend. Is the server running?",
        topic: null,
      }));
      return;
    }

    let snapshot = null;
    try {
      snapshot = await call("/session");
    } catch {
      /* the poll owns this failure */
    }

    put(1, "busy", "Starting…");
    if (snapshot?.running) {
      put(1, "ok", "Already running");
    } else {
      try {
        const out = await call("/session/start", "POST");
        if (out.running) put(1, "ok", `Watching (${out.source ?? "webcam"})`);
        else put(1, "bad", out.error ?? "Could not start");
      } catch (e) {
        put(1, "bad", e.message);
      }
    }

    put(2, "busy", "Connecting…");
    let board = null;
    try {
      board = await call("/session/board/connect", "POST");
      if (board.connected) put(2, "ok", board.port ?? "Connected");
      else put(2, "bad", board.reason ?? board.last_error ?? "Not connected");
    } catch (e) {
      put(2, "bad", e.message);
    }

    if (board?.connected) {
      // WARN, not BAD, when the angles were not acknowledged. The backend
      // already decides what is blocking, and it files this as ADVISORY: an
      // unacknowledged CFG leaves the board on the angles the sketch booted
      // with, which on this rig are the same numbers the config would have
      // sent. Failing the boot on it stopped a machine that could sort - and
      // in SIMULATION there is no board to accept a configuration at all, so
      // it could never be anything but unapplied.
      //
      // Only `blocking` from /ready gates the boot now. Re-deriving that here
      // is exactly the second place to be wrong that the note below warns of.
      put(
        3,
        board.servo_config_applied ? "ok" : "warn",
        board.servo_config_applied
          ? `Rest ${board.servo_config?.rest_deg}°, push ${board.servo_config?.push_deg}°`
          : "Running the angles the sketch booted with",
      );
    } else {
      put(3, "warn", "No Arduino - paddles cannot move");
    }

    await load();

    // `/ready` a second time, and this is the one that decides. Asked before
    // the camera was started it answers about a machine that has not been
    // brought up yet - which is how the first version of this screen managed to
    // report "needs attention" under four green ticks.
    //
    // A BLOCKING check is one the pipeline genuinely cannot run without, and
    // the backend already draws that line (advisory failures are the shipped
    // state, not defects). Re-deriving it here would give us two places to be
    // wrong about it.
    let blocking = [];
    try {
      const ready = await call("/ready");
      blocking = (ready.checks ?? []).filter((c) => c.blocking && !c.ready);
    } catch {
      /* the service answered a moment ago; the poll owns a later failure */
    }

    const failed = checks.find((c) => c.state === "bad");
    if (failed || blocking.length) {
      const topic =
        failed?.name === "Camera"
          ? "camera"
          : failed?.name === "Arduino"
            ? "arduino"
            : failed?.name === "Sorting paddles"
              ? "servos"
              : null;
      setStartup((p) => ({
        ...p,
        phase: "failed",
        checks: [...checks],
        topic,
        reason: failed
          ? `${failed.name}: ${failed.detail}`
          : `${blocking[0].name} is not ready: ${blocking[0].detail}`,
      }));
      return;
    }
    setStartup((p) => ({ ...p, phase: "done", checks: [...checks], reason: "All systems ready." }));
  }, [load]);

  useEffect(() => {
    if (started.current) return;
    started.current = true;
    call("/health")
      .then(setHealth)
      .catch(() => {});
    runStartup();
  }, [runStartup]);

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
      // A refusal is the machine explaining itself, not a crash. Keep it until
      // the next action rather than letting the poll wipe it a moment later.
      setActionError(out.error ? `${out.error} — ${out.reason}` : null);
      await load();
    } catch (e) {
      setActionError(e.message);
    } finally {
      setBusy(null);
    }
  };

  const retry = async () => {
    setBusy("retry");
    try {
      await runStartup();
      setHelp(null);
    } finally {
      setBusy(null);
    }
  };

  const rows = useMemo(() => subsystems(state), [state]);
  const machine = useMemo(() => machineState(state, startup), [state, startup]);
  const worst = useMemo(() => worstSubsystem(rows), [rows]);

  // The startup screen owns the whole viewport, so nobody reads a half-built
  // machine as a broken one.
  if (startup.phase !== "done") {
    return (
      <div className="shell">
        <Startup startup={startup} onRetry={retry} onHelp={setHelp} busy={busy} />
        {help && (
          <HelpDrawer
            topic={help}
            row={rows.find((r) => r.key === help)}
            onClose={() => setHelp(null)}
            onRetry={retry}
            busy={busy}
          />
        )}
      </div>
    );
  }

  const item = state?.current_item;
  const pending = (state?.routing?.pending ?? [])[0];
  const fault = state?.hardware?.fault ?? {};
  const target = item?.decision
    ? (item.decision.physical_bin ?? item.decision.decision)
    : pending?.decision;
  const processed = (state?.items ?? []).filter((i) => i.decision);

  return (
    <div className="shell">
      <Masthead
        state={state}
        machine={machine}
        mode={mode}
        onMode={setMode}
        onEstop={() => act("estop", "/hardware/estop")}
        busy={busy}
      />

      {error && (
        <div className="banner is-bad">
          <span aria-hidden="true">⚠</span> Cannot reach Aurum. {error}
        </div>
      )}
      {actionError && (
        <div className="banner is-bad">
          <span aria-hidden="true">⚠</span> {actionError}
        </div>
      )}

      <ActionRequired
        row={worst}
        faultActive={Boolean(fault.active)}
        onHelp={setHelp}
        onReset={() => act("fault", "/hardware/fault/reset")}
        busy={busy}
      />

      {mode === "operator" ? (
        <>
          <MachineStatus
            machine={machine}
            extra={
              state?.mock_mass?.enabled ? (
                <p className="status-caveat">
                  Weights are assumed, not measured — the load cell is not supplying them.
                </p>
              ) : null
            }
          />

          <div className="grid-two">
            <CameraPanel state={state} api={API} onHelp={setHelp} />
            <CurrentObject item={item} machine={machine} />
          </div>

          <Process item={item} pending={pending} />

          <div className="grid-two">
            <NextSort routing={state?.routing} subsystemRows={rows} onHelp={setHelp} />
            <Bins items={state?.items} current={target} />
          </div>

          <div className="grid-two">
            <SystemHealth rows={rows} onHelp={setHelp} />
            <Activity state={state} />
          </div>
        </>
      ) : (
        <div className="maintenance">
          <div className="card controls">
            <h2 className="card-title">Manual controls</h2>
            <p className="card-note">
              Not the normal path. The load cell starts every measurement by itself; these exist
              for a bench with no working cell, or a mass that will not settle.
            </p>
            <div className="control-row">
              <button disabled={busy} onClick={() => act("camera", "/session/start")}>
                {busy === "camera" ? "Starting…" : "Start camera"}
              </button>
              <button disabled={busy} onClick={() => act("board", "/session/board/connect")}>
                {busy === "board" ? "Connecting…" : "Connect board"}
              </button>
              <button disabled={busy} onClick={() => act("stop", "/session/stop")}>
                Stop
              </button>
              <button disabled={busy} onClick={() => act("reset", "/track/reset")}>
                {busy === "reset" ? "Resetting…" : "New item / reset run"}
              </button>
              <button disabled={busy} onClick={() => act("measure", "/session/measure")}>
                {busy === "measure" ? "Measuring…" : "Measure & route now"}
              </button>
              <button disabled={busy} onClick={() => act("scripted", "/session/demo/step")}>
                {busy === "scripted" ? "Running…" : "Scripted object"}
              </button>
              <button
                disabled={busy}
                onClick={() => act("calibration", "/session/calibration/reload")}
              >
                {busy === "calibration" ? "Reloading…" : "Reload calibration"}
              </button>
            </div>
          </div>

          <PanBanner pan={state?.pan} automatic={state?.automatic} />

          <section className="card">
            <h2 className="card-title">Evidence for the current object</h2>
            <p className="card-note">
              Model {health?.model_version ?? "--"} · {state?.confirmed_count ?? 0} confirmed in
              view
            </p>
            <ItemChain item={item} />
          </section>

          <div className="systems">
            <ConveyorPanel conveyor={state?.conveyor} />
            <PricingPanel pricing={state?.pricing} />
            <HardwarePanel
              hardware={state?.hardware}
              board={state?.board}
              actuation={state?.actuation}
              busy={busy}
              onReset={() => act("fault", "/hardware/fault/reset")}
            />
          </div>

          <section className="card">
            <h2 className="card-title">Sorted this run</h2>
            <p className="card-note">
              One physical object, one identity, one movement. {processed.length} processed.{" "}
              {processed.length > 0 && (
                <a className="mono small" href={`${API}/session/report.csv`}>
                  Download CSV
                </a>
              )}
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
                      <span className="chip" style={{ "--swatch": CLASS_COLOR[i.class_name] }}>
                        {i.class_name}
                      </span>
                    </td>
                    <td className="mono">{pct(i.confidence)}</td>
                    <td className="mono">
                      {grams(i.weight_g)} <span className="muted small">{i.weight_status}</span>
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
                      {i.decision.physical_bin &&
                        i.decision.physical_bin !== i.decision.decision && (
                          <span className="muted small"> → {i.decision.physical_bin}</span>
                        )}
                    </td>
                    <td className="mono small">{i.actuation?.servo ?? "—"}</td>
                    <td className="muted small">{i.decision.reason_code}</td>
                  </tr>
                ))}
                {processed.length === 0 && (
                  <tr>
                    <td colSpan={8} className="muted">
                      Nothing sorted yet.
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
              <p className="card-note">
                Every object's whole trail — detected, classified, weighed, valued, binned,
                actuated — with the provenance below stamped on each event.
                <span className="mono small"> GET /epr/&lt;item_id&gt;</span>
              </p>
              <div className="field-grid">
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
                  <div className="field-value">{state.epr.provenance?.price_provider ?? "--"}</div>
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
                  <div className="field-value">{state.epr.provenance?.hardware_mode ?? "--"}</div>
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
        </div>
      )}

      {help && (
        <HelpDrawer
          topic={help}
          row={rows.find((r) => r.key === help)}
          onClose={() => setHelp(null)}
          onRetry={retry}
          busy={busy}
        />
      )}

      <footer className="foot">
        Aurum identifies components and estimates contained metal from cited composition. It does
        not assay anything. Mechanical conveying and singulation are the next hardware stage.
      </footer>
    </div>
  );
}
