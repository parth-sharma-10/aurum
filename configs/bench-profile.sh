# Aurum — the BENCH configuration. Real board, simulated everything else.
#
#   set -a; source configs/bench-profile.sh; set +a
#   uvicorn app.api:app --port 8000
#
# THE ONLY REAL THING HERE IS THE SERVO COMMAND. The belt does not exist, the
# mass is not weighed, and the geometry was never measured. What this profile
# buys is the last link in the chain: a MOVE frame that leaves the host over a
# real serial port and makes a physical paddle stroke.
#
# Nothing in this file may be quoted as a measurement. It differs from
# configs/demo-profile.sh in exactly one respect — that file simulates the
# board too, so no byte reaches a port even when one is attached.
#
#   demo-profile.sh    everything simulated, nothing to plug in
#   bench-profile.sh   everything simulated EXCEPT the servo command
#
# Neither is the shipped default. configs/conveyor.yaml still ships
# `mode: NONE`, `simulation: false` and `arduino.enabled: false`, because that
# is what the physical machine currently is.
#
# Before sourcing this, confirm the board is the sorter and not the weight-only
# sketch — aurum_weight has no servo code at all, so nothing can move on it:
#
#   lsof /dev/cu.usbmodem101          # must be empty; close the IDE Serial Monitor
#   AURUM/1 PING <id>  ->  AURUM/1 PONG <id>     only aurum_sorter answers
#
# ---------------------------------------------------------------------------

# The demonstration belt. SIMULATION switches the routing layer onto the TEST
# distances in configs/conveyor.yaml and stamps every derived figure SIMULATED
# all the way to the EPR ledger.
#
# This line is load-bearing and not interchangeable with AURUM_SIMULATION. The
# real geometry block is UNMEASURED, and an unmeasured machine refuses to
# schedule rather than guessing — so without a SIMULATION belt no route is ever
# scheduled and the servo never fires, board or no board.
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
# port you have not looked up.
AURUM_ARDUINO_PORT=/dev/cu.usbmodem101

# Actuation ships OFF. Without this every move() returns ACTUATION_DISABLED
# and no frame is written, which looks exactly like a dead servo.
AURUM_ARDUINO_ENABLED=true

# STALE AS WRITTEN: the cell is no longer bypassed. The mounting was corrected
# on 2026-08-26 and it now carries a calibration verified against a second
# known mass (docs/hardware.md). Leave this true only to run the servo half of
# the bench without touching the cell — an item that cannot be weighed then
# gets a per-class stand-in mass (CPU 25 g, PCB 180 g, RAM 30 g, Connector 5 g),
# SIMULATED and never `usable`, and every figure derived from it says so.
# Set it false to exercise the real cell. Calibrating is still not a
# prerequisite for moving a servo.
AURUM_DEMO_MOCK_MASS=true
