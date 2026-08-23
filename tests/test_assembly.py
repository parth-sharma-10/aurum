"""Grouping detected components into physical objects.

The question this module answers is the one the whole product model turns on:
given four boxes on a frame, is that a motherboard or is it four things lying
on a bench? Getting it wrong in one direction double-counts a mass; getting it
wrong in the other loses the hierarchy entirely.

**No test here describes a motherboard.** Each configuration is expressed only
as geometry - a large container box and some smaller boxes at coordinates -
because that is all the grouping code is allowed to know. A test that said
"a motherboard has one CPU" would be encoding exactly the assumption section 1
forbids.
"""

from __future__ import annotations

from app import config
from app.vision import assembly as assembly_module
from app.vision.tracker import ItemTracker, TrackedDetection

CFG = config.load()

#: A container box with room for plenty inside it.
BOARD = (0, 0, 400, 300)


def items(*detections: TrackedDetection):
    """Run detections through the real tracker so they carry real identities."""
    tracker = ItemTracker(cfg=CFG)
    for _ in range(CFG["tracking.min_detections_to_confirm"]):
        tracker.update(list(detections))
    return tracker.active


def det(track_id: int, cls: str, box, conf: float = 0.9) -> TrackedDetection:
    return TrackedDetection(track_id=track_id, class_name=cls, confidence=conf, xyxy=box)


def group(*detections: TrackedDetection):
    return assembly_module.group(items(*detections), cfg=CFG)


def only(*detections: TrackedDetection):
    result = group(*detections)
    assert len(result) == 1, [a.counts for a in result]
    return result[0]


class TestArbitraryConfigurations:
    """Section 2's three boards, and nothing in the code knows their names."""

    def test_a_board_with_two_modules_a_processor_and_connectors(self):
        board = only(
            det(1, "PCB", BOARD),
            det(2, "RAM", (20, 20, 60, 200)),
            det(3, "RAM", (70, 20, 110, 200)),
            det(4, "CPU", (180, 100, 250, 170)),
            det(5, "Connector", (300, 20, 330, 60)),
            det(6, "Connector", (300, 80, 330, 120)),
        )
        assert board.counts == {"PCB": 1, "RAM": 2, "CPU": 1, "Connector": 2}
        assert board.is_assembly

    def test_a_board_with_four_modules(self):
        board = only(
            det(1, "PCB", BOARD),
            det(2, "RAM", (20, 20, 55, 200)),
            det(3, "RAM", (60, 20, 95, 200)),
            det(4, "RAM", (100, 20, 135, 200)),
            det(5, "RAM", (140, 20, 175, 200)),
            det(6, "CPU", (250, 100, 320, 170)),
        )
        assert board.counts == {"PCB": 1, "RAM": 4, "CPU": 1}

    def test_a_board_with_no_modules_at_all(self):
        """Absence is not inferred: RAM is simply not in the inventory."""
        board = only(
            det(1, "PCB", BOARD),
            det(2, "CPU", (180, 100, 250, 170)),
            det(3, "Connector", (300, 20, 330, 60)),
        )
        assert board.counts == {"PCB": 1, "CPU": 1, "Connector": 1}
        assert "RAM" not in board.counts, "a component not seen must not be recorded as zero"

    def test_the_same_code_handles_all_three_without_any_rule_about_boards(self):
        """One grouping call, three layouts, one PCB each and nothing hardcoded."""
        layouts = [
            [det(1, "PCB", BOARD), det(2, "RAM", (20, 20, 60, 200))],
            [det(1, "PCB", BOARD), det(2, "CPU", (180, 100, 250, 170))],
            [det(1, "PCB", BOARD)],
        ]
        for layout in layouts:
            assembly = only(*layout)
            assert assembly.counts["PCB"] == 1
            assert assembly.class_name == "PCB"


class TestContainment:
    def test_a_component_outside_the_board_is_its_own_object(self):
        result = group(
            det(1, "PCB", BOARD),
            det(2, "CPU", (180, 100, 250, 170)),
            det(3, "RAM", (900, 900, 940, 1080)),
        )
        assert len(result) == 2
        inventories = sorted((a.counts for a in result), key=len, reverse=True)
        assert inventories[0] == {"PCB": 1, "CPU": 1}
        assert inventories[1] == {"RAM": 1}

    def test_components_merely_beside_a_board_are_not_absorbed(self):
        """Touching is not containing. A box mostly outside stays outside."""
        result = group(
            det(1, "PCB", BOARD),
            det(2, "CPU", (380, 100, 460, 170)),  # only a quarter of it overlaps
        )
        assert len(result) == 2

    def test_a_partially_occluded_component_still_belongs_to_its_board(self):
        """Most of the box is inside, which is what the ratio asks about."""
        board = only(
            det(1, "PCB", BOARD),
            det(2, "RAM", (350, 100, 410, 200)),  # 5/6 inside
        )
        assert board.counts == {"PCB": 1, "RAM": 1}

    def test_standalone_components_are_each_their_own_assembly(self):
        result = group(
            det(1, "CPU", (0, 0, 50, 50)),
            det(2, "RAM", (200, 200, 240, 380)),
            det(3, "Connector", (500, 500, 530, 540)),
        )
        assert len(result) == 3
        assert all(not a.is_assembly for a in result)
        assert all(len(a.counts) == 1 for a in result)

    def test_a_lone_component_keeps_its_own_item_id(self):
        """An assembly of one is not a new identity wrapped round an old one."""
        [item] = items(det(1, "CPU", (0, 0, 50, 50)))
        [assembly] = assembly_module.group([item], cfg=CFG)
        assert assembly.assembly_id == item.item_id
        assert assembly.root is item


class TestNesting:
    def test_a_daughterboard_and_its_chip_join_the_outermost_object(self):
        """A hand picks up the motherboard, so the motherboard is the object."""
        board = only(
            det(1, "PCB", BOARD),
            det(2, "PCB", (200, 150, 380, 290)),  # daughterboard, inside
            det(3, "CPU", (220, 180, 280, 240)),  # chip on the daughterboard
        )
        assert board.counts == {"PCB": 2, "CPU": 1}
        assert board.root.bbox == BOARD, "the outer board is the object, not the inner one"

    def test_a_component_attaches_to_the_tightest_container_that_holds_it(self):
        """Which still resolves to one object, but by the right path."""
        outer = det(1, "PCB", BOARD)
        inner = det(2, "PCB", (200, 150, 380, 290))
        chip = det(3, "CPU", (220, 180, 280, 240))
        [board] = group(outer, inner, chip)
        by_id = {m.track_id: m for m in board.members}
        assert set(by_id) == {1, 2, 3}
        assert board.assembly_id == by_id[1].item_id

    def test_identical_boxes_do_not_swallow_each_other(self):
        """A container must be strictly larger, so no cycle can form."""
        result = group(det(1, "PCB", BOARD), det(2, "PCB", BOARD))
        assert len(result) == 2


class TestIdentity:
    def test_one_assembly_has_one_id_across_every_component(self):
        board = only(
            det(1, "PCB", BOARD),
            det(2, "RAM", (20, 20, 60, 200)),
            det(3, "CPU", (180, 100, 250, 170)),
        )
        member_ids = board.as_dict()["member_ids"]
        assert board.item_id == board.assembly_id
        assert len(member_ids) == 3
        assert board.assembly_id in member_ids

    def test_the_id_is_stable_while_the_object_stays_in_view(self):
        detections = [det(1, "PCB", BOARD), det(2, "CPU", (180, 100, 250, 170))]
        tracker = ItemTracker(cfg=CFG)
        seen = set()
        for _ in range(8):
            tracker.update(detections)
            [assembly] = assembly_module.group(tracker.active, cfg=CFG)
            seen.add(assembly.assembly_id)
        assert len(seen) == 1, "the object's identity changed while it sat still"

    def test_the_weakest_member_sets_the_assembly_confidence(self):
        """A servo fires on the whole object, not on its best-seen part."""
        board = only(
            det(1, "PCB", BOARD, conf=0.95),
            det(2, "RAM", (20, 20, 60, 200), conf=0.51),
            det(3, "CPU", (180, 100, 250, 170), conf=0.92),
        )
        assert board.confidence == 0.51

    def test_one_mass_is_recorded_once_and_reachable_from_either_side(self):
        board = only(det(1, "PCB", BOARD), det(2, "CPU", (180, 100, 250, 170)))
        board.attach_weight(842.3, "MEASURED")
        assert board.weight_g == 842.3
        assert board.root.weight_g == 842.3


class TestContainedFraction:
    def test_it_measures_the_inner_box_not_the_overlap_of_both(self):
        small, large = (0, 0, 10, 10), (0, 0, 100, 100)
        assert assembly_module.contained_fraction(small, large) == 1.0
        assert assembly_module.contained_fraction(large, small) == 0.01

    def test_no_overlap_is_zero_and_a_degenerate_box_does_not_divide_by_zero(self):
        assert assembly_module.contained_fraction((0, 0, 10, 10), (50, 50, 60, 60)) == 0.0
        assert assembly_module.contained_fraction((5, 5, 5, 5), (0, 0, 10, 10)) == 0.0
        assert assembly_module.contained_fraction(None, (0, 0, 10, 10)) == 0.0
