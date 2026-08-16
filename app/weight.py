"""Mass input for a batch — real HX711 load cell, or a labelled simulation.

The PPT's bench unit pairs the camera with an HX711 load cell. That hardware is
not attached to a laptop running the demo, so this module has two backends and
is loud about which one is active. A simulated reading is never presented as a
measurement: `simulated` is true in the batch record and the dashboard renders
"SIMULATED SENSOR" in place of the value's label.
"""

from __future__ import annotations

import math
import os
import time
from dataclasses import dataclass


@dataclass
class WeightReading:
    grams: float
    simulated: bool
    source: str

    def as_dict(self) -> dict:
        return {
            "grams": round(self.grams, 1),
            "kg": round(self.grams / 1000.0, 3),
            "simulated": self.simulated,
            "source": self.source,
            **({"warning": "SIMULATED SENSOR — not a physical measurement"}
               if self.simulated else {}),
        }


class WeightSource:
    """Base interface."""

    simulated = True
    label = "unknown"

    def read(self) -> WeightReading:  # pragma: no cover - interface
        raise NotImplementedError


class SimulatedLoadCell(WeightSource):
    """A clearly-labelled stand-in for the HX711.

    Produces a slowly drifting value with sensor-like jitter so the dashboard
    behaves as it would with hardware attached. It is seeded from a fixed base
    so a demo is repeatable, and it is *always* flagged as simulated.
    """

    simulated = True
    label = "simulated"

    def __init__(self, base_grams: float = 1840.0, jitter_g: float = 2.5) -> None:
        self.base = base_grams
        self.jitter = jitter_g
        self._t0 = time.time()

    def read(self) -> WeightReading:
        t = time.time() - self._t0
        drift = math.sin(t / 7.0) * self.jitter
        noise = math.sin(t * 13.0) * (self.jitter / 4.0)
        return WeightReading(self.base + drift + noise, True, "simulated load cell")


class HX711LoadCell(WeightSource):
    """Real HX711 over a serial link from an Arduino.

    Expects the Arduino sketch to print one calibrated reading in grams per
    line. Kept deliberately thin — if the port is not there, construction fails
    and the caller falls back to simulation with a visible mode change, rather
    than silently reporting invented numbers.
    """

    simulated = False
    label = "hx711"

    def __init__(self, port: str, baud: int = 9600, timeout: float = 1.0) -> None:
        try:
            import serial  # pyserial, optional dependency
        except ImportError as exc:
            raise RuntimeError(
                "pyserial is not installed; `pip install pyserial` to use a "
                "real HX711 load cell"
            ) from exc
        self._ser = serial.Serial(port, baud, timeout=timeout)
        self._last = 0.0
        time.sleep(2.0)  # Arduino resets on serial open

    def read(self) -> WeightReading:
        line = self._ser.readline().decode("ascii", errors="ignore").strip()
        try:
            self._last = float(line)
        except ValueError:
            pass  # keep the previous good reading rather than emitting garbage
        return WeightReading(self._last, False, f"HX711 @ {self._ser.port}")


def get_weight_source(mode: str = "auto", port: str | None = None) -> WeightSource:
    """Resolve a weight source.

    mode: "auto" | "hx711" | "simulated" | "off"
    """
    if mode == "off":
        return None  # type: ignore[return-value]
    port = port or os.environ.get("AURUM_HX711_PORT")

    if mode in ("auto", "hx711") and port:
        try:
            src = HX711LoadCell(port)
            print(f"[weight] HX711 connected on {port}")
            return src
        except Exception as exc:
            if mode == "hx711":
                raise
            print(f"[weight] HX711 unavailable ({exc}); falling back to SIMULATED")
    elif mode == "hx711":
        raise RuntimeError("mode=hx711 requires --hx711-port or AURUM_HX711_PORT")

    print("[weight] using SIMULATED load cell — readings are not measurements")
    return SimulatedLoadCell()
