# Aurum — the BENCH configuration. Real components; only the belt is a model.
#
#   set -a; source configs/bench-profile.sh; set +a
#   uvicorn app.api:app --port 8000
#
# REAL HERE: the camera, the HX711 mass (the cell carries a verified
# calibration as of 2026-08-26), the serial transport, and the servo command —
# a MOVE frame that leaves the host over a real port and makes a physical
# paddle stroke.
#
# NOT REAL HERE: the belt and the geometry. There is no conveyor, so the
# transport distances are the TEST block and every figure derived from them is
# stamped SIMULATED all the way to the EPR ledger. Those may not be quoted as
# measurements. The mass and the routing decision may.
#
#   demo-profile.sh    everything simulated, nothing to plug in
#   bench-profile.sh   real hardware, simulated belt
#
# Neither is the shipped default. configs/conveyor.yaml still ships
# `mode: NONE`, `simulation: false` and `arduino.enabled: false`, because that
# is what the physical machine currently is.
#
# Before sourcing this, confirm the board is the sorter and not the weight-only
# sketch — aurum_weight has no servo code at all, so nothing can move on it:
#
#   lsof /dev/cu.usbmodem1101         # must be empty; close the IDE Serial Monitor
#   AURUM/1 PING <id>  ->  AURUM/1 PONG <id>     only aurum_sorter answers
#
# ---------------------------------------------------------------------------

# The demonstration belt. SIMULATION switches the routing layer onto the TEST
# distances in configs/conveyor.yaml and stamps every derived figure SIMULATED
# all the way to the EPR ledger.
#
# This line is load-bearing and not interchangeable with AURUM_SIMULATION. The
# real geometry block is UNMEASURED, and an unmeasured machine refuses to
# schedule rather than guessing.
#
# SIMULATION, because the demonstration IS the timing model. The paddle is
# meant to fire when the item would have reached it: distance to the bin
# divided by the belt speed, less the servo's own actuation delay. That wait
# is the feature, not latency to be removed - `Upcoming` counts it down and
# the servo fires at the end of it.
#
# Every figure derived from it is stamped SIMULATED all the way to the EPR
# ledger, because the belt is a model and there is no belt on this bench.
#
# NONE removes the scheduler entirely and fires the paddle the instant the bin
# is decided. Correct for a bench where the operator carries the object, and
# wrong for showing the conveyor model.
AURUM_CONVEYOR_MODE=SIMULATION

# 10 cm/s = 0.10 m/s. Stated rather than defaulted, so the bench says out loud
# what speed it is pretending to run at.
AURUM_SIM_BELT_SPEED_CM_S=10.0

# AURUM_SIMULATION IS DELIBERATELY NOT SET HERE.
#
# Setting it true is what demo-profile.sh does, and it builds an in-process
# board: HARDWARE_MODE=SIMULATION, the protocol and the acknowledgement all
# run, and no byte reaches a serial port even when one is configured. Leaving
# it unset is the whole point of this file — the transport becomes a real
# serial port and the ACK comes back from an actual Arduino.

# The attached board. `ls /dev/cu.usbmodem*` to find yours; never hardcode a
# port you have not looked up — and this line is the proof, because it said
# usbmodem101 on 2026-08-26 while the board was answering on usbmodem1101. The
# number follows the USB location, so it changes when the board moves hub or
# socket. Check it every session; a stale value here fails as "could not open".
# "auto" rather than a name: the same board has come up as usbmodem101 and as
# usbmodem1101 on this bench, and a stale name presents as a board that will
# not connect. Autodetection selects on the USB vendor id, so the Bluetooth and
# debug-console cu.* nodes macOS also lists cannot be picked by mistake, and it
# refuses outright if two USB serial devices are attached. Name the port here
# instead if that ever happens.
AURUM_ARDUINO_PORT=auto

# The board acknowledges a MOVE only after the paddle finishes its stroke, and
# an open load cell makes the sketch emit as fast as the port allows - the ACK
# arrives buried in that flood. 4000 ms was latching ACK_TIMEOUT mid-run and
# stopping the machine. Raised for the bench; the real fix is the load cell.
AURUM_ARDUINO_ACK_TIMEOUT_MS=8000

# THE LENOVO FHD WEBCAM, which is the one aimed at the rig. Index 0 is the
# MacBook's built-in camera pointing at the ceiling - it opens, it reads, and
# it streams a plausible blue-grey gradient, so nothing downstream complains
# and the operator watches a wall. Index 1 enumerates but never returns a
# frame. Only 2 is the external webcam.
#
# OpenCV has no name lookup on macOS, so this is an index and indices can
# shuffle when USB devices change. If the feed is not the rig, re-probe:
#   for i in 0 1 2 3: cv2.VideoCapture(i).read()
AURUM_CAMERA_INDEX=2

# Actuation ships OFF. Without this every move() returns ACTUATION_DISABLED
# and no frame is written, which looks exactly like a dead servo.
AURUM_ARDUINO_ENABLED=true

# FALSE, so the pan drives the machine. The cell is mounted and carries a
# calibration verified against a second known mass (392.2167 counts/g,
# docs/hardware.md), and with a stand-in mass in play the load cell is not in
# the loop at all: `PanMachine` never sees an arrival, so nothing is ever
# triggered and the servo never fires on its own. Mock mass is for a bench
# with no working cell.
#
# Set it true to run the vision and decision halves without touching the cell.
# Every figure derived from a stand-in is stamped SIMULATED and never `usable`.
# The demonstration fallback. ON, because the HX711 on this rig has read open
# (raw 0, then a 0/-1 dither) and an unreadable cell otherwise costs the whole
# demonstration: nothing arrives on the pan, so the automatic chain never runs.
#
# With this on, an item the cell cannot weigh is given a per-class stand-in
# mass, and the camera starts the cycle for as long as the cell cannot. Both
# reverse themselves the moment the cell reads again - the cell is asked first
# on every pass, and a real arrival always wins.
#
# EVERY FIGURE DERIVED FROM A STAND-IN IS STAMPED SIMULATED, all the way to the
# EPR ledger and the dashboard. None of them may be quoted as a measurement.
AURUM_DEMO_MOCK_MASS=true
