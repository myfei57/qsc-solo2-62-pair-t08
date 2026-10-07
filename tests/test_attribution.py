"""Behavioural tests for the versioned attribution registry."""

from __future__ import annotations

import json
import tempfile
import unittest

from coldstore.attribution import (
    MODE_IMMEDIATE,
    MODE_NEXT_SHIFT,
    SHIFT_SECONDS,
    STATUS_ACTIVE,
    STATUS_QUARANTINED,
    STATUS_SCHEDULED,
    STATUS_SUPERSEDED,
    AttributionRegistry,
    shift_boundary_after,
)
from coldstore.store import Store

BASE = 10 * SHIFT_SECONDS  # a round shift boundary offset


class FakeClock:
    """Controllable epoch source."""

    def __init__(self, value: int = BASE) -> None:
        self.value = value

    def __call__(self) -> int:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += seconds


class AttributionCase(unittest.TestCase):
    def make_registry(self) -> tuple[AttributionRegistry, FakeClock]:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        clock = FakeClock()
        store = Store.open(f"{self._tmp.name}/state.json")
        return AttributionRegistry(store, clock=clock), clock

    def test_first_claim_is_active_immediately(self) -> None:
        registry, _ = self.make_registry()
        record = registry.register("comp", "C-1", 3, "zhang")
        self.assertEqual(record.status, STATUS_ACTIVE)
        self.assertEqual(record.supersedes, 0)
        view = registry.attribution_of("comp", "C-1")
        self.assertIsNotNone(view)
        assert view is not None
        self.assertEqual(view.zone, 3)
        self.assertEqual(view.version, 1)

    def test_change_takes_effect_at_next_shift_not_now(self) -> None:
        registry, clock = self.make_registry()
        registry.register("comp", "C-1", 3, "zhang")
        boundary = shift_boundary_after(clock.value)

        record = registry.register("comp", "C-1", 5, "li", confirm_handover=True)
        self.assertEqual(record.status, STATUS_SCHEDULED)
        self.assertEqual(record.effective_at, boundary)

        # Old attribution remains authoritative before the boundary.
        current = registry.point_in_time(clock.value)
        self.assertEqual(current.active[0].zone, 3)
        self.assertEqual(len(current.pending), 1)

        # After rollover the new attribution is live and the old one still
        # answers historical queries.
        clock.advance(SHIFT_SECONDS)
        self.assertEqual(len(registry.activate_due()), 1)
        live = registry.point_in_time(clock.value)
        self.assertEqual(live.active[0].zone, 5)
        historical = registry.point_in_time(BASE + 1)
        self.assertEqual(historical.active[0].zone, 3)
        self.assertEqual(historical.shift, (BASE + 1) // SHIFT_SECONDS)

    def test_point_in_time_rolls_over_due_cutover_automatically(self) -> None:
        registry, clock = self.make_registry()
        registry.register("fan", "F-2", 1, "zhang")
        registry.register("fan", "F-2", 2, "li", confirm_handover=True)
        clock.advance(SHIFT_SECONDS)
        live = registry.point_in_time()
        self.assertEqual(live.active[0].zone, 2)
        self.assertEqual(live.pending, [])

    def test_unconfirmed_competing_claim_is_quarantined_keeps_earliest(self) -> None:
        registry, clock = self.make_registry()
        registry.register("ammonia", "A-9", 2, "zhang")
        later = registry.register("ammonia", "A-9", 4, "li")
        self.assertEqual(later.status, STATUS_QUARANTINED)

        # The earlier registered attribution stays authoritative.
        view = registry.attribution_of("ammonia", "A-9", clock.value)
        assert view is not None
        self.assertEqual(view.zone, 2)
        picture = registry.point_in_time(clock.value)
        self.assertEqual(len(picture.quarantined), 1)
        self.assertEqual(picture.quarantined[0].zone, 4)

        # Confirming the handover schedules it for the next shift.
        confirmed = registry.confirm_handover(later.version, "wang")
        self.assertEqual(confirmed.status, STATUS_SCHEDULED)
        clock.advance(SHIFT_SECONDS)
        registry.activate_due()
        self.assertEqual(registry.attribution_of("ammonia", "A-9").zone, 4)

        # The old version is never deleted.
        chain = registry.versions_for("ammonia", "A-9")
        self.assertEqual([item.status for item in chain], [STATUS_SUPERSEDED, STATUS_ACTIVE])

    def test_immediate_change_requires_distinct_confirmer(self) -> None:
        registry, clock = self.make_registry()
        registry.register("comp", "C-7", 1, "zhang")
        with self.assertRaises(ValueError):
            registry.register(
                "comp", "C-7", 2, "zhang",
                mode=MODE_IMMEDIATE, confirmer="zhang", confirm_handover=True,
            )
        with self.assertRaises(ValueError):
            registry.register(
                "comp", "C-7", 2, "zhang",
                mode=MODE_IMMEDIATE, confirmer="wang",
            )
        record = registry.register(
            "comp", "C-7", 2, "zhang",
            mode=MODE_IMMEDIATE, confirmer="wang", confirm_handover=True,
        )
        self.assertEqual(record.status, STATUS_ACTIVE)
        self.assertEqual(record.effective_at, clock.value)
        self.assertEqual(registry.attribution_of("comp", "C-7").zone, 2)

    def test_cross_period_coexistence_old_and_new_both_queryable(self) -> None:
        registry, clock = self.make_registry()
        registry.register("comp", "C-1", 3, "zhang")
        registry.register("fan", "F-1", 3, "zhang")
        registry.register("comp", "C-1", 5, "li", confirm_handover=True)
        registry.register("fan", "F-1", 5, "li", confirm_handover=True)

        old_time = clock.value
        clock.advance(SHIFT_SECONDS)
        registry.activate_due()
        new_time = clock.value

        check = registry.align(old_time, new_time)
        self.assertTrue(check.aligned)
        self.assertEqual(len(check.moved), 2)
        self.assertEqual(
            {(row["kind"], row["from_zone"], row["to_zone"]) for row in check.moved},
            {("comp", 3, 5), ("fan", 3, 5)},
        )

    def test_alignment_flags_missing_units(self) -> None:
        registry, clock = self.make_registry()
        registry.register("comp", "C-1", 3, "zhang")
        first = clock.value
        clock.advance(SHIFT_SECONDS)
        # A brand new unit appearing only on the new side breaks alignment.
        registry.register("ammonia", "A-1", 9, "li", mode=MODE_IMMEDIATE, confirmer="wang",
                          confirm_handover=True)
        check = registry.align(first, clock.value)
        self.assertFalse(check.aligned)
        self.assertEqual(len(check.added), 1)

    def test_repeated_queries_have_stable_signature(self) -> None:
        registry, clock = self.make_registry()
        registry.register("comp", "C-1", 3, "zhang")
        first = registry.point_in_time(clock.value)
        again = registry.point_in_time(clock.value)
        self.assertEqual(first.signature, again.signature)

    def test_ledger_records_lifecycle_and_chain_verifies(self) -> None:
        registry, clock = self.make_registry()
        registry.register("comp", "C-1", 3, "zhang")
        registry.register("comp", "C-1", 5, "li", confirm_handover=True)
        registry.point_in_time(clock.value)
        clock.advance(SHIFT_SECONDS)
        registry.activate_due()

        kinds = [entry.kind for entry in registry.ledger()]
        self.assertEqual(kinds, ["register", "register", "query", "activate"])
        self.assertTrue(registry.verify_ledger())

    def test_ledger_detects_tampering(self) -> None:
        registry, _ = self.make_registry()
        registry.register("comp", "C-1", 3, "zhang")
        entries = registry.ledger()
        raw = json.loads(registry._store.get("attribution:ledger")[0])
        payload = json.loads(raw[0])
        payload["detail"] = "tampered"
        raw[0] = json.dumps(payload, ensure_ascii=False)
        registry._store.put("attribution:ledger", json.dumps(raw, ensure_ascii=False))
        self.assertFalse(registry.verify_ledger())
        self.assertEqual(len(entries), 1)

    def test_equipment_in_zone_matches_lookup(self) -> None:
        registry, _ = self.make_registry()
        registry.register("comp", "C-1", 3, "zhang")
        registry.register("fan", "F-1", 3, "zhang")
        registry.register("comp", "C-2", 4, "li")
        in_zone = registry.equipment_in_zone(3)
        self.assertEqual({(item.kind, item.equipment_id) for item in in_zone},
                         {("comp", "C-1"), ("fan", "F-1")})

    def test_validation_rejects_bad_inputs(self) -> None:
        registry, _ = self.make_registry()
        with self.assertRaises(ValueError):
            registry.register("pump", "P-1", 1, "zhang")
        with self.assertRaises(ValueError):
            registry.register("comp", "C-1", 0, "zhang")
        with self.assertRaises(ValueError):
            registry.register("comp", "C-1", 1, "")
        with self.assertRaises(ValueError):
            registry.register("comp", "C-1", 1, "zhang", mode="whenever")

    def test_second_scheduled_change_is_rejected_until_pending_resolves(self) -> None:
        registry, clock = self.make_registry()
        registry.register("comp", "C-1", 1, "zhang")
        registry.register("comp", "C-1", 2, "li", confirm_handover=True)
        with self.assertRaises(ValueError):
            registry.register("comp", "C-1", 3, "wang", confirm_handover=True)
        clock.advance(SHIFT_SECONDS)
        registry.activate_due()
        record = registry.register("comp", "C-1", 3, "wang", confirm_handover=True)
        self.assertEqual(record.status, STATUS_SCHEDULED)

    def test_immediate_change_cancels_pending_and_restores_predecessor(self) -> None:
        registry, clock = self.make_registry()
        registry.register("comp", "C-1", 1, "zhang")
        pending = registry.register("comp", "C-1", 2, "li", confirm_handover=True)
        record = registry.register(
            "comp", "C-1", 3, "zhao",
            mode=MODE_IMMEDIATE, confirmer="qian", confirm_handover=True,
        )
        self.assertEqual(record.status, STATUS_ACTIVE)
        chain = registry.versions_for("comp", "C-1")
        statuses = {item.version: item.status for item in chain}
        self.assertEqual(statuses[pending.version], "cancelled")
        self.assertEqual(registry.attribution_of("comp", "C-1").zone, 3)
        # Exactly one active row, no overlap.
        live = registry.point_in_time(clock.value)
        self.assertEqual(len(live.active), 1)
        self.assertIn("cancel", [entry.kind for entry in registry.ledger()])

    def test_quarantined_confirmation_repoints_at_latest_authority(self) -> None:
        registry, clock = self.make_registry()
        registry.register("comp", "C-1", 1, "zhang")
        stale = registry.register("comp", "C-1", 2, "li")
        self.assertEqual(stale.status, STATUS_QUARANTINED)
        # Meanwhile a direct handover to zone 3 happens immediately.
        direct = registry.register(
            "comp", "C-1", 3, "zhao",
            mode=MODE_IMMEDIATE, confirmer="qian", confirm_handover=True,
        )
        # The stale claim now takes over from the latest authority, not v1.
        confirmed = registry.confirm_handover(stale.version, "sun")
        self.assertEqual(confirmed.supersedes, direct.version)
        clock.advance(SHIFT_SECONDS)
        registry.activate_due()
        self.assertEqual(registry.attribution_of("comp", "C-1").zone, 2)
        self.assertEqual(
            registry.attribution_of("comp", "C-1", clock.value - SHIFT_SECONDS).zone, 3
        )

    def test_identical_reregistration_is_rejected(self) -> None:
        registry, _ = self.make_registry()
        registry.register("comp", "C-1", 3, "zhang")
        with self.assertRaises(ValueError):
            registry.register("comp", "C-1", 3, "li")


if __name__ == "__main__":
    unittest.main()
