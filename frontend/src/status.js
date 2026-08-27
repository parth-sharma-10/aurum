/**
 * The layer between what the machine says and what an operator reads.
 *
 * Everything here is a pure function over the `/session` snapshot. No fetching,
 * no state, no JSX - so the whole mapping can be reasoned about, and so the
 * operator screen and the engineering screen can never disagree about what a
 * status word means.
 *
 * TWO RULES THIS FILE EXISTS TO ENFORCE.
 *
 * **A raw enum never reaches the operator.** `TIMING_EXPIRED` is a routing
 * reason code; "The object missed its sorting window" is what happened. Both
 * are true, and only one of them is useful to somebody holding a CPU.
 *
 * **A real failure is never softened into a wait.** `machineState` reads the
 * camera and the board BEFORE it reads the pan, because "camera not started"
 * and "waiting for an object" look identical from the pan's point of view and
 * mean completely different things to the person standing at the bench. An
 * operator who is told to wait for a machine that cannot see will wait for
 * ever.
 */

export const API = import.meta.env.VITE_AURUM_API ?? "http://127.0.0.1:8000";

/** Fast enough that the countdown to a paddle stroke reads as a countdown. */
export const POLL_MS = 400;

export const CLASS_COLOR = {
  PCB: "var(--cls-pcb)",
  RAM: "var(--cls-ram)",
  CPU: "var(--cls-cpu)",
  Connector: "var(--cls-connector)",
};

export const BIN_CLASS = { A: "badge-a", B: "badge-b", C: "badge-c" };

export async function call(path, method = "GET") {
  const res = await fetch(`${API}${path}`, { method });
  if (!res.ok) throw new Error(`${path} -> HTTP ${res.status}`);
  return res.json();
}

// -- formatters ------------------------------------------------------------

export const pct = (c) => (c == null ? "--" : `${(c * 100).toFixed(0)}%`);
export const grams = (g) => (g == null ? "--" : `${g.toFixed(1)} g`);
export const num = (v, digits = 4) => (v == null ? "--" : Number(v).toFixed(digits));
export const seconds = (v) => (v == null ? "--" : `${v.toFixed(2)} s`);

export const money = (v, currency) =>
  v == null
    ? null
    : `${currency === "INR" ? "₹" : `${currency ?? ""} `}${Number(v).toLocaleString(undefined, {
        minimumFractionDigits: 2,
        maximumFractionDigits: 2,
      })}`;

/** Milligrams where that reads better than a long decimal of grams. */
export const metalMass = (g) =>
  g == null ? "--" : g < 1 ? `${(g * 1000).toFixed(2)} mg` : `${g.toFixed(3)} g`;

/** A clock time from an ISO stamp. The date is never the interesting part here. */
export const clock = (iso) => {
  if (!iso) return "";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "" : d.toLocaleTimeString([], { hour12: false });
};

// -- the vocabulary --------------------------------------------------------

/**
 * Every enum the backend can put on screen, in words.
 *
 * Sourced from the enums themselves rather than invented: `FaultCode`
 * (app/hardware/fault.py), `ErrorCode` (app/errors.py), `RouteReason` and
 * `RouteStatus` (app/routing/scheduler.py), `WeightStatus` (app/weight.py),
 * `ItemState` (app/vision/tracker.py) and `PanState` (app/pipeline/pan.py).
 */
const WORDS = {
  // Link and hardware
  CONNECTED: "Connected",
  CONNECTING: "Connecting",
  DISCONNECTED: "Not connected",
  DEGRADED: "Connection unreliable",
  PHYSICAL: "Real hardware",
  SIMULATION: "Simulation",

  // Faults
  ARDUINO_DISCONNECTED: "Arduino disconnected",
  WRITE_FAILED: "Could not send to the Arduino",
  ACK_TIMEOUT: "The Arduino did not answer",
  BOARD_ERROR: "The Arduino reported an error",
  INVALID_SERVO_STATE: "Servo configuration is wrong",
  INVALID_SCHEDULE: "Sorting schedule is inconsistent",
  INVALID_SPEED: "Conveyor speed is invalid",
  ENCODER_FAILURE: "Conveyor encoder not reporting",
  RECOVERY_REQUIRED: "Interrupted mid-command",
  EMERGENCY_STOP: "Emergency stop",

  // Recorded failures
  VISION_ERROR: "Camera or model problem",
  TRACKING_ERROR: "Lost track of the object",
  WEIGHT_ERROR: "Weighing problem",
  MATERIAL_ERROR: "Composition lookup failed",
  PRICE_ERROR: "Price lookup failed",
  DECISION_ERROR: "Could not decide a bin",
  ROUTING_ERROR: "Could not schedule the sort",
  ARDUINO_ERROR: "Arduino problem",
  SERVO_ERROR: "Paddle problem",
  CONVEYOR_ERROR: "Conveyor problem",
  TIMEOUT: "Timed out",
  CONFIG_ERROR: "Configuration problem",
  HARDWARE_FAULT: "Hardware fault",

  // Weight
  RAW: "Unfiltered reading",
  UNSTABLE: "Still stabilising",
  STABLE: "Settled",
  SIMULATED: "Assumed, not measured",
  MEASURED: "Weighed",
  UNAVAILABLE: "Not available",

  // Routing
  SCHEDULED: "Scheduled",
  DUE: "Firing now",
  EXECUTED: "Sorted",
  NO_ACTION: "No paddle needed",
  UNSCHEDULED: "Not scheduled",
  ROUTE_A: "Routed to bin A",
  ROUTE_B: "Routed to bin B",
  NO_ROUTE_C: "Bin C needs no paddle",
  GEOMETRY_UNMEASURED: "The machine has not been measured",
  BELT_SPEED_UNMEASURED: "Conveyor speed is unknown",
  SERVO_GEOMETRY_UNMEASURED: "Distance to the paddle is unknown",
  CAMERA_LOAD_CELL_GEOMETRY_UNMEASURED: "Distance to the platform is unknown",
  ACTUATION_DELAY_UNMEASURED: "Paddle delay has not been timed",
  INVALID_POSITION: "The object is past the paddle",
  INVALID_DECISION: "No valid bin was chosen",
  ALREADY_ROUTED: "Already sorted once",
  STALE_ITEM: "The object is no longer tracked",
  TIMING_UNAVAILABLE: "Sorting time could not be worked out",
  TIMING_EXPIRED: "The object missed its sorting window",
  ACTUATION_FAILED: "The paddle command failed",

  // Actuation outcomes
  ACTUATED: "Paddle fired",
  FAILED: "Failed",
  SKIPPED: "Skipped",
  EXPIRED: "Missed its window",
  BLOCKED: "Blocked by a fault",
  ACKED: "Acknowledged",

  // Tracking
  NEW: "Just seen",
  TRACKING: "Tracking",
  CONFIRMED: "Confirmed",
  LEAVING: "Leaving view",
  FINALIZED: "Finished",

  // Provenance
  REFERENCE: "Published reference price",
  LIVE: "Live market price",
  MANUAL: "Entered by hand",
  ESTIMATED: "Estimated",
  CALIBRATED: "Calibrated",
  UNCALIBRATED: "Not calibrated",
  UNMEASURED: "Not measured",
  STALE: "Out of date",
};

/**
 * One enum in plain English.
 *
 * An unknown key is title-cased rather than dropped or thrown on: the backend
 * gains states faster than this table does, and a word nobody has translated
 * yet is far better than a blank where the machine's state should be.
 */
export function plain(value) {
  if (value == null) return null;
  const key = String(value);
  if (WORDS[key]) return WORDS[key];
  return key
    .replace(/_/g, " ")
    .toLowerCase()
    .replace(/^\w/, (c) => c.toUpperCase());
}


/**
 * Why an object was refused a grade, and what the operator can do about it.
 *
 * Each of these is a DIFFERENT problem and they must not share one message.
 * A PCB fragment weighing 10.8 g against a 20 g floor is a mass anomaly - the
 * camera did its job. Telling that operator to "hold it still for the camera"
 * sends them to the wrong end of the machine, which is the failure this whole
 * mapping layer exists to prevent.
 */
const REFUSALS = {
  UNKNOWN_MASS_ANOMALY: {
    title: "Weight does not match what this looks like",
    what: "The camera and the scale disagree, so Aurum will not grade it.",
    todo: "Check only one object is on the platform, and that nothing is leaning on it.",
  },
  UNKNOWN_CONFIDENCE: {
    title: "Not sure what this is",
    what: "The camera could not identify the object well enough to grade it.",
    todo: "Keep it still, fully inside the camera view, and well lit.",
  },
  UNKNOWN_CLASS: {
    title: "Object not recognised",
    what: "This is not one of the components Aurum has been trained on.",
    todo: "Nothing to fix — it goes to bin C, which is the safe outcome.",
  },
  UNKNOWN_WEIGHT: {
    title: "Could not weigh it",
    what: "Without a weight, nothing can be worked out about what it contains.",
    todo: "Take it off, let the reading settle at zero, and put it back on.",
  },
  UNKNOWN_MATERIAL: {
    title: "No composition on record",
    what: "Aurum has no cited material data for this component.",
    todo: "Nothing to fix — it goes to bin C.",
  },
  UNKNOWN_EVIDENCE: {
    title: "Not enough evidence",
    what: "The material data for this component is incomplete.",
    todo: "Nothing to fix — it goes to bin C.",
  },
  UNKNOWN_DATA: {
    title: "Missing data",
    what: "Something needed to grade this object was unavailable.",
    todo: "Nothing to fix — it goes to bin C.",
  },
};

/** The refusal for a decision, or null when the object was graded normally. */
export function refusal(decision) {
  if (!decision?.unknown) return null;
  return (
    REFUSALS[decision.reason_code] ?? {
      title: "Could not grade this object",
      what: plain(decision.reason_code) ?? "Aurum could not justify a bin.",
      todo: "It goes to bin C, which is the safe outcome.",
    }
  );
}

/**
 * What an object is worth, and which half of the figure it is.
 *
 * `total_value` is null whenever the base-metal half could not be priced, and
 * the precious half on its own is still a real number worth showing - printing
 * "--" over a genuine 94.96 rupees of recoverable metal is throwing away the
 * answer because part of it is missing.
 */
export function objectValue(valuation) {
  if (!valuation) return { text: "--", note: null };
  if (valuation.total_value != null) {
    return {
      text: money(valuation.total_value, valuation.currency),
      note: plain(valuation.price_status),
    };
  }
  if (valuation.precious_value != null) {
    return {
      text: money(valuation.precious_value, valuation.currency),
      note: "precious metals only",
    };
  }
  return { text: "--", note: plain(valuation.price_status) };
}

// -- how to fix things -----------------------------------------------------

/**
 * The checklist for each subsystem that can stop the machine.
 *
 * Deliberately concrete and ordered by how often each one is the answer. The
 * second Arduino step is first-hand: two Aurum backends holding one macOS
 * `cu.*` port silently split the board's replies, and that cost this project
 * three sessions of suspecting the firmware.
 */
export const HELP = {
  camera: {
    title: "Camera",
    need: "Aurum needs the camera to identify what an object is.",
    steps: [
      "Check the webcam is plugged in.",
      "Close any other app using the camera (Zoom, Photo Booth, another browser tab).",
      "On macOS, allow camera access for the terminal running Aurum.",
      "Restart Aurum and let it start the camera again.",
    ],
  },
  loadcell: {
    title: "Load cell",
    need: "Aurum needs the load cell to weigh objects. Without it nothing can be graded.",
    steps: [
      "Check the Arduino is connected — the load cell is read through it.",
      "Check the HX711 wiring: DOUT to D2, SCK to D3, and both power leads.",
      "Take everything off the platform and let it settle.",
      "If weights look wrong, run the calibration again.",
    ],
  },
  arduino: {
    title: "Arduino",
    need: "Aurum needs the Arduino to move the sorting paddles and to read the load cell.",
    steps: [
      "Check the USB cable is seated at both ends.",
      "Make sure no other Aurum backend is running — two processes on one port break every command.",
      "Close the Arduino IDE Serial Monitor if it is open.",
      "Check the port in the configuration matches the board (it changes when you move USB sockets).",
      "Unplug the board, plug it back in, and restart Aurum.",
    ],
  },
  servos: {
    title: "Sorting paddles",
    need: "Aurum needs the paddles configured before it will sort anything automatically.",
    steps: [
      "Check the Arduino is connected first — the paddles are configured over the same link.",
      "Check the external 5 V servo supply is powered and shares a ground with the Arduino.",
      "Restart Aurum so it sends the paddle configuration again.",
    ],
  },
  conveyor: {
    title: "Conveyor",
    need: "Aurum uses the conveyor speed to work out when an object reaches its bin.",
    steps: [
      "Check the conveyor mode in the configuration profile.",
      "If there is no belt, use the simulated belt so timing can still be demonstrated.",
      "If a belt is fitted, check its measured speed is set.",
    ],
  },
  fault: {
    title: "Hardware fault",
    need: "A fault is latched. No paddle will move until somebody has looked at the machine.",
    steps: [
      "Look at the rig: a command may have left a paddle part-way out.",
      "Move anything jammed clear by hand.",
      "Clear the fault only once you have actually checked — clearing it is a statement that you did.",
    ],
  },
};

// -- the one dominant machine state ---------------------------------------

const STATE = (key, title, detail, action, tone) => ({ key, title, detail, action, tone });

/**
 * Where the machine is, in one state, with what to do about it.
 *
 * Precedence is the whole design. Safety first (a latched fault stops
 * everything), then the subsystems that make the automatic cycle impossible,
 * and only then the cycle itself. Reading the pan first would let a machine
 * with no camera report "waiting for an object", which is a lie told calmly.
 */
export function machineState(state, startup) {
  if (startup && startup.phase !== "done") {
    return startup.phase === "failed"
      ? STATE(
          "ATTENTION",
          "Aurum needs attention",
          startup.reason ?? "A system did not start.",
          "Fix the problem below, then press Retry.",
          "fault",
        )
      : STATE(
          "STARTING",
          "Starting Aurum",
          startup.reason ?? "Checking systems…",
          "One moment.",
          "busy",
        );
  }

  if (!state) {
    return STATE(
      "OFFLINE",
      "Cannot reach Aurum",
      "The browser cannot reach the Aurum backend.",
      "Check the Aurum server is running, then reload this page.",
      "fault",
    );
  }

  const fault = state.hardware?.fault ?? {};
  const camera = state.camera ?? {};
  const board = state.board ?? {};
  const pan = state.pan ?? {};
  const physical = state.hardware?.mode === "PHYSICAL";

  if (fault.active && fault.code === "EMERGENCY_STOP") {
    return STATE(
      "EMERGENCY_STOP",
      "Emergency stop active",
      "All physical movement has been stopped. The machine will stay paused until it is reset.",
      "Check the machine is safe, then reset the emergency stop.",
      "fault",
    );
  }
  if (fault.active) {
    return STATE(
      "FAULT",
      "Hardware fault",
      plain(fault.code) ?? "A fault is latched.",
      "Check the machine, then clear the fault. Nothing will move until you do.",
      "fault",
    );
  }
  if (camera.error) {
    return STATE(
      "CAMERA_OFFLINE",
      "Camera offline",
      "Aurum cannot see, so it cannot identify anything.",
      "Fix the camera below. Sorting is paused until it is back.",
      "fault",
    );
  }
  if (!state.running) {
    return STATE(
      "CAMERA_OFFLINE",
      "Camera not started",
      "The camera is not running, so no object can be identified.",
      "Start the camera below.",
      "fault",
    );
  }
  if (physical && !board.connected) {
    return STATE(
      "ARDUINO_OFFLINE",
      "Arduino not connected",
      "Aurum cannot weigh objects or move the sorting paddles.",
      "Fix the Arduino connection below. Automatic sorting is paused.",
      "fault",
    );
  }
  // The load cell is what STARTS the cycle, so a cell that cannot be read is
  // not a machine waiting for an object - it is a machine that will wait for
  // ever. `pan.grams` is null only when the pan could not get a mass at all,
  // and `pan.reason` is the cell's own account of why: not connected, not
  // calibrated, or stopped answering mid-run. All three used to fall through
  // to "Aurum is ready and watching the platform", which is the exact lie the
  // camera and board checks above exist to prevent, told by the one sensor
  // that drives everything.
  if (pan.state && pan.grams == null && pan.reason) {
    return physical
      ? STATE(
          "LOAD_CELL_OFFLINE",
          "Load cell not readable",
          pan.reason,
          "Fix the load cell below. Nothing can be weighed or sorted until it reads.",
          "fault",
        )
      : STATE(
          "LOAD_CELL_ABSENT",
          "No load cell",
          pan.reason,
          "This is a simulated run. Drive the chain from Developer controls in Advanced mode.",
          "ready",
        );
  }

  // The automatic cycle. One pass of the pan machine, in the operator's words.
  const item = state.current_item;
  const pending = (state.routing?.pending ?? [])[0];

  switch (pan.state) {
    case "OBJECT_PRESENT":
      return STATE(
        "DETECTING",
        "Object detected",
        "Something has been placed on the platform.",
        "Leave it where it is.",
        "busy",
      );
    case "WEIGHING":
      return STATE(
        "WEIGHING",
        "Weighing object",
        pan.grams == null ? "Reading weight…" : `Reading weight… ${pan.grams.toFixed(1)} g`,
        "Hold the object still until the reading settles.",
        "busy",
      );
    case "WEIGHT_STABLE":
      return STATE(
        "IDENTIFYING",
        "Identifying object",
        item?.class_name
          ? `The camera sees a ${item.class_name}.`
          : "Matching the object against the model.",
        "Keep the object in view.",
        "busy",
      );
    case "PROCESSING":
      return STATE(
        "DECIDING",
        "Choosing a bin",
        "Combining what it is, what it weighs and what it contains.",
        "Nothing to do.",
        "busy",
      );
    case "ROUTING":
      return STATE(
        "SCHEDULED",
        "Sort scheduled",
        item?.decision
          ? `Bin ${item.decision.physical_bin ?? item.decision.decision} chosen.`
          : "Working out when to fire the paddle.",
        "Nothing to do.",
        "busy",
      );
    case "WAITING_FOR_CLEAR":
      if (pending) {
        return STATE(
          "SORTING",
          "Sorting",
          `Routing the object to bin ${pending.decision}.`,
          pending.seconds_remaining == null
            ? "The paddle is about to fire."
            : `${pending.servo?.replace("SERVO_", "Paddle ") ?? "The paddle"} fires in ${pending.seconds_remaining.toFixed(1)} s.`,
          "busy",
        );
      }
      // Only a cycle that actually produced a decision may be called complete.
      // Without one there is mass on the platform that no sort accounts for -
      // an object put down before the camera confirmed it, or a tare that has
      // drifted above the clear threshold so the pan can never report itself
      // empty. Both used to render as "Sort complete: this object has been
      // handled", which is a success claim for an object that was never
      // processed, and it is what the bench showed while nothing had run at all.
      if (!item?.decision) {
        return STATE(
          "WAITING_FOR_CLEAR",
          "Platform not clear",
          pan.reason ?? "There is weight on the platform that no sort accounts for.",
          "Take everything off the platform. If it still reads a weight when empty, the load cell needs re-taring.",
          "warning",
        );
      }
      return STATE(
        "COMPLETE",
        "Sort complete",
        `Object routed to bin ${item.decision.physical_bin ?? item.decision.decision}.`,
        "Take the object off the platform. Aurum is ready for the next one.",
        "good",
      );
    default:
      break;
  }

  // WAITING_FOR_OBJECT, with something in view but not yet on the platform.
  if (state.confirmed_count > 0 && !pan.grams) {
    return STATE(
      "DETECTING",
      "Object in view",
      "The camera has confirmed an object.",
      "Place it on the weighing platform. Aurum does the rest.",
      "ready",
    );
  }
  if (pending) {
    return STATE(
      "SORTING",
      "Sorting",
      `Routing an object to bin ${pending.decision}.`,
      pending.seconds_remaining == null
        ? "The paddle is about to fire."
        : `Paddle fires in ${pending.seconds_remaining.toFixed(1)} s.`,
      "busy",
    );
  }
  return STATE(
    "WAITING",
    "Waiting for an object",
    "Aurum is ready and watching the platform.",
    "Place one object on the weighing platform. Aurum will identify, weigh, grade and sort it by itself.",
    "ready",
  );
}

// -- subsystem health ------------------------------------------------------

const LEVEL = { ready: 0, warning: 1, offline: 2, fault: 3 };

/**
 * The six things that have to work, each as one row an operator can read.
 *
 * `level` drives both the word and the mark, never colour alone. `technical` is
 * the raw truth - port, link state, last error, calibration factor - kept out
 * of the way rather than deleted, because it is the first thing anybody
 * debugging will ask for.
 */
export function subsystems(state) {
  const board = state?.board ?? {};
  const cal = state?.calibration ?? {};
  const camera = state?.camera ?? {};
  const conveyor = state?.conveyor ?? {};
  const speed = conveyor.speed ?? {};
  const simulated = state?.hardware?.mode === "SIMULATION";
  const boardUp = Boolean(board.connected) || simulated;

  const rows = [];

  rows.push({
    key: "camera",
    label: "Camera",
    ...(camera.error
      ? { level: "fault", headline: "Not working", detail: camera.error }
      : state?.running
        ? { level: "ready", headline: "Ready", detail: `Watching (${camera.source ?? "webcam"})` }
        : { level: "offline", headline: "Not started", detail: "No video is being processed." }),
    technical: [
      ["Source", camera.source ?? "none"],
      ["Frames processed", state?.frames_processed ?? 0],
      ["Error", camera.error ?? "none"],
    ],
  });

  rows.push({
    key: "loadcell",
    label: "Load cell",
    ...(!boardUp
      ? {
          level: "offline",
          headline: "Not available",
          detail: "The Arduino is not connected, and the load cell is read through it.",
        }
      : cal.verified
        ? { level: "ready", headline: "Ready", detail: "Calibration verified" }
        : {
            level: "warning",
            headline: "Not calibrated",
            detail: "Weights can be shown but not relied on.",
          }),
    technical: [
      ["Counts per gram", num(cal.counts_per_gram, 1)],
      ["Verified", String(Boolean(cal.verified))],
      ["Verification error", cal.verification_error_g == null ? "--" : `${num(cal.verification_error_g, 3)} g`],
      ["Recorded", cal.recorded_at ?? "never"],
    ],
  });

  rows.push({
    key: "arduino",
    label: "Arduino",
    ...(simulated
      ? { level: "ready", headline: "Simulated", detail: "No real board is in use." }
      : board.connected
        ? { level: "ready", headline: "Connected", detail: board.port ?? "" }
        : {
            level: "offline",
            headline: "Not connected",
            detail: "Aurum cannot move the sorting paddles.",
          }),
    technical: [
      ["Port", board.port ?? "none"],
      ["Link state", plain(board.state) ?? "--"],
      ["Last error", board.last_error ?? "none"],
      ["Discarded lines", board.dropped_lines ?? 0],
    ],
  });

  // The only claim in this whole system that software cannot make for itself.
  // A stalled servo, a cut signal wire and a dead supply rail all acknowledge
  // a MOVE identically, so this comes from a human having watched the paddle.
  const watched = (state?.hardware?.movement_verification?.verified ?? []).length;

  rows.push({
    key: "servos",
    label: "Sorting paddles",
    ...(simulated
      ? { level: "ready", headline: "Simulated", detail: "No paddle will physically move." }
      : board.servo_config_applied
        ? {
            level: "ready",
            headline: "Ready",
            detail:
              watched >= 2
                ? "Both paddles watched moving"
                : `Rest ${board.servo_config?.rest_deg}°, push ${board.servo_config?.push_deg}°`,
          }
        : board.connected
          ? {
              level: "warning",
              headline: "Not configured",
              detail: "The board is using its own start-up angles, not Aurum's.",
            }
          : {
              level: "offline",
              headline: "Not available",
              detail: "No Arduino, so no paddles.",
            }),
    technical: [
      ["Movement verified", watched ? `${watched} of 2, by a person watching` : "never"],
      ["Configuration applied", String(Boolean(board.servo_config_applied))],
      ["Rest angle", board.servo_config?.rest_deg ?? "--"],
      ["Push angle", board.servo_config?.push_deg ?? "--"],
      ["Hold", board.servo_config?.hold_ms == null ? "--" : `${board.servo_config.hold_ms} ms`],
    ],
  });

  rows.push({
    key: "conveyor",
    label: "Conveyor",
    ...(speed.usable
      ? {
          level: "ready",
          headline: `${num(speed.cm_s, 1)} cm/s`,
          detail: speed.status === "SIMULATED" ? "Simulated belt, for timing" : "Running",
        }
      : {
          level: "offline",
          headline: "No speed",
          detail: "Aurum cannot work out when an object reaches its bin.",
        }),
    technical: [
      ["Mode", conveyor.mode ?? "--"],
      ["Speed source", conveyor.speed_source ?? "--"],
      ["Reason", speed.reason ?? "--"],
    ],
  });

  rows.push({
    key: "ledger",
    label: "Record",
    ...(state?.epr?.session_id
      ? { level: "ready", headline: "Recording", detail: "Every object is being logged." }
      : { level: "offline", headline: "Not recording", detail: "No run is open." }),
    technical: [
      ["Run", state?.epr?.session_id ?? "none"],
      ["Model", state?.epr?.provenance?.vision_model_version ?? "--"],
      ["Software", state?.epr?.provenance?.software_version ?? "--"],
    ],
  });

  return rows;
}

/** The worst subsystem, for the action-required banner. Null when all are fine. */
export function worstSubsystem(rows) {
  const bad = rows.filter((r) => r.level !== "ready" && r.level !== "warning");
  if (!bad.length) return null;
  return bad.sort((a, b) => LEVEL[b.level] - LEVEL[a.level])[0];
}

// -- the pipeline ----------------------------------------------------------

/**
 * The seven stages, each answering from the item record rather than the pan.
 *
 * The pan says where the MACHINE is; the record says what this OBJECT has
 * actually been through. They differ exactly when it matters - an object whose
 * paddle has fired is COMPLETE even while the pan is still waiting for the
 * platform to clear.
 */
export function stages(item, pending) {
  const d = item?.decision;
  const act = item?.actuation;
  const weighed = item?.weight_status && item.weight_status !== "UNAVAILABLE";
  // Bin C is finished, not stuck. No paddle was ever going to move for it, so
  // there is no `outcome` field to read - the route carries NO_ACTION instead.
  // Judging only on `outcome` left every bin-C object showing SORT in progress
  // for ever while the banner above it said "Sort complete".
  const noPaddle = act?.route?.status === "NO_ACTION" || act?.outcome === "NO_ACTION";
  const settled = noPaddle || act?.outcome === "ACTUATED";

  const rows = [
    {
      key: "DETECTED",
      label: "Detect",
      done: Boolean(item?.item_id),
      detail: item?.item_id ? `Object ${short(item.item_id)}` : "Nothing on the platform yet",
    },
    {
      key: "WEIGHED",
      label: "Weigh",
      done: Boolean(weighed),
      detail: weighed
        ? `${grams(item.weight_g)} · ${plain(item.weight_status)}`
        : "Not weighed yet",
    },
    {
      key: "IDENTIFIED",
      label: "Identify",
      done: Boolean(item?.class_name && d),
      detail: item?.class_name
        ? `${item.class_name} · ${pct(item.confidence)} confidence`
        : "Not identified yet",
    },
    {
      key: "DECIDED",
      label: "Decide",
      done: Boolean(d),
      detail: d ? `Bin ${d.physical_bin ?? d.decision}` : "No bin chosen yet",
    },
    {
      key: "SCHEDULED",
      label: "Schedule",
      done: Boolean(act),
      detail: pending?.seconds_remaining != null
        ? `Paddle fires in ${pending.seconds_remaining.toFixed(1)} s`
        : act?.route?.reason
          ? act.route.reason
          : "Not scheduled yet",
    },
    {
      key: "SORTING",
      label: "Sort",
      done: Boolean(act?.commanded || act?.outcome || noPaddle),
      detail: noPaddle
        ? "Bin C needs no paddle"
        : act?.outcome
          ? plain(act.outcome)
          : "Paddle has not fired",
    },
    {
      key: "COMPLETE",
      label: "Complete",
      done: settled,
      detail: noPaddle
        ? "Reached bin C, which needs no paddle"
        : (plain(act?.outcome) ?? "Not finished"),
    },
  ];

  // A failed stage is not a pending one, and must not render as though the
  // machine is still working on it.
  const failedAt = ["FAILED", "EXPIRED", "BLOCKED"].includes(act?.outcome) ? 5 : -1;
  const active = rows.findIndex((r) => !r.done);
  return rows.map((r, i) => ({
    ...r,
    state: i === failedAt ? "failed" : r.done ? "done" : i === active && item ? "active" : "pending",
  }));
}

/** The tail of an item id. Full ids are for the record, not for reading aloud. */
export const short = (id) => (id ? String(id).replace(/^AUR-ITEM-/, "#") : "");

// -- what happened ---------------------------------------------------------

/**
 * A recent-activity feed built entirely from `/session`.
 *
 * Deliberately not the EPR ledger: that would mean a request per item, and the
 * snapshot already carries real timestamps for the moments worth showing.
 * Nothing here is invented - an event appears only when the field it comes
 * from is present.
 */
export function activity(state, limit = 12) {
  const events = [];
  const push = (at, ok, text, technical) => {
    if (at) events.push({ at, ok, text, technical });
  };

  for (const item of state?.items ?? []) {
    const id = short(item.item_id);
    push(item.first_seen, true, `Object ${id} detected`, item.item_id);
    if (item.weight_status && item.weight_status !== "UNAVAILABLE") {
      push(
        item.weight_timestamp,
        true,
        `${id} weighed ${grams(item.weight_g)}`,
        plain(item.weight_status),
      );
    }
    if (item.class_name && item.decision) {
      push(item.weight_timestamp ?? item.last_seen, true, `${id} identified as ${item.class_name}`, `${pct(item.confidence)} confidence`);
      const bin = item.decision.physical_bin ?? item.decision.decision;
      push(item.weight_timestamp ?? item.last_seen, true, `${id} sent to bin ${bin}`, plain(item.decision.reason_code));
    }
    const act = item.actuation;
    if (act?.outcome) {
      const ok = ["ACTUATED", "NO_ACTION"].includes(act.outcome);
      push(
        item.last_seen,
        ok,
        ok
          ? act.outcome === "NO_ACTION"
            ? `${id} needed no paddle`
            : `${id} sorted by paddle ${act.target}`
          : `${id} not sorted: ${plain(act.outcome)}`,
        act.reason,
      );
    }
  }

  for (const e of state?.errors?.recent ?? []) {
    push(e.timestamp, false, plain(e.error_code) ?? "Problem", e.message);
  }

  return events
    .sort((a, b) => String(b.at).localeCompare(String(a.at)))
    .slice(0, limit);
}
