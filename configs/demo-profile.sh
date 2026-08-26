# Aurum — the DEMONSTRATION configuration.
#
#   set -a; source configs/demo-profile.sh; set +a
#   uvicorn app.api:app --port 8000
#
# EVERY VALUE HERE IS A TEST VALUE. Nothing in this file was measured on a
# machine, and nothing in it may be quoted as a measurement. It exists so the
# whole chain — detect, track, weigh, value, grade, route, actuate, record —
# can be shown running on a laptop with no conveyor, no load cell and no board.
#
# It is deliberately NOT the shipped default. configs/conveyor.yaml ships
# `mode: NONE`, `simulation: false` and `arduino.enabled: false`, because that
# is what the physical machine currently is: no belt, actuation off, and an
# unmeasured geometry that makes the router refuse rather than guess.
#
# The eventual physical configuration is the other end of this file:
#
#     AURUM_CONVEYOR_MODE=ENCODER        a real belt reporting its own speed
#     AURUM_SIMULATION=false             commands reach a real serial port
#     AURUM_DEMO_MOCK_MASS=false         the load cell supplies the mass
#
# ---------------------------------------------------------------------------

# The demonstration belt. SIMULATION also switches the routing layer onto the
# TEST distances in configs/conveyor.yaml, and stamps every derived figure
# SIMULATED all the way to the EPR ledger.
AURUM_CONVEYOR_MODE=SIMULATION

# 10 cm/s = 0.10 m/s. Stated here rather than left to the shipped default, so
# the demonstration says out loud what speed it is pretending to run at. Slow
# on purpose: timing error from Python, serial latency and inference jitter is
# roughly +/-200 ms, which is +/-2 cm here and +/-10 cm at 50 cm/s.
AURUM_SIM_BELT_SPEED_CM_S=10.0

# HARDWARE_MODE=SIMULATION. The protocol, the acknowledgement and the fault
# latch all run against an in-process board; no byte reaches a serial port even
# if one is configured. Drop this line — and set AURUM_ARDUINO_PORT — to drive
# the real Arduino instead.
AURUM_SIMULATION=true

# Actuation ships OFF. The demonstration turns it on deliberately.
AURUM_ARDUINO_ENABLED=true

# The load cell is mechanically bypassed (see docs/hardware.md), so an item
# that cannot be weighed gets a per-class stand-in mass: CPU 25 g, PCB 180 g,
# RAM 30 g, Connector 5 g. The reading is SIMULATED and never `usable`, and
# every figure derived from it carries that status. Remove this line the day
# the cell is mounted and calibrated.
AURUM_DEMO_MOCK_MASS=true
