"""Tests for conveyor geometry and the routing scheduler.

Three properties run through this file.

**A decision is not a route.** An unmeasured belt makes an item unroutable
while leaving its decision exactly as it was. Nothing here rewrites an A into
a C to express "I could not schedule it".

**Nothing actuates.** A route that becomes DUE means the moment arrived, not
that a paddle moved. No serial port is opened anywhere in this phase.

**Simulated geometry cannot become production geometry.** The TEST profile is
reachable only with simulation switched on, and every result computed from it
is stamped SIMULATED.

All distances and speeds here are TEST fixture values. None of them was
measured on a conveyor, because the conveyor does not exist yet.
"""

from __future__ import annotations

import math

import pytest

from app import config
from app.routing import Geometry, RouteReason, RouteStatus, RoutingMode, RoutingScheduler

# TEST geometry. Chosen so the arithmetic is exact by hand:
# 60 cm at 20 cm/s = 3.000 s travel; less 150 ms of paddle = 2.850 s.
TEST_SPEED = 20.0
TEST_A_CM = 60.0
TEST_B_CM = 90.0
TEST_DELAY_MS = 150.0
T0 = 10.0


def geometry(**overrides) -> Geometry:
    base = {
        "mode": RoutingMode.SIMULATED,
        "belt_speed_cm_s": TEST_SPEED,
        "camera_to_load_cell_cm": 25.0,
        "camera_to_servo_a_cm": TEST_A_CM,
        "camera_to_servo_b_cm": TEST_B_CM,
        "servo_actuation_delay_ms": TEST_DELAY_MS,
        "timing_offset_ms": 0.0,
    }
    return Geometry(**{**base, **overrides})


def scheduler(geo: Geometry | None = None, lifecycle=None) -> RoutingScheduler:
    return RoutingScheduler(geometry=geo or geometry(), lifecycle=lifecycle)


class Lifecycle:
    """Stands in for the item tracker's identity lookup."""

    def __init__(self, known):
        self._known = set(known)

    def get(self, item_id):
        return object() if item_id in self._known else None


class TestBasicRouting:
    def test_a_reaches_servo_a(self):
        route = scheduler().schedule("AUR-ITEM-0017", "A", T0)
        assert route.status is RouteStatus.SCHEDULED
        assert route.reason_code is RouteReason.ROUTE_A
        assert route.target == "A"
        assert route.servo == "SERVO_A"

    def test_b_reaches_servo_b(self):
        route = scheduler().schedule("AUR-ITEM-0018", "B", T0)
        assert route.reason_code is RouteReason.ROUTE_B
        assert route.servo == "SERVO_B"

    def test_c_produces_no_actuator_action(self):
        route = scheduler().schedule("AUR-ITEM-0019", "C", T0)
        assert route.status is RouteStatus.NO_ACTION
        assert route.reason_code is RouteReason.NO_ROUTE_C
        assert route.servo is None
        assert route.execute_at is None

    def test_there_is_no_servo_c(self):
        from app.routing import SERVO_FOR_TARGET

        assert "C" not in SERVO_FOR_TARGET

    def test_c_is_never_converted_into_a_or_b(self):
        route = scheduler().schedule("AUR-ITEM-0019", "C", T0)
        assert route.target == "C"
        assert route.decision == "C"

    def test_a_decision_object_is_accepted_as_well_as_a_letter(self):
        """The engine hands over a Decision; the scheduler reads either."""
        from app.decision import Bin, Decision, ReasonCode

        decision = Decision(Bin.A, ReasonCode.A_PREFERRED_CLASS, "because", {}, {})
        assert scheduler().schedule("AUR-ITEM-1", decision, T0).target == "A"


class TestTimingModel:
    def test_travel_time_is_distance_over_speed(self):
        assert geometry().travel_time_s(60.0) == pytest.approx(3.0)

    def test_execute_at_subtracts_the_actuation_delay(self):
        """The command must leave before arrival: the paddle takes time."""
        route = scheduler().schedule("AUR-ITEM-1", "A", T0)
        assert route.travel_time_s == pytest.approx(3.0)
        assert route.execute_at == pytest.approx(T0 + 3.0 - 0.150)

    def test_a_negative_offset_fires_earlier(self):
        route = scheduler(geometry(timing_offset_ms=-200.0)).schedule("AUR-ITEM-1", "A", T0)
        assert route.execute_at == pytest.approx(T0 + 3.0 - 0.150 - 0.200)

    def test_a_positive_offset_fires_later(self):
        route = scheduler(geometry(timing_offset_ms=200.0)).schedule("AUR-ITEM-1", "A", T0)
        assert route.execute_at == pytest.approx(T0 + 3.0 - 0.150 + 0.200)

    def test_the_further_servo_fires_later(self):
        queue = scheduler()
        a = queue.schedule("AUR-ITEM-1", "A", T0)
        b = queue.schedule("AUR-ITEM-2", "B", T0)
        assert b.execute_at > a.execute_at
        assert b.execute_at - a.execute_at == pytest.approx((TEST_B_CM - TEST_A_CM) / TEST_SPEED)

    def test_a_faster_belt_fires_sooner(self):
        slow = scheduler(geometry(belt_speed_cm_s=10.0)).schedule("AUR-ITEM-1", "A", T0)
        fast = scheduler(geometry(belt_speed_cm_s=40.0)).schedule("AUR-ITEM-2", "A", T0)
        assert fast.execute_at < slow.execute_at

    def test_every_term_is_reported_not_folded_into_a_constant(self):
        route = scheduler().schedule("AUR-ITEM-1", "A", T0).as_dict(now=T0)
        geo = route["geometry"]
        assert geo["distance_cm"] == TEST_A_CM
        assert geo["belt_speed_cm_s"] == TEST_SPEED
        assert geo["travel_time_s"] == pytest.approx(3.0)
        assert geo["actuation_delay_ms"] == TEST_DELAY_MS
        assert geo["timing_offset_ms"] == 0.0
        assert "distance_cm/belt_speed_cm_s" in route["formula"]


class TestUnmeasuredGeometry:
    def test_an_unmeasured_belt_blocks_routing(self):
        route = scheduler(geometry(belt_speed_cm_s=None)).schedule("AUR-ITEM-1", "A", T0)
        assert route.status is RouteStatus.UNSCHEDULED
        assert route.reason_code is RouteReason.BELT_SPEED_UNMEASURED

    def test_an_unmeasured_servo_a_distance_blocks_an_a_route(self):
        route = scheduler(geometry(camera_to_servo_a_cm=None)).schedule("AUR-ITEM-1", "A", T0)
        assert route.reason_code is RouteReason.SERVO_GEOMETRY_UNMEASURED

    def test_an_unmeasured_servo_b_distance_blocks_a_b_route(self):
        route = scheduler(geometry(camera_to_servo_b_cm=None)).schedule("AUR-ITEM-1", "B", T0)
        assert route.reason_code is RouteReason.SERVO_GEOMETRY_UNMEASURED

    def test_an_unmeasured_servo_b_does_not_block_an_a_route(self):
        """Only the geometry a route actually needs is required."""
        route = scheduler(geometry(camera_to_servo_b_cm=None)).schedule("AUR-ITEM-1", "A", T0)
        assert route.status is RouteStatus.SCHEDULED

    def test_an_unmeasured_actuation_delay_blocks_routing(self):
        route = scheduler(geometry(servo_actuation_delay_ms=None)).schedule("AUR-ITEM-1", "A", T0)
        assert route.reason_code is RouteReason.ACTUATION_DELAY_UNMEASURED

    def test_the_decision_survives_an_unroutable_machine(self):
        """Unroutable is not a demotion to C. The two are separate."""
        route = scheduler(geometry(belt_speed_cm_s=None)).schedule("AUR-ITEM-1", "A", T0)
        assert route.decision == "A"
        assert route.target == "A"
        assert route.status is RouteStatus.UNSCHEDULED

    def test_c_still_resolves_on_an_unmeasured_machine(self):
        """C needs no geometry: nobody has to do anything."""
        route = scheduler(geometry(belt_speed_cm_s=None)).schedule("AUR-ITEM-1", "C", T0)
        assert route.status is RouteStatus.NO_ACTION

    def test_the_refusal_says_what_to_measure(self):
        route = scheduler(geometry(belt_speed_cm_s=None)).schedule("AUR-ITEM-1", "A", T0)
        assert "conveyor.belt.speed_cm_s" in route.reason


class TestInvalidValues:
    @pytest.mark.parametrize(
        ("speed", "expected"),
        [(0.0, "zero"), (-5.0, "negative"), (None, "UNMEASURED")],
    )
    def test_an_unusable_belt_speed_is_named(self, speed, expected):
        assert expected in geometry(belt_speed_cm_s=speed).belt_speed_problem()

    def test_a_zero_belt_speed_never_divides(self):
        route = scheduler(geometry(belt_speed_cm_s=0.0)).schedule("AUR-ITEM-1", "A", T0)
        assert route.status is RouteStatus.UNSCHEDULED

    @pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf, "fast", True, None])
    def test_a_non_finite_belt_speed_is_rejected_by_the_config_reader(self, value):
        from app.routing.geometry import _finite

        assert _finite(value) is None

    def test_a_negative_distance_is_refused(self):
        route = scheduler(geometry(camera_to_servo_a_cm=-10.0)).schedule("AUR-ITEM-1", "A", T0)
        assert route.reason_code is RouteReason.SERVO_GEOMETRY_UNMEASURED
        assert "negative" in route.reason

    @pytest.mark.parametrize("bad", [math.nan, math.inf, "here", True])
    def test_an_invalid_position_offset_is_refused(self, bad):
        route = scheduler().schedule("AUR-ITEM-1", "A", T0, position_offset_cm=bad)
        assert route.reason_code is RouteReason.INVALID_POSITION

    def test_an_item_already_past_the_servo_is_refused(self):
        route = scheduler().schedule("AUR-ITEM-1", "A", T0, position_offset_cm=TEST_A_CM + 5)
        assert route.reason_code is RouteReason.INVALID_POSITION
        assert "already past" in route.reason

    def test_a_valid_position_offset_shortens_the_distance(self):
        route = scheduler().schedule("AUR-ITEM-1", "A", T0, position_offset_cm=20.0)
        assert route.distance_cm == pytest.approx(40.0)
        assert route.travel_time_s == pytest.approx(2.0)

    @pytest.mark.parametrize("bad", [math.nan, math.inf, "soon", None, True])
    def test_an_unusable_detection_time_is_refused(self, bad):
        route = scheduler().schedule("AUR-ITEM-1", "A", bad)
        assert route.reason_code is RouteReason.TIMING_UNAVAILABLE


class TestInvalidDecisions:
    @pytest.mark.parametrize("bad", [None, "", "D", "SERVO_A", "a", 1, object()])
    def test_an_unroutable_decision_is_refused(self, bad):
        route = scheduler().schedule("AUR-ITEM-1", bad, T0)
        assert route.status is RouteStatus.UNSCHEDULED
        assert route.reason_code is RouteReason.INVALID_DECISION

    def test_a_bad_decision_never_becomes_a_servo_command(self):
        route = scheduler().schedule("AUR-ITEM-1", "D", T0)
        assert route.servo is None
        assert route.execute_at is None


class TestTimingSafety:
    def test_a_route_in_the_past_is_refused(self):
        route = scheduler().schedule("AUR-ITEM-1", "A", T0, now=T0 + 60)
        assert route.status is RouteStatus.UNSCHEDULED
        assert route.reason_code is RouteReason.TIMING_EXPIRED

    def test_the_refusal_explains_why_catching_up_is_unsafe(self):
        route = scheduler().schedule("AUR-ITEM-1", "A", T0, now=T0 + 60)
        assert "behind it" in route.reason

    def test_expired_routes_are_not_queued(self):
        queue = scheduler()
        queue.schedule("AUR-ITEM-1", "A", T0, now=T0 + 60)
        assert queue.pending() == []

    def test_exactly_at_the_firing_time_is_still_schedulable(self):
        execute_at = T0 + 3.0 - 0.150
        route = scheduler().schedule("AUR-ITEM-1", "A", T0, now=execute_at)
        assert route.status is RouteStatus.SCHEDULED

    def test_just_before_the_firing_time_is_scheduled_not_due(self):
        route = scheduler().schedule("AUR-ITEM-1", "A", T0)
        assert route.status_at(route.execute_at - 1e-6) is RouteStatus.SCHEDULED

    def test_exactly_at_the_firing_time_is_due(self):
        route = scheduler().schedule("AUR-ITEM-1", "A", T0)
        assert route.status_at(route.execute_at) is RouteStatus.DUE

    def test_just_after_the_firing_time_is_due(self):
        route = scheduler().schedule("AUR-ITEM-1", "A", T0)
        assert route.status_at(route.execute_at + 1e-6) is RouteStatus.DUE

    def test_becoming_due_is_not_becoming_executed(self):
        """DUE means the moment arrived. Nothing has moved."""
        route = scheduler().schedule("AUR-ITEM-1", "A", T0)
        assert route.status_at(route.execute_at + 10) is not RouteStatus.EXECUTED


class TestIdentityAndDuplicates:
    def test_one_item_gets_one_route(self):
        queue = scheduler()
        queue.schedule("AUR-ITEM-1", "A", T0)
        second = queue.schedule("AUR-ITEM-1", "A", T0)
        assert second.reason_code is RouteReason.ALREADY_ROUTED
        assert len(queue.pending()) == 1

    def test_a_repeat_cannot_change_the_target(self):
        queue = scheduler()
        queue.schedule("AUR-ITEM-1", "A", T0)
        assert queue.schedule("AUR-ITEM-1", "B", T0).status is RouteStatus.UNSCHEDULED
        assert queue.get("AUR-ITEM-1").target == "A"

    def test_a_repeat_of_a_c_item_is_also_refused(self):
        queue = scheduler()
        queue.schedule("AUR-ITEM-1", "C", T0)
        assert queue.schedule("AUR-ITEM-1", "C", T0).reason_code is RouteReason.ALREADY_ROUTED

    def test_different_items_route_independently(self):
        queue = scheduler()
        first = queue.schedule("AUR-ITEM-1", "A", T0)
        second = queue.schedule("AUR-ITEM-2", "A", T0 + 2.0)
        assert first.execute_at != second.execute_at
        assert len(queue.pending()) == 2

    def test_the_aurum_identity_is_the_routing_key(self):
        route = scheduler().schedule("AUR-ITEM-00017", "A", T0)
        assert route.item_id == "AUR-ITEM-00017"

    def test_an_item_with_no_identity_is_refused(self):
        assert scheduler().schedule("", "A", T0).reason_code is RouteReason.STALE_ITEM


class TestStaleItems:
    def test_an_item_the_lifecycle_knows_is_routable(self):
        queue = scheduler(lifecycle=Lifecycle({"AUR-ITEM-1"}))
        assert queue.schedule("AUR-ITEM-1", "A", T0).status is RouteStatus.SCHEDULED

    def test_an_item_the_lifecycle_does_not_know_is_stale(self):
        queue = scheduler(lifecycle=Lifecycle({"AUR-ITEM-1"}))
        route = queue.schedule("AUR-ITEM-9", "A", T0)
        assert route.reason_code is RouteReason.STALE_ITEM

    def test_a_stale_item_creates_no_action(self):
        queue = scheduler(lifecycle=Lifecycle(set()))
        queue.schedule("AUR-ITEM-9", "A", T0)
        assert queue.pending() == []


class TestQueue:
    def test_pending_is_ordered_by_firing_time(self):
        queue = scheduler()
        queue.schedule("AUR-ITEM-3", "B", T0 + 5)
        queue.schedule("AUR-ITEM-1", "A", T0)
        queue.schedule("AUR-ITEM-2", "A", T0 + 1)
        assert [r.item_id for r in queue.pending()] == [
            "AUR-ITEM-1",
            "AUR-ITEM-2",
            "AUR-ITEM-3",
        ]

    def test_due_returns_only_arrived_routes(self):
        queue = scheduler()
        queue.schedule("AUR-ITEM-1", "A", T0)
        queue.schedule("AUR-ITEM-2", "A", T0 + 10)
        assert [r.item_id for r in queue.due(T0 + 3)] == ["AUR-ITEM-1"]

    def test_c_items_are_not_in_the_pending_queue(self):
        queue = scheduler()
        queue.schedule("AUR-ITEM-1", "C", T0)
        assert queue.pending() == []
        assert queue.get("AUR-ITEM-1").status is RouteStatus.NO_ACTION

    def test_marking_executed_removes_it_from_pending(self):
        queue = scheduler()
        queue.schedule("AUR-ITEM-1", "A", T0)
        queue.mark_executed("AUR-ITEM-1", at=T0 + 3)
        assert queue.pending() == []
        assert queue.get("AUR-ITEM-1").status is RouteStatus.EXECUTED
        assert queue.get("AUR-ITEM-1").executed_at == T0 + 3

    def test_marking_executed_twice_is_harmless(self):
        queue = scheduler()
        queue.schedule("AUR-ITEM-1", "A", T0)
        queue.mark_executed("AUR-ITEM-1", at=T0 + 3)
        queue.mark_executed("AUR-ITEM-1", at=T0 + 9)
        assert queue.get("AUR-ITEM-1").executed_at == T0 + 3

    def test_marking_an_unknown_item_returns_none(self):
        assert scheduler().mark_executed("AUR-ITEM-NOPE") is None

    def test_refusals_are_kept_for_inspection(self):
        queue = scheduler(geometry(belt_speed_cm_s=None))
        queue.schedule("AUR-ITEM-1", "A", T0)
        assert len(queue.rejected()) == 1


class TestMultipleItems:
    def test_four_items_keep_independent_schedules(self):
        queue = scheduler()
        queue.schedule("AUR-ITEM-17", "A", T0)
        queue.schedule("AUR-ITEM-18", "B", T0)
        queue.schedule("AUR-ITEM-19", "C", T0)
        queue.schedule("AUR-ITEM-20", "A", T0 + 2.0)

        times = {r.item_id: r.execute_at for r in queue.pending()}
        assert len(times) == 3
        assert len(set(times.values())) == 3
        assert queue.get("AUR-ITEM-19").status is RouteStatus.NO_ACTION

    def test_one_schedule_never_overwrites_another(self):
        queue = scheduler()
        first = queue.schedule("AUR-ITEM-17", "A", T0).execute_at
        queue.schedule("AUR-ITEM-18", "A", T0 + 5)
        assert queue.get("AUR-ITEM-17").execute_at == first

    def test_they_become_due_in_order(self):
        queue = scheduler()
        queue.schedule("AUR-ITEM-17", "A", T0)
        queue.schedule("AUR-ITEM-18", "B", T0)
        assert [r.item_id for r in queue.due(T0 + 3.0)] == ["AUR-ITEM-17"]
        assert len(queue.due(T0 + 5.0)) == 2


class TestSimulationIsolation:
    def test_production_geometry_is_unmeasured(self):
        """Guard: shipping TEST distances as real geometry would route by luck."""
        geo = Geometry.from_config(config.load(environ={}))
        assert geo.mode is RoutingMode.REAL
        assert geo.belt_speed_cm_s is None
        assert geo.camera_to_servo_a_cm is None
        assert geo.camera_to_servo_b_cm is None
        assert geo.servo_actuation_delay_ms is None

    def test_a_production_machine_refuses_to_route(self):
        queue = RoutingScheduler(cfg=config.load(environ={}))
        assert queue.schedule("AUR-ITEM-1", "A", T0).status is RouteStatus.UNSCHEDULED

    def test_the_test_profile_is_reachable_only_in_simulation(self):
        geo = Geometry.from_config(config.load(environ={"AURUM_SIMULATION": "true"}))
        assert geo.mode is RoutingMode.SIMULATED
        # 10 cm/s = 0.10 m/s, the demonstration belt speed. A TEST value.
        assert geo.belt_speed_cm_s == 10.0

    def test_every_simulated_route_is_stamped(self):
        cfg = config.load(environ={"AURUM_SIMULATION": "true"})
        route = RoutingScheduler(cfg=cfg).schedule("AUR-ITEM-1", "A", T0)
        assert route.simulated is True
        assert route.as_dict(now=T0)["mode"] == "SIMULATED"

    def test_a_simulated_route_never_claims_measured_geometry(self):
        cfg = config.load(environ={"AURUM_SIMULATION": "true"})
        snapshot = RoutingScheduler(cfg=cfg).snapshot(now=T0)
        assert snapshot["simulated"] is True
        assert "engineering approximation" in snapshot["geometry"]["belt_speed_basis"]

    def test_the_belt_speed_is_never_described_as_measured(self):
        """A geometry with no speed source attached says so.

        Re-pointed 2026-08-26: "this machine has no encoder" became a claim
        about the machine that app/routing/conveyor.py can now falsify. The
        property protected is unchanged - a configured constant must never
        read as a measurement - and the basis string now names its source
        instead of asserting a fact about the hardware.
        """
        blob = str(geometry().as_dict()).lower()
        assert "no speed source is attached" in blob
        assert "configured constant" in blob


class TestAuditability:
    def test_a_route_answers_every_question_about_itself(self):
        queue = scheduler()
        record = queue.schedule("AUR-ITEM-17", "A", T0, component_class="CPU").as_dict(now=T0)
        for field in (
            "item_id",
            "component_class",
            "decision",
            "target",
            "servo",
            "status",
            "reason_code",
            "reason",
            "mode",
            "simulated",
            "detected_at",
            "execute_at",
            "seconds_remaining",
            "geometry",
            "formula",
        ):
            assert field in record, field

    def test_the_countdown_is_available_for_a_display(self):
        route = scheduler().schedule("AUR-ITEM-17", "A", T0)
        assert route.seconds_remaining(T0) == pytest.approx(2.850)
        assert route.as_dict(now=T0 + 1.0)["seconds_remaining"] == pytest.approx(1.850)

    def test_a_refusal_is_as_explainable_as_a_success(self):
        route = scheduler(geometry(belt_speed_cm_s=None)).schedule("AUR-ITEM-1", "A", T0)
        record = route.as_dict(now=T0)
        assert record["status"] == "UNSCHEDULED"
        assert record["reason_code"] == "BELT_SPEED_UNMEASURED"
        assert record["reason"]

    def test_the_snapshot_reports_whether_the_machine_can_route_at_all(self):
        assert scheduler().snapshot(now=T0)["routable"] is True
        assert scheduler(geometry(belt_speed_cm_s=None)).snapshot(now=T0)["routable"] is False


class TestNoActuation:
    def test_nothing_in_the_routing_layer_mentions_serial_or_a_port(self):
        """Phase 6 must not reach hardware. Phase 7 owns that."""
        from pathlib import Path

        import app.routing as routing_pkg

        source = ""
        for path in Path(routing_pkg.__file__).parent.glob("*.py"):
            source += path.read_text().lower()
        for forbidden in ("import serial", "pyserial", "write(", "baudrate"):
            assert forbidden not in source, forbidden
