"""Tests for the configuration layer.

The behaviour that matters is precedence and refusal: environment beats YAML
beats defaults, and a quantity nobody measured must never be handed out as if
it were a number.
"""

from __future__ import annotations

import re

import pytest

from app import config
from app.config import UNMEASURED, Config, ConfigError


def write(dir_path, name, text):
    (dir_path / name).write_text(text)
    return dir_path


@pytest.fixture
def empty_dir(tmp_path):
    """A config directory with no files: defaults only."""
    return tmp_path


class TestDefaults:
    def test_defaults_load_with_no_config_files_at_all(self, empty_dir):
        cfg = config.load(empty_dir, environ={})
        assert cfg["conveyor.camera.index"] == 0
        assert cfg["grading.fallback"] == "C"
        assert cfg["grading.policy.price_unavailable_policy"] == "mass_fraction_only"

    def test_every_spec_key_resolves(self, empty_dir):
        cfg = config.load(empty_dir, environ={})
        assert set(cfg.as_dict()) == set(config.SPEC)

    def test_defaults_are_reported_as_defaults(self, empty_dir):
        cfg = config.load(empty_dir, environ={})
        assert set(cfg.sources.values()) == {"default"}

    def test_a_missing_file_is_not_an_error(self, empty_dir):
        """A site that only overrides grading should not have to ship both files."""
        write(empty_dir, "grading.yaml", "grading:\n  fallback: B\n")
        cfg = config.load(empty_dir, environ={})
        assert cfg["grading.fallback"] == "B"
        assert cfg["conveyor.camera.index"] == 0


class TestYaml:
    def test_yaml_overrides_defaults(self, empty_dir):
        write(empty_dir, "conveyor.yaml", "conveyor:\n  camera:\n    index: 3\n")
        cfg = config.load(empty_dir, environ={})
        assert cfg["conveyor.camera.index"] == 3
        assert cfg.sources["conveyor.camera.index"] == "yaml"

    def test_the_shipped_config_files_load(self):
        """The files actually committed must parse and satisfy every validator."""
        cfg = config.load(environ={})
        assert cfg["grading.bin_a.preferred_classes"] == ["CPU", "Connector"]
        assert cfg["conveyor.detection.confidence"] == 0.35

    def test_malformed_yaml_names_the_file(self, empty_dir):
        write(empty_dir, "grading.yaml", "grading:\n  bin_a: [unclosed\n")
        with pytest.raises(ConfigError, match="not valid YAML"):
            config.load(empty_dir, environ={})

    def test_a_non_mapping_top_level_is_rejected(self, empty_dir):
        write(empty_dir, "grading.yaml", "- just\n- a\n- list\n")
        with pytest.raises(ConfigError, match="mapping at the top level"):
            config.load(empty_dir, environ={})

    def test_an_explicit_yaml_null_falls_back_to_the_default(self, empty_dir):
        write(empty_dir, "conveyor.yaml", "conveyor:\n  camera:\n    index: null\n")
        assert config.load(empty_dir, environ={})["conveyor.camera.index"] == 0


class TestEnvironmentPrecedence:
    def test_environment_beats_yaml(self, empty_dir):
        write(empty_dir, "conveyor.yaml", "conveyor:\n  camera:\n    index: 3\n")
        cfg = config.load(empty_dir, environ={"AURUM_CAMERA_INDEX": "7"})
        assert cfg["conveyor.camera.index"] == 7
        assert cfg.sources["conveyor.camera.index"] == "env"

    def test_environment_beats_defaults(self, empty_dir):
        cfg = config.load(empty_dir, environ={"AURUM_GRADING_FALLBACK": "B"})
        assert cfg["grading.fallback"] == "B"

    def test_an_absent_variable_leaves_yaml_in_place(self, empty_dir):
        write(empty_dir, "conveyor.yaml", "conveyor:\n  camera:\n    index: 3\n")
        cfg = config.load(empty_dir, environ={"UNRELATED": "x"})
        assert cfg["conveyor.camera.index"] == 3

    def test_an_empty_variable_is_treated_as_unset(self, empty_dir):
        """`export AURUM_CAMERA_INDEX=` must not parse as a value."""
        write(empty_dir, "conveyor.yaml", "conveyor:\n  camera:\n    index: 3\n")
        cfg = config.load(empty_dir, environ={"AURUM_CAMERA_INDEX": "  "})
        assert cfg["conveyor.camera.index"] == 3

    def test_a_list_can_come_from_a_comma_separated_variable(self, empty_dir):
        cfg = config.load(empty_dir, environ={"AURUM_BIN_A_PREFERRED_CLASSES": "CPU, PCB"})
        assert cfg["grading.bin_a.preferred_classes"] == ["CPU", "PCB"]


class TestUnmeasured:
    def test_unmeasured_is_falsy_and_not_zero(self, empty_dir):
        speed = config.load(empty_dir, environ={})["conveyor.belt.speed_cm_s"]
        assert speed is UNMEASURED
        assert not speed
        assert speed != 0
        assert repr(speed) == "UNMEASURED"

    def test_require_refuses_an_unmeasured_value_and_says_how_to_fix_it(self, empty_dir):
        cfg = config.load(empty_dir, environ={})
        with pytest.raises(ConfigError, match="AURUM_BELT_SPEED_CM_S"):
            cfg.require("conveyor.belt.speed_cm_s")

    def test_require_returns_a_measured_value(self, empty_dir):
        cfg = config.load(empty_dir, environ={"AURUM_BELT_SPEED_CM_S": "12.5"})
        assert cfg.require("conveyor.belt.speed_cm_s") == 12.5

    def test_unmeasured_can_be_set_from_the_environment_too(self, empty_dir):
        write(empty_dir, "conveyor.yaml", "conveyor:\n  belt:\n    speed_cm_s: 20\n")
        cfg = config.load(empty_dir, environ={"AURUM_BELT_SPEED_CM_S": "UNMEASURED"})
        assert cfg["conveyor.belt.speed_cm_s"] is UNMEASURED

    def test_the_shipped_conveyor_geometry_is_unmeasured(self):
        """Guard: shipping a guessed belt geometry would route items by luck."""
        cfg = config.load(environ={})
        for key in (
            "conveyor.belt.speed_cm_s",
            "conveyor.geometry.camera_to_load_cell_cm",
            "conveyor.geometry.camera_to_servo_a_cm",
            "conveyor.geometry.camera_to_servo_b_cm",
            "conveyor.timing.servo_actuation_delay_ms",
            "conveyor.weight.calibration_factor",
        ):
            assert cfg[key] is UNMEASURED, f"{key} must ship unmeasured"


class TestValidation:
    @pytest.mark.parametrize(
        ("env", "message"),
        [
            ({"AURUM_CAMERA_INDEX": "two"}, "whole number"),
            ({"AURUM_BELT_SPEED_CM_S": "fast"}, "expected a number"),
            ({"AURUM_BELT_SPEED_CM_S": "-4"}, "must not be negative"),
            ({"AURUM_DETECTION_CONFIDENCE": "1.5"}, "between 0 and 1"),
            ({"AURUM_BIN_A_MIN_CONFIDENCE": "-0.1"}, "between 0 and 1"),
            ({"AURUM_SIMULATION": "maybe"}, "expected true or false"),
            ({"AURUM_GRADING_FALLBACK": "D"}, "must be one of"),
            ({"AURUM_PRICE_UNAVAILABLE_POLICY": "guess"}, "must be one of"),
        ],
    )
    def test_invalid_values_fail_with_a_message_naming_the_key(self, empty_dir, env, message):
        with pytest.raises(ConfigError, match=message):
            config.load(empty_dir, environ=env)

    def test_the_error_names_the_offending_setting(self, empty_dir):
        with pytest.raises(ConfigError, match="conveyor.camera.index"):
            config.load(empty_dir, environ={"AURUM_CAMERA_INDEX": "two"})

    def test_an_unknown_setting_is_an_error_not_a_none(self, empty_dir):
        cfg = config.load(empty_dir, environ={})
        with pytest.raises(ConfigError, match="unknown setting"):
            cfg["grading.bin_z.threshold"]


class TestSections:
    def test_conveyor_configuration_loads_as_a_section(self):
        weight = config.load(environ={}).section("conveyor.weight")
        assert weight["stability_window_ms"] == 500.0
        assert weight["stability_tolerance_g"] == 0.5
        assert weight["hx711_port"] is None

    def test_grading_configuration_loads_as_a_section(self):
        bin_a = config.load(environ={}).section("grading.bin_a")
        assert bin_a["minimum_precious_fraction_ppm"] == 1000.0
        assert bin_a["minimum_confidence"] == 0.75
        assert bin_a["minimum_precious_value"] is UNMEASURED

    def test_a_section_prefix_does_not_leak_siblings(self):
        assert "minimum_confidence" not in config.load(environ={}).section("grading.policy")


class TestSimulation:
    def test_simulation_is_off_by_default(self):
        assert config.load(environ={})["conveyor.runtime.simulation"] is False

    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
    def test_simulation_can_be_switched_on_however_it_is_spelled(self, empty_dir, value):
        cfg = config.load(empty_dir, environ={"AURUM_SIMULATION": value})
        assert cfg["conveyor.runtime.simulation"] is True

    @pytest.mark.parametrize("value", ["0", "false", "no", "off"])
    def test_simulation_can_be_switched_off(self, empty_dir, value):
        cfg = config.load(empty_dir, environ={"AURUM_SIMULATION": value})
        assert cfg["conveyor.runtime.simulation"] is False


class TestPriceUnavailablePolicy:
    def test_the_default_keeps_sorting_without_a_price(self):
        cfg = config.load(environ={})
        assert cfg["grading.policy.price_unavailable_policy"] == "mass_fraction_only"

    def test_the_strict_policy_is_selectable(self, empty_dir):
        cfg = config.load(empty_dir, environ={"AURUM_PRICE_UNAVAILABLE_POLICY": "route_to_c"})
        assert cfg["grading.policy.price_unavailable_policy"] == "route_to_c"

    def test_no_third_policy_can_be_invented_by_a_typo(self, empty_dir):
        with pytest.raises(ConfigError, match="mass_fraction_only, route_to_c"):
            config.load(empty_dir, environ={"AURUM_PRICE_UNAVAILABLE_POLICY": "fallback"})


class TestNoCredentialsInVersionControl:
    """Configuration under version control must never carry a credential."""

    CREDENTIAL = re.compile(r"(api[_-]?key|secret|token|passwd|password)\s*[:=]\s*\S+", re.I)

    def test_committed_config_holds_no_populated_credential(self):
        for path in sorted(config.CONFIG_DIR.glob("*.yaml")):
            for line in path.read_text().splitlines():
                if line.lstrip().startswith("#"):
                    continue
                assert not self.CREDENTIAL.search(line), f"{path.name}: {line!r}"

    def test_the_environment_template_ships_no_values(self):
        """The committed template must carry keys only, never populated values."""
        template = config.ROOT / "env.example"
        for line in template.read_text().splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            _key, _, value = stripped.partition("=")
            assert value.strip() == "", f"env.example must not ship a value: {stripped!r}"


class TestConfigObject:
    def test_membership_and_get_default(self, empty_dir):
        cfg = config.load(empty_dir, environ={})
        assert "grading.fallback" in cfg
        assert "grading.nope" not in cfg
        assert cfg.get("grading.nope", "fallback-value") == "fallback-value"

    def test_as_dict_is_a_copy(self, empty_dir):
        cfg = config.load(empty_dir, environ={})
        snapshot = cfg.as_dict()
        snapshot["grading.fallback"] = "A"
        assert cfg["grading.fallback"] == "C"

    def test_config_can_be_built_directly_for_tests(self):
        cfg = Config({"a.b": 1}, {"a.b": "default"})
        assert cfg["a.b"] == 1
