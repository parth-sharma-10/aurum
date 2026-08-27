"""Tests for the belt speed sources and the dynamic ETA.

The property under test: a speed is either a number whose provenance is on the
reading, or an explicit absence. No path here produces a plausible default for
a belt nobody has measured, and no path lets a simulated speed be read as a
measured one.

Every distance and speed in this file is a TEST value.
"""

from __future__ import annotations

import pytest

from app import config
from app.routing.conveyor import (
    Conveyor,
    ConveyorMode,
    EncoderSpeed,
    ManualSpeed,
    SimulatedSpeed,
    SpeedStatus,
    hardware_mode,
)
from app.routing.geometry import Geometry, RoutingMode
from app.routing.scheduler import RouteReason, RouteStatus, RoutingScheduler

T0 = 1_000_000.0


def cfg(**environ):
    return config.load(environ=environ)


def geometry(**overrides) -> Geometry:
    base = {
        "mode": RoutingMode.SIMULATED,
        "belt_speed_cm_s": 10.0,
        "camera_to_load_cell_cm": 25.0,
        "camera_to_servo_a_cm": 60.0,
        "camera_to_servo_b_cm": 90.0,
        "servo_actuation_delay_ms": 150.0,
        "timing_offset_ms": 0.0,
    }
    return Geometry(**{**base, **overrides})


class TestTheDemonstrationProfile:
    """`configs/demo-profile.sh` is checked in, so what it resolves to is a
    testable fact rather than something a reader has to trust a comment about.

    Two properties: it produces the demonstration belt at exactly 0.10 m/s,
    stamped SIMULATED; and sourcing it changes nothing on disk, so the shipped
    default stays the no-belt machine.
    """

    PROFILE = config.CONFIG_DIR / "demo-profile.sh"

    def environ(self) -> dict:
        out = {}
        for line in self.PROFILE.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                key, _, value = line.partition("=")
                out[key] = value
        return out

    def test_it_selects_the_simulated_belt(self):
        assert Conveyor.from_config(cfg(**self.environ())).mode is ConveyorMode.SIMULATION

    def test_the_belt_runs_at_a_tenth_of_a_metre_per_second(self):
        speed = Conveyor.from_config(cfg(**self.environ())).speed()
        assert speed.cm_s == pytest.approx(10.0)
        assert speed.m_s == pytest.approx(0.10)

    def test_that_speed_is_never_presented_as_measured(self):
        speed = Conveyor.from_config(cfg(**self.environ())).speed()
        assert speed.status is SpeedStatus.SIMULATED
        assert "Nothing was measured to get this" in speed.reason

    def test_it_runs_the_board_in_simulation(self):
        assert hardware_mode(cfg(**self.environ())) == "SIMULATION"

    def test_it_does_not_change_the_shipped_default(self):
        """The profile is opt-in. Without it the machine is still beltless."""
        assert Conveyor.from_config(cfg()).mode is ConveyorMode.NONE
        assert hardware_mode(cfg()) == "PHYSICAL"


class TestTheBenchProfile:
    """`configs/bench-profile.sh` drives a real board through the belt model.

    The demonstration is the whole chain: the cell measures, the engine decides
    a bin, the scheduler works out when the item would reach that bin's paddle,
    and the servo fires at that moment. The wait is the feature.

    Two settings make it work and both are easy to get backwards. Leaving
    `AURUM_SIMULATION` unset is what reaches the serial port. `CONVEYOR_MODE`
    on SIMULATION is what makes a route schedulable, because the real geometry
    block is UNMEASURED and an unmeasured machine refuses to schedule rather
    than guessing.

    `DEMO_MOCK_MASS` is now ON. It used to be off, because a stand-in mass
    alone meant the pan never saw an arrival and nothing was ever triggered.
    That is no longer what it does: the fallback also hands the ARRIVAL to the
    camera for as long as the cell cannot supply one, so the chain runs on a
    rig whose HX711 reads open — which is what this one has been doing. The
    cell is still asked first on every pass and still wins whenever it reads.
    """

    PROFILE = config.CONFIG_DIR / "bench-profile.sh"

    def environ(self) -> dict:
        out = {}
        for line in self.PROFILE.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                key, _, value = line.partition("=")
                out[key] = value
        return out

    def test_it_drives_a_real_board(self):
        assert hardware_mode(cfg(**self.environ())) == "PHYSICAL"

    def test_it_never_sets_the_simulation_flag(self):
        """Setting it would silently take the board back off the wire."""
        assert "AURUM_SIMULATION" not in self.environ()

    def test_it_runs_the_belt_timing_model(self):
        """The wait between decision and paddle is the demonstration."""
        belt = Conveyor.from_config(cfg(**self.environ()))
        assert belt.mode is ConveyorMode.SIMULATION
        assert belt.present is True

    def test_the_belt_is_never_presented_as_measured(self):
        """Only the servo command is real. Every derived figure says so."""
        speed = Conveyor.from_config(cfg(**self.environ())).speed()
        assert speed.cm_s == pytest.approx(10.0)
        assert speed.status is SpeedStatus.SIMULATED

    def test_the_fallback_is_armed_so_an_open_cell_cannot_stop_the_demonstration(self):
        """The cell on this rig has read open. Armed, the camera starts the
        cycle for as long as the cell cannot, and the stand-in mass carries a
        SIMULATED stamp everywhere it surfaces."""
        assert self.environ()["AURUM_DEMO_MOCK_MASS"] == "true"

    def test_the_load_cell_is_still_preferred_whenever_it_reads(self):
        """Armed is not the same as forced. `camera_trigger.enabled` stays off,
        so the pan is asked first on every pass and a real arrival always wins;
        the camera is only reached once the cell has refused."""
        assert "AURUM_DEMO_CAMERA_TRIGGER" not in self.environ()
        assert cfg(**self.environ())["demo.camera_trigger.enabled"] is False

    def test_it_resolves_a_port_and_enables_actuation(self):
        env = self.environ()
        # "auto" or an explicit node. This board has come up as usbmodem101 and
        # as usbmodem1101, and a stale name presents as a board that will not
        # connect thirty seconds before a demonstration.
        assert env["AURUM_ARDUINO_PORT"] == "auto" or env["AURUM_ARDUINO_PORT"].startswith("/dev/")
        assert env["AURUM_ARDUINO_ENABLED"] == "true"

    def test_it_does_not_change_the_shipped_default(self):
        assert Conveyor.from_config(cfg()).mode is ConveyorMode.NONE


class TestTheShippedDefault:
    def test_there_is_no_belt(self):
        """The machine has no conveyor, so the software says it has no conveyor."""
        belt = Conveyor.from_config(cfg())
        assert belt.mode is ConveyorMode.NONE
        assert belt.present is False

    def test_no_belt_means_no_speed_and_no_guess(self):
        speed = Conveyor.from_config(cfg()).speed()
        assert speed.cm_s is None
        assert speed.status is SpeedStatus.UNAVAILABLE
        assert "no belt" in speed.reason

    def test_no_belt_means_no_eta(self):
        assert Conveyor.from_config(cfg()).eta_seconds(60.0) is None

    def test_the_snapshot_says_the_operator_carries_the_object(self):
        assert "operator carries" in Conveyor.from_config(cfg()).snapshot()["note"]


class TestTheMockConveyor:
    def test_it_runs_at_a_tenth_of_a_metre_per_second(self):
        speed = Conveyor.from_config(cfg(AURUM_CONVEYOR_MODE="SIMULATION")).speed()
        assert speed.cm_s == 10.0
        assert speed.m_s == pytest.approx(0.10)

    def test_it_is_labelled_simulated_and_never_measured(self):
        speed = Conveyor.from_config(cfg(AURUM_CONVEYOR_MODE="SIMULATION")).speed()
        assert speed.status is SpeedStatus.SIMULATED
        assert "Nothing was measured" in speed.reason

    def test_the_speed_is_configurable(self):
        environ = {"AURUM_CONVEYOR_MODE": "SIMULATION", "AURUM_SIM_BELT_SPEED_CM_S": "25"}
        assert Conveyor.from_config(cfg(**environ)).speed().cm_s == 25.0

    def test_it_brings_the_demonstration_geometry_with_it(self):
        """A mock belt needs mock distances; both are stamped SIMULATED."""
        belt = Conveyor.from_config(cfg(AURUM_CONVEYOR_MODE="SIMULATION"))
        geo = belt.live_geometry()
        assert geo.mode is RoutingMode.SIMULATED
        assert geo.camera_to_servo_a_cm == 60.0
        assert geo.camera_to_servo_b_cm == 90.0

    def test_a_zero_simulated_speed_is_refused_not_divided_by(self):
        speed = SimulatedSpeed(0.0).read()
        assert speed.status is SpeedStatus.UNAVAILABLE
        assert speed.usable is False


class TestManualSpeed:
    def test_an_unentered_speed_is_absent_not_zero(self):
        speed = ManualSpeed(None).read()
        assert speed.cm_s is None
        assert speed.status is SpeedStatus.UNAVAILABLE

    def test_an_entered_speed_is_real_but_not_live(self):
        speed = ManualSpeed(15.0).read()
        assert speed.status is SpeedStatus.MANUAL
        assert speed.usable
        assert "cannot notice the belt slowing" in speed.reason

    def test_it_can_be_re_entered(self):
        source = ManualSpeed(15.0)
        assert source.set(30.0).cm_s == 30.0

    def test_the_config_path_stays_unmeasured_until_somebody_measures(self):
        belt = Conveyor.from_config(cfg(AURUM_CONVEYOR_MODE="MANUAL"))
        assert belt.speed().status is SpeedStatus.UNAVAILABLE
        assert "conveyor.manual.belt_speed_cm_s" in belt.speed().reason


class TestEncoderSpeed:
    """pulses -> distance -> speed -> timestamp -> health."""

    def encoder(self, clock=None, **kwargs):
        kwargs.setdefault("pulses_per_revolution", 20)
        kwargs.setdefault("roller_circumference_cm", 20.0)
        return EncoderSpeed(clock=clock or (lambda: 0.0), **kwargs)

    def test_distance_per_pulse_is_the_circumference_over_the_count(self):
        assert self.encoder().distance_per_pulse_cm == 1.0

    def test_one_sample_is_a_position_not_a_velocity(self):
        speed = self.encoder().update(0, at=0.0)
        assert speed.cm_s is None
        assert "second sample" in speed.reason

    def test_two_samples_differentiate_into_a_speed(self):
        source = self.encoder()
        source.update(0, at=0.0)
        # 10 pulses x 1 cm over 1 s.
        speed = source.update(10, at=1.0)
        assert speed.cm_s == pytest.approx(10.0)
        assert speed.status is SpeedStatus.MEASURED
        assert speed.pulses == 10

    def test_a_faster_belt_reads_faster(self):
        source = self.encoder()
        source.update(0, at=0.0)
        assert source.update(50, at=1.0).cm_s == pytest.approx(50.0)

    def test_a_stationary_belt_is_not_a_usable_speed(self):
        source = self.encoder()
        source.update(100, at=0.0)
        speed = source.update(100, at=1.0)
        assert speed.cm_s == 0.0
        assert speed.usable is False
        assert "has not moved" in speed.reason

    def test_an_unmeasured_roller_makes_every_reading_unavailable(self):
        source = self.encoder(roller_circumference_cm=None)
        assert source.distance_per_pulse_cm is None
        assert "UNMEASURED" in source.update(10, at=1.0).reason
        assert source.read().status is SpeedStatus.UNAVAILABLE

    def test_a_counter_that_wraps_is_discarded_not_read_as_reverse(self):
        source = self.encoder()
        source.update(65000, at=0.0)
        speed = source.update(5, at=1.0)
        assert speed.cm_s is None
        assert "went backwards" in speed.reason

    def test_two_samples_at_the_same_instant_are_not_differentiated(self):
        source = self.encoder()
        source.update(0, at=1.0)
        assert source.update(10, at=1.0).cm_s is None

    def test_an_encoder_that_has_never_reported_says_so(self):
        speed = self.encoder().read()
        assert speed.status is SpeedStatus.UNAVAILABLE
        assert "never reported" in speed.reason

    def test_a_silent_encoder_goes_stale_rather_than_holding_its_last_speed(self):
        now = [0.0]
        source = self.encoder(clock=lambda: now[0], timeout_s=2.0)
        source.update(0, at=0.0)
        source.update(10, at=1.0)
        now[0] = 1.5
        assert source.read().status is SpeedStatus.MEASURED
        now[0] = 4.0
        stale = source.read()
        assert stale.status is SpeedStatus.STALE
        assert stale.usable is False

    def test_a_stale_encoder_stops_the_machine_routing(self):
        """Fail closed: firing on a speed nobody can confirm hits the next item."""
        now = [0.0]
        source = self.encoder(clock=lambda: now[0], timeout_s=2.0)
        source.update(0, at=0.0)
        source.update(10, at=1.0)
        now[0] = 10.0
        belt = Conveyor(ConveyorMode.ENCODER, source=source, geometry=geometry(), cfg=cfg())
        route = RoutingScheduler(cfg=cfg(), conveyor=belt).schedule("AUR-ITEM-1", "A", T0)
        assert route.status is RouteStatus.UNSCHEDULED
        assert route.reason_code is RouteReason.BELT_SPEED_UNMEASURED

    def test_the_health_snapshot_carries_the_geometry(self):
        source = self.encoder()
        source.update(0, at=0.0)
        source.update(10, at=1.0)
        snap = source.snapshot()
        assert snap["distance_per_pulse_cm"] == 1.0
        assert snap["healthy"] is True
        assert snap["last_count"] == 10


class TestTheEta:
    def belt(self, cm_s=10.0):
        return Conveyor(
            ConveyorMode.SIMULATION,
            source=SimulatedSpeed(cm_s),
            geometry=geometry(),
            cfg=cfg(),
        )

    def test_eta_is_distance_over_speed(self):
        assert self.belt(10.0).eta_seconds(60.0) == pytest.approx(6.0)

    def test_doubling_the_speed_halves_the_eta(self):
        assert self.belt(20.0).eta_seconds(60.0) == pytest.approx(3.0)

    def test_the_eta_to_each_actuator_is_reported(self):
        snap = self.belt(10.0).snapshot()
        assert snap["eta_to_servo_a_s"] == pytest.approx(6.0)
        assert snap["eta_to_servo_b_s"] == pytest.approx(9.0)
        assert snap["eta_to_load_cell_s"] == pytest.approx(2.5)

    def test_a_negative_distance_has_no_eta(self):
        assert self.belt().eta_seconds(-5.0) is None

    def test_an_unusable_speed_has_no_eta_rather_than_a_default(self):
        belt = Conveyor(ConveyorMode.MANUAL, source=ManualSpeed(None), cfg=cfg())
        assert belt.eta_seconds(60.0) is None


class TestTheEtaIsDynamic:
    """The whole point: a belt that changes speed changes the firing time."""

    def scheduler(self, source):
        belt = Conveyor(ConveyorMode.MANUAL, source=source, geometry=geometry(), cfg=cfg())
        return RoutingScheduler(cfg=cfg(), conveyor=belt), belt

    def test_slowing_the_belt_moves_the_next_item_s_firing_time_later(self):
        source = ManualSpeed(20.0)
        queue, _ = self.scheduler(source)
        fast = queue.schedule("AUR-ITEM-1", "A", T0)
        source.set(10.0)
        slow = queue.schedule("AUR-ITEM-2", "A", T0)
        assert slow.travel_time_s == pytest.approx(2 * fast.travel_time_s)
        assert slow.execute_at > fast.execute_at

    def test_the_speed_on_the_route_is_the_speed_that_was_used(self):
        source = ManualSpeed(20.0)
        queue, _ = self.scheduler(source)
        source.set(12.0)
        route = queue.schedule("AUR-ITEM-1", "A", T0)
        assert route.belt_speed_cm_s == 12.0

    def test_a_belt_that_stops_stops_scheduling(self):
        source = ManualSpeed(20.0)
        queue, _ = self.scheduler(source)
        queue.schedule("AUR-ITEM-1", "A", T0)
        source.set(None)
        refused = queue.schedule("AUR-ITEM-2", "A", T0)
        assert refused.status is RouteStatus.UNSCHEDULED

    def test_the_refusal_names_the_speed_source_s_own_reason(self):
        queue, _ = self.scheduler(ManualSpeed(None))
        route = queue.schedule("AUR-ITEM-1", "A", T0)
        assert "hand-measured belt speed" in route.reason

    def test_the_basis_string_names_where_the_speed_came_from(self):
        _, belt = self.scheduler(ManualSpeed(20.0))
        assert "MANUAL via manual" in belt.live_geometry().as_dict()["belt_speed_basis"]


class TestSchedulingOverTheMockBelt:
    """The mock belt supplies a speed to the EXISTING timing model. Nothing
    here sleeps and nothing here moves a servo."""

    def queue(self):
        environ = {"AURUM_CONVEYOR_MODE": "SIMULATION"}
        settings = cfg(**environ)
        return RoutingScheduler(cfg=settings, conveyor=Conveyor.from_config(settings))

    def test_an_item_gets_a_firing_time(self):
        route = self.queue().schedule("AUR-ITEM-1", "A", T0)
        assert route.status is RouteStatus.SCHEDULED
        # 60 cm at 10 cm/s = 6 s, less 150 ms of servo travel.
        assert route.travel_time_s == pytest.approx(6.0)
        assert route.execute_at == pytest.approx(T0 + 6.0 - 0.150)

    def test_bin_b_is_further_down_the_belt(self):
        route = self.queue().schedule("AUR-ITEM-1", "B", T0)
        assert route.travel_time_s == pytest.approx(9.0)

    def test_bin_c_still_schedules_nothing(self):
        route = self.queue().schedule("AUR-ITEM-1", "C", T0)
        assert route.status is RouteStatus.NO_ACTION
        assert route.servo is None

    def test_an_item_already_past_the_servo_is_refused(self):
        route = self.queue().schedule("AUR-ITEM-1", "A", T0, position_offset_cm=70.0)
        assert route.reason_code is RouteReason.INVALID_POSITION

    def test_an_item_part_way_down_the_belt_fires_sooner(self):
        route = self.queue().schedule("AUR-ITEM-1", "A", T0, position_offset_cm=20.0)
        assert route.distance_cm == pytest.approx(40.0)
        assert route.travel_time_s == pytest.approx(4.0)

    def test_every_route_over_the_mock_belt_is_stamped_simulated(self):
        assert self.queue().schedule("AUR-ITEM-1", "A", T0).simulated is True


class TestHardwareMode:
    def test_the_shipped_machine_is_physical(self):
        assert hardware_mode(cfg()) == "PHYSICAL"

    def test_the_simulation_switch_is_the_one_that_decides(self):
        assert hardware_mode(cfg(AURUM_SIMULATION="true")) == "SIMULATION"

    def test_a_mock_belt_does_not_by_itself_make_the_machine_simulated(self):
        """A demonstration belt on a real board is a legitimate configuration."""
        assert hardware_mode(cfg(AURUM_CONVEYOR_MODE="SIMULATION")) == "PHYSICAL"


class TestConfiguration:
    def test_an_unknown_mode_is_rejected_at_load_time(self):
        with pytest.raises(config.ConfigError) as exc:
            cfg(AURUM_CONVEYOR_MODE="FAST")
        assert "NONE" in str(exc.value)

    @pytest.mark.parametrize("mode", ["NONE", "SIMULATION", "ENCODER", "MANUAL"])
    def test_every_mode_builds_a_conveyor(self, mode):
        assert Conveyor.from_config(cfg(AURUM_CONVEYOR_MODE=mode)).mode == mode

    def test_encoder_geometry_is_configuration_not_an_assumed_model(self):
        environ = {
            "AURUM_CONVEYOR_MODE": "ENCODER",
            "AURUM_ENCODER_PULSES_PER_REV": "600",
            "AURUM_ENCODER_ROLLER_CIRCUMFERENCE_CM": "30",
        }
        belt = Conveyor.from_config(cfg(**environ))
        assert belt.source.distance_per_pulse_cm == pytest.approx(0.05)
