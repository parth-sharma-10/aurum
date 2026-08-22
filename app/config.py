"""Every tunable value in Aurum, resolved in one place.

Three sources, lowest priority first:

    defaults      the SPEC table below — what Aurum does with no config at all
    YAML          configs/conveyor.yaml, configs/grading.yaml
    environment   AURUM_* variables

Environment wins, because that is what a deployment or a demo laptop overrides
without editing a file that is under version control. Command-line arguments
still beat all three, but they are applied by the caller at its own argparse
site rather than here; this module has no opinion about argv.

Two ideas carry the honesty requirement into configuration:

`UNMEASURED` is a real value, not a missing one. A belt speed nobody has put a
tape measure to is not zero and is not a guess — it is unmeasured, it is falsy,
and `require()` refuses to hand it out. Downstream that means an item routes to
C rather than the machine pretending to know where it is.

Every resolved value records where it came from (`sources`), so a threshold
shown on the dashboard can say whether it is a shipped default, a site's YAML,
or something someone exported in a shell that morning.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "configs"

# The top-level key of each file is also the first segment of every key it
# owns, so `configs/grading.yaml` holds `grading:` and nothing else. That keeps
# dotted lookup uniform across files with no per-file special cases.
CONFIG_FILES = ("conveyor.yaml", "grading.yaml", "pricing.yaml", "tracking.yaml")

UNMEASURED_TOKEN = "UNMEASURED"


class ConfigError(ValueError):
    """Configuration that cannot be used, with enough detail to fix it."""


class _Unmeasured:
    """A physical quantity that has not been measured on the real machine.

    Falsy on purpose: `if not cfg.get("conveyor.belt.speed_cm_s")` is the check
    every consumer wants, and it must never be confused with a speed of zero.
    """

    __slots__ = ()

    def __bool__(self) -> bool:
        return False

    def __repr__(self) -> str:
        return UNMEASURED_TOKEN


UNMEASURED = _Unmeasured()


def _int(value: Any, key: str) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{key}: expected a whole number, got {value!r}") from exc


def _float(value: Any, key: str) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{key}: expected a number, got {value!r}") from exc


def _fraction(value: Any, key: str) -> float:
    """A confidence or ratio. Outside 0..1 it is a typo, not a preference."""
    out = _float(value, key)
    if not 0.0 <= out <= 1.0:
        raise ConfigError(f"{key}: must be between 0 and 1, got {out}")
    return out


def _non_negative(value: Any, key: str) -> float:
    out = _float(value, key)
    if out < 0:
        raise ConfigError(f"{key}: must not be negative, got {out}")
    return out


def _bool(value: Any, key: str) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"{key}: expected true or false, got {value!r}")


def _text(value: Any, key: str) -> str:
    return str(value).strip()


def _optional_text(value: Any, key: str) -> str | None:
    """A port or path that is legitimately absent until hardware is attached."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _text_list(value: Any, key: str) -> list[str]:
    """A YAML list, or a comma-separated string from the environment."""
    if isinstance(value, (list, tuple)):
        items = [str(v).strip() for v in value]
    else:
        items = [part.strip() for part in str(value).split(",")]
    return [item for item in items if item]


def _one_of(*allowed: str):
    def parse(value: Any, key: str) -> str:
        text = str(value).strip()
        if text not in allowed:
            raise ConfigError(f"{key}: must be one of {', '.join(allowed)}, got {text!r}")
        return text

    return parse


# key -> (parser, default, environment variable)
#
# Values marked UNMEASURED must be measured on the physical machine. Values
# annotated "engineering approximation" in configs/*.yaml are prototype
# starting points and are not research-derived; see docs/COMPLETION_PLAN.md.
SPEC: dict[str, tuple] = {
    "conveyor.camera.index": (_int, 0, "AURUM_CAMERA_INDEX"),
    "conveyor.camera.backend": (_text, "auto", "AURUM_CAMERA_BACKEND"),
    "conveyor.camera.width": (_int, 1280, "AURUM_CAMERA_WIDTH"),
    "conveyor.camera.height": (_int, 720, "AURUM_CAMERA_HEIGHT"),
    "conveyor.detection.confidence": (_fraction, 0.35, "AURUM_DETECTION_CONFIDENCE"),
    "conveyor.detection.iou": (_fraction, 0.5, "AURUM_DETECTION_IOU"),
    "conveyor.belt.speed_cm_s": (_non_negative, UNMEASURED, "AURUM_BELT_SPEED_CM_S"),
    "conveyor.geometry.camera_to_load_cell_cm": (
        _non_negative,
        UNMEASURED,
        "AURUM_CAMERA_TO_LOAD_CELL_CM",
    ),
    "conveyor.geometry.camera_to_servo_a_cm": (
        _non_negative,
        UNMEASURED,
        "AURUM_CAMERA_TO_SERVO_A_CM",
    ),
    "conveyor.geometry.camera_to_servo_b_cm": (
        _non_negative,
        UNMEASURED,
        "AURUM_CAMERA_TO_SERVO_B_CM",
    ),
    "conveyor.timing.offset_ms": (_float, 0.0, "AURUM_TIMING_OFFSET_MS"),
    "conveyor.timing.servo_actuation_delay_ms": (
        _non_negative,
        UNMEASURED,
        "AURUM_SERVO_ACTUATION_DELAY_MS",
    ),
    "conveyor.weight.stability_window_ms": (
        _non_negative,
        500.0,
        "AURUM_WEIGHT_STABILITY_WINDOW_MS",
    ),
    "conveyor.weight.stability_tolerance_g": (
        _non_negative,
        0.5,
        "AURUM_WEIGHT_STABILITY_TOLERANCE_G",
    ),
    "conveyor.weight.timeout_s": (_non_negative, 5.0, "AURUM_WEIGHT_TIMEOUT_S"),
    "conveyor.weight.filter_samples": (_int, 5, "AURUM_WEIGHT_FILTER_SAMPLES"),
    "conveyor.weight.calibration_factor": (
        _float,
        UNMEASURED,
        "AURUM_WEIGHT_CALIBRATION_FACTOR",
    ),
    "conveyor.weight.hx711_port": (_optional_text, None, "AURUM_HX711_PORT"),
    "conveyor.arduino.port": (_optional_text, None, "AURUM_ARDUINO_PORT"),
    # Actuation ships OFF. Nothing moves until someone turns this on with the
    # board connected and bench-verified.
    "conveyor.arduino.enabled": (_bool, False, "AURUM_ARDUINO_ENABLED"),
    "conveyor.arduino.baudrate": (_int, 115200, "AURUM_ARDUINO_BAUDRATE"),
    "conveyor.arduino.timeout_s": (_non_negative, 1.0, "AURUM_ARDUINO_TIMEOUT_S"),
    # The board acknowledges AFTER the paddle has finished its stroke, so this
    # must exceed conveyor.servo.actuation_ms plus travel. 2 s against a 700 ms
    # hold leaves room for a slower servo without waiting on a dead link.
    "conveyor.arduino.ack_timeout_ms": (_non_negative, 2000.0, "AURUM_ARDUINO_ACK_TIMEOUT_MS"),
    # Servo geometry. BENCH/TEST values from independent servo testing, not
    # calibrated against a conveyor - there is no conveyor. The board reads
    # these at boot via the CFG frame, so tuning them needs no reflash.
    "conveyor.servo.rest_angle_deg": (_non_negative, 0.0, "AURUM_SERVO_REST_ANGLE_DEG"),
    "conveyor.servo.push_angle_deg": (_non_negative, 90.0, "AURUM_SERVO_PUSH_ANGLE_DEG"),
    "conveyor.servo.actuation_ms": (_non_negative, 700.0, "AURUM_SERVO_ACTUATION_MS"),
    # The demonstration profile. TEST values, used ONLY when
    # conveyor.runtime.simulation is true. See configs/conveyor.yaml.
    "conveyor.simulation.belt_speed_cm_s": (_non_negative, 20.0, "AURUM_SIM_BELT_SPEED_CM_S"),
    "conveyor.simulation.camera_to_load_cell_cm": (
        _non_negative,
        25.0,
        "AURUM_SIM_CAMERA_TO_LOAD_CELL_CM",
    ),
    "conveyor.simulation.camera_to_servo_a_cm": (
        _non_negative,
        60.0,
        "AURUM_SIM_CAMERA_TO_SERVO_A_CM",
    ),
    "conveyor.simulation.camera_to_servo_b_cm": (
        _non_negative,
        90.0,
        "AURUM_SIM_CAMERA_TO_SERVO_B_CM",
    ),
    "conveyor.simulation.servo_actuation_delay_ms": (
        _non_negative,
        150.0,
        "AURUM_SIM_SERVO_ACTUATION_DELAY_MS",
    ),
    "conveyor.simulation.timing_offset_ms": (_float, 0.0, "AURUM_SIM_TIMING_OFFSET_MS"),
    # ------------------------------------------------------------------
    # DEMONSTRATION FALLBACK - a stand-in mass, for when the load cell
    # cannot supply one.
    #
    # Ships OFF. With it on, an item that could not be weighed is given
    # `demo.mock_mass.grams` so the rest of the pipeline can be shown
    # running. That mass is FABRICATED: it is stamped SIMULATED, it can
    # never reach MEASURED, and every estimate computed from it carries
    # overall_status SIMULATED all the way to the dashboard.
    #
    # It exists so a broken load cell does not cost the whole
    # demonstration. It is not a substitute for calibrating the cell, and
    # nothing computed from it may be quoted as a measurement.
    # ------------------------------------------------------------------
    "demo.mock_mass.enabled": (_bool, False, "AURUM_DEMO_MOCK_MASS"),
    #: Fallback for a class with no entry below.
    "demo.mock_mass.grams": (_non_negative, 180.0, "AURUM_DEMO_MOCK_MASS_G"),
    # Per class, because one flat mass makes the ppm figures wrong in a way a
    # judge can spot: a CPU is not 180 g, and pretending it is drags its
    # precious fraction down by a factor of seven. These are plausible typical
    # masses for the class, TEST values chosen to keep the arithmetic sensible.
    # They are not measurements of anything and nothing was weighed to get them.
    "demo.mock_mass.cpu_g": (_non_negative, 25.0, "AURUM_DEMO_MOCK_MASS_CPU_G"),
    "demo.mock_mass.pcb_g": (_non_negative, 180.0, "AURUM_DEMO_MOCK_MASS_PCB_G"),
    "demo.mock_mass.ram_g": (_non_negative, 30.0, "AURUM_DEMO_MOCK_MASS_RAM_G"),
    "demo.mock_mass.connector_g": (_non_negative, 5.0, "AURUM_DEMO_MOCK_MASS_CONNECTOR_G"),
    "conveyor.runtime.simulation": (_bool, False, "AURUM_SIMULATION"),
    "conveyor.runtime.host": (_text, "127.0.0.1", "AURUM_HOST"),
    "conveyor.runtime.port": (_int, 8000, "AURUM_PORT"),
    "grading.bin_a.minimum_precious_fraction_ppm": (
        _non_negative,
        1000.0,
        "AURUM_BIN_A_MIN_PRECIOUS_PPM",
    ),
    "grading.bin_a.minimum_confidence": (_fraction, 0.75, "AURUM_BIN_A_MIN_CONFIDENCE"),
    "grading.bin_a.minimum_precious_value": (
        _non_negative,
        UNMEASURED,
        "AURUM_BIN_A_MIN_PRECIOUS_VALUE",
    ),
    "grading.bin_a.preferred_classes": (
        _text_list,
        ["CPU", "Connector"],
        "AURUM_BIN_A_PREFERRED_CLASSES",
    ),
    "grading.bin_b.minimum_precious_fraction_ppm": (
        _non_negative,
        100.0,
        "AURUM_BIN_B_MIN_PRECIOUS_PPM",
    ),
    "grading.bin_b.minimum_confidence": (_fraction, 0.60, "AURUM_BIN_B_MIN_CONFIDENCE"),
    "grading.fallback": (_one_of("A", "B", "C"), "C", "AURUM_GRADING_FALLBACK"),
    "grading.policy.class_aware": (_bool, True, "AURUM_GRADING_CLASS_AWARE"),
    "grading.policy.price_unavailable_policy": (
        _one_of("mass_fraction_only", "route_to_c"),
        "mass_fraction_only",
        "AURUM_PRICE_UNAVAILABLE_POLICY",
    ),
    "pricing.provider": (_one_of("unavailable", "static"), "unavailable", "AURUM_PRICE_PROVIDER"),
    "pricing.max_age_seconds": (_non_negative, 900.0, "AURUM_PRICE_MAX_AGE_SECONDS"),
    "tracking.tracker": (_text, "bytetrack.yaml", "AURUM_TRACKER"),
    "tracking.max_missing_frames": (_int, 15, "AURUM_TRACK_MAX_MISSING_FRAMES"),
    "tracking.min_detections_to_confirm": (_int, 3, "AURUM_TRACK_MIN_DETECTIONS"),
}


class Config:
    """Resolved settings, plus where each one came from."""

    def __init__(self, values: dict[str, Any], sources: dict[str, str]) -> None:
        self._values = values
        self.sources = sources

    def __contains__(self, key: str) -> bool:
        return key in self._values

    def __getitem__(self, key: str) -> Any:
        try:
            return self._values[key]
        except KeyError:
            raise ConfigError(f"unknown setting {key!r}") from None

    def get(self, key: str, default: Any = None) -> Any:
        return self._values.get(key, default)

    def require(self, key: str) -> Any:
        """A value that must actually be known, or an error naming the fix.

        This is the seam where an unmeasured belt refuses to become a guess.
        """
        value = self[key]
        if value is UNMEASURED:
            _parser, _default, env = SPEC[key]
            raise ConfigError(
                f"{key} is UNMEASURED. Measure it on the machine, then set it in "
                f"configs/ or export {env}."
            )
        return value

    def section(self, prefix: str) -> dict[str, Any]:
        """Every setting under a dotted prefix, keyed by its remaining path."""
        head = prefix.rstrip(".") + "."
        return {k[len(head) :]: v for k, v in self._values.items() if k.startswith(head)}

    def as_dict(self) -> dict[str, Any]:
        return dict(self._values)


def _read_yaml(config_dir: Path) -> dict[str, Any]:
    """Merge the config files into one tree. A missing file is not an error."""
    merged: dict[str, Any] = {}
    for name in CONFIG_FILES:
        path = config_dir / name
        if not path.exists():
            continue
        try:
            loaded = yaml.safe_load(path.read_text()) or {}
        except yaml.YAMLError as exc:
            raise ConfigError(f"{path} is not valid YAML: {exc}") from exc
        if not isinstance(loaded, dict):
            raise ConfigError(f"{path}: expected a mapping at the top level")
        merged.update(loaded)
    return merged


def _dig(tree: dict[str, Any], key: str) -> tuple[bool, Any]:
    """Walk a dotted path. Returns (found, value) so that a legitimate None
    stored in YAML is not confused with an absent key."""
    node: Any = tree
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            return False, None
        node = node[part]
    return True, node


def load(config_dir: Path | None = None, environ: dict[str, str] | None = None) -> Config:
    """Resolve every setting: defaults, then YAML, then environment."""
    config_dir = CONFIG_DIR if config_dir is None else Path(config_dir)
    environ = os.environ if environ is None else environ
    tree = _read_yaml(config_dir)

    values: dict[str, Any] = {}
    sources: dict[str, str] = {}
    for key, (parser, default, env_var) in SPEC.items():
        raw, source = default, "default"

        found, from_yaml = _dig(tree, key)
        if found and from_yaml is not None:
            raw, source = from_yaml, "yaml"

        from_env = environ.get(env_var)
        if from_env is not None and from_env.strip() != "":
            raw, source = from_env, "env"

        if source == "default":
            values[key], sources[key] = default, source
            continue

        if isinstance(raw, str) and raw.strip().upper() == UNMEASURED_TOKEN:
            values[key], sources[key] = UNMEASURED, source
            continue

        values[key], sources[key] = parser(raw, key), source

    return Config(values, sources)
