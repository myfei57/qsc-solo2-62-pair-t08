"""Behavioural tests for the versioned ownership registry."""

from __future__ import annotations

import tempfile
import unittest

from coldstore.ownership import (
    ABORTED,
    ACTIVE,
    AMMONIA,
    COMPLETED,
    COMPRESSOR,
    CONFLICT,
    DUAL_RUN,
    FAN,
    KEEP_NEW,
    KEEP_OLD,
    OwnershipRegistry,
    PLANNED,
    ROLLED_BACK,
    SUPERSEDED,
    effective_time,
    floor_to_shift,
    next_shift_start,
)
from coldstore.store import Store

DAY = 24 * 60 * 60
# 2026-10-06 00:00:00 UTC is a Monday; use it as the pinned clock origin.
T0 = 1791072000


def open_registry() -> tuple[str, OwnershipRegistry]:
    tmp = tempfile.mkdtemp()
    store = Store.open(f"{tmp}/ownership.json")
    return tmp, OwnershipRegistry(store)


class ShiftClockCase(unittest.TestCase):
    def test_shift_boundaries(self) -> None:
        self.assertEqual(floor_to_shift(T0), T0)
        self.assertEqual(floor_to_shift(T0 + 7 * 3600), T0)
        self.assertEqual(floor_to_shift(T0 + 8 * 3600), T0 + 8 * 3600)
        self.assertEqual(floor_to_shift(T0 + 23 * 3600), T0 + 16 * 3600)

    def test_next_shift_start(self) -> None:
        self.assertEqual(next_shift_start(T0), T0 + 8 * 3600)
        self.assertEqual(next_shift_start(T0 + 7 * 3600), T0 + 8 * 3600)
        self.assertEqual(next_shift_start(T0 + 8 * 3600), T0 + 16 * 3600)
        self.assertEqual(next_shift_start(T0 + 23 * 3600), T0 + DAY)

    def test_effective_time(self) -> None:
        self.assertEqual(effective_time(T0 + 100, "emergency"), T0 + 100)
        self.assertEqual(effective_time(T0 + 100, "next_shift"), T0 + 8 * 3600)
        with self.assertRaises(ValueError):
            effective_time(T0, "later")


class RegistrationCase(unittest.TestCase):
    def test_first_emergency_registration_is_active(self) -> None:
        _, registry = open_registry()
        version = registry.register(
            COMPRESSOR, "c1", "room-a", 1,
            effective="emergency", reason="initial wiring", now=T0,
        )
        self.assertEqual(version.status, ACTIVE)
        self.assertEqual(version.valid_from, T0)
        self.assertEqual(version.version_id, "own-00000001")

    def test_planned_change_waits_for_next_shift(self) -> None:
        _, registry = open_registry()
        registry.register(COMPRESSOR, "c1", "room-a", 1, effective="emergency", now=T0)
        planned = registry.register(
            COMPRESSOR, "c1", "room-b", 2,
            effective="next_shift", now=T0 + 3600,
        )
        self.assertEqual(planned.status, PLANNED)
        self.assertEqual(planned.valid_from, T0 + 8 * 3600)

        # Before the boundary the old ownership still answers.
        answer = registry.query_subject(COMPRESSOR, "c1", T0 + 7 * 3600, now=T0 + 3600)
        self.assertEqual(answer.results[0].owner_room_id, "room-a")

        activated = registry.activate_due(now=T0 + 8 * 3600)
        self.assertEqual(activated, [planned.version_id])
        answer = registry.query_subject(COMPRESSOR, "c1", T0 + 8 * 3600, now=T0 + 8 * 3600)
        self.assertEqual(answer.results[0].owner_room_id, "room-b")
        self.assertEqual(answer.results[0].owner_zone, 2)

    def test_history_remains_queryable_after_change(self) -> None:
        _, registry = open_registry()
        first = registry.register(COMPRESSOR, "c1", "room-a", 1, effective="emergency", now=T0)
        registry.register(
            COMPRESSOR, "c1", "room-b", 2,
            effective="next_shift", now=T0 + 3600,
        )
        registry.activate_due(now=T0 + 8 * 3600)

        # The point in time page returns the owner as it was then.
        old = registry.query_subject(COMPRESSOR, "c1", T0 + 2 * 3600, now=T0 + 9 * 3600)
        self.assertEqual(old.results[0].owner_room_id, "room-a")
        self.assertEqual(old.results[0].version_id, first.version_id)
        new = registry.query_subject(COMPRESSOR, "c1", T0 + 10 * 3600, now=T0 + 9 * 3600)
        self.assertEqual(new.results[0].owner_room_id, "room-b")

        history = registry.history(COMPRESSOR, "c1")
        self.assertEqual([item.owner_room_id for item in history], ["room-a", "room-b"])
        self.assertEqual(history[0].status, SUPERSEDED)
        self.assertIsNotNone(history[0].valid_to)

    def test_emergency_change_must_name_superseded_version(self) -> None:
        _, registry = open_registry()
        registry.register(COMPRESSOR, "c1", "room-a", 1, effective="emergency", now=T0)
        with self.assertRaises(ValueError):
            registry.register(
                COMPRESSOR, "c1", "room-b", 2, effective="emergency", now=T0 + 10,
            )

        # Naming the live version is accepted and closes it immediately.
        replacement = registry.register(
            COMPRESSOR, "c1", "room-b", 2,
            effective="emergency", supersedes="own-00000001", now=T0 + 10,
        )
        self.assertEqual(replacement.status, ACTIVE)

    def test_validation_rejects_unknown_kind_and_owner(self) -> None:
        _, registry = open_registry()
        with self.assertRaises(ValueError):
            registry.register("pump", "p1", "room-a", 1, effective="emergency", now=T0)
        with self.assertRaises(ValueError):
            registry.register(FAN, "f1", "", 1, effective="emergency", now=T0)
        with self.assertRaises(ValueError):
            registry.register(FAN, "f1", "room-a", -3, effective="emergency", now=T0)


class BitemporalCase(unittest.TestCase):
    def test_late_backfill_does_not_rewrite_earlier_page_views(self) -> None:
        _, registry = open_registry()
        registry.register(AMMONIA, "a1", "room-a", 1, effective="emergency", now=T0)

        # On day 3 the page saw room-a owning the ammonia monitor on day 2.
        seen = registry.query_subject(AMMONIA, "a1", T0 + DAY, known_at=T0 + 3 * DAY, now=T0 + 3 * DAY)
        self.assertEqual(seen.results[0].owner_room_id, "room-a")

        # A correction is registered on day 5 saying room-b actually owned it
        # back on day 2.
        registry.register(
            AMMONIA, "a1", "room-b", 2,
            effective="emergency", supersedes="own-00000001",
            valid_from=T0 + DAY, reason="recovered paperwork", now=T0 + 5 * DAY,
        )

        # Re-running the same old page view (as known on day 3) is unchanged.
        old_view = registry.query_subject(AMMONIA, "a1", T0 + DAY, known_at=T0 + 3 * DAY, now=T0 + 6 * DAY)
        self.assertEqual(old_view.results[0].owner_room_id, "room-a")
        # But the current knowledge of that same business day now says room-b.
        corrected = registry.query_subject(AMMONIA, "a1", T0 + DAY, known_at=T0 + 6 * DAY, now=T0 + 6 * DAY)
        self.assertEqual(corrected.results[0].owner_room_id, "room-b")

    def test_unhandled_backdated_overlap_parks_conflict(self) -> None:
        _, registry = open_registry()
        registry.register(AMMONIA, "a1", "room-a", 1, effective="emergency", now=T0)
        parked = registry.register(
            AMMONIA, "a1", "room-c", 3,
            effective="next_shift", valid_from=T0 + 3600, now=T0 + 5 * DAY,
        )
        self.assertEqual(parked.status, CONFLICT)
        self.assertEqual(len(registry.conflicts()), 1)
        # Parked claims do not change any query answer.
        answer = registry.query_subject(AMMONIA, "a1", T0 + 2 * 3600, now=T0 + 5 * DAY)
        self.assertEqual(answer.results[0].owner_room_id, "room-a")

    def test_predating_backfill_parks_conflict_instead_of_rewriting(self) -> None:
        _, registry = open_registry()
        registry.register(
            AMMONIA, "a1", "room-a", 1, effective="emergency", valid_from=T0, now=T0,
        )
        # Claiming ownership from before the existing version starts would
        # otherwise rewrite the whole chain; it must park instead.
        parked = registry.register(
            AMMONIA, "a1", "room-z", 9,
            effective="emergency", valid_from=T0 - DAY, now=T0 + 2 * DAY,
        )
        self.assertEqual(parked.status, CONFLICT)
        answer = registry.query_subject(AMMONIA, "a1", T0 + 100, now=T0 + 2 * DAY)
        self.assertEqual(answer.results[0].owner_room_id, "room-a")

    def test_resolve_conflict_keep_new_and_keep_old(self) -> None:
        _, registry = open_registry()
        registry.register(AMMONIA, "a1", "room-a", 1, effective="emergency", now=T0)
        parked = registry.register(
            AMMONIA, "a1", "room-c", 3,
            effective="next_shift", valid_from=T0 + 3600, now=T0 + 5 * DAY,
        )
        kept = registry.resolve_conflict(parked.version_id, KEEP_NEW, by="chief", now=T0 + 6 * DAY)
        self.assertEqual(kept.status, ACTIVE)
        answer = registry.query_subject(AMMONIA, "a1", T0 + 2 * 3600, now=T0 + 6 * DAY)
        self.assertEqual(answer.results[0].owner_room_id, "room-c")

        _, registry2 = open_registry()
        registry2.register(AMMONIA, "a1", "room-a", 1, effective="emergency", now=T0)
        parked2 = registry2.register(
            AMMONIA, "a1", "room-c", 3,
            effective="next_shift", valid_from=T0 + 3600, now=T0 + 5 * DAY,
        )
        dropped = registry2.resolve_conflict(parked2.version_id, KEEP_OLD, by="chief", now=T0 + 6 * DAY)
        self.assertEqual(dropped.status, ABORTED)
        answer = registry2.query_subject(AMMONIA, "a1", T0 + 2 * 3600, now=T0 + 6 * DAY)
        self.assertEqual(answer.results[0].owner_room_id, "room-a")

    def test_predating_conflict_cannot_be_kept_new(self) -> None:
        _, registry = open_registry()
        registry.register(
            AMMONIA, "a1", "room-a", 1, effective="emergency", valid_from=T0, now=T0,
        )
        parked = registry.register(
            AMMONIA, "a1", "room-z", 9,
            effective="emergency", valid_from=T0 - DAY, now=T0 + 2 * DAY,
        )
        with self.assertRaises(ValueError):
            registry.resolve_conflict(parked.version_id, KEEP_NEW, now=T0 + 3 * DAY)


class ZoneQueryCase(unittest.TestCase):
    def test_query_zone_returns_every_owned_unit_as_of(self) -> None:
        _, registry = open_registry()
        registry.register(COMPRESSOR, "c1", "room-a", 1, effective="emergency", now=T0)
        registry.register(AMMONIA, "a1", "room-a", 1, effective="emergency", now=T0)
        registry.register(FAN, "f1", "room-a", 1, effective="emergency", now=T0)
        registry.register(
            COMPRESSOR, "c1", "room-b", 2,
            effective="next_shift", now=T0 + 3600,
        )
        registry.activate_due(now=T0 + 8 * 3600)

        before = registry.query_zone(1, T0 + 4 * 3600, now=T0 + 9 * 3600)
        kinds = sorted(item.subject_kind for item in before.results)
        self.assertEqual(kinds, ["ammonia", "compressor", "fan"])

        after = registry.query_zone(2, T0 + 9 * 3600, now=T0 + 9 * 3600)
        self.assertEqual([item.subject_id for item in after.results], ["c1"])

        # The old zone page at a past point still returns the compressor.
        past = registry.query_zone(1, T0 + 4 * 3600, now=T0 + 9 * 3600)
        self.assertIn("c1", [item.subject_id for item in past.results])

    def test_query_room_and_fingerprint_stable(self) -> None:
        _, registry = open_registry()
        registry.register(FAN, "f1", "room-a", 1, effective="emergency", now=T0)
        first = registry.query_room("room-a", T0 + 100, now=T0 + 100, log=False)
        second = registry.query_room("room-a", T0 + 200, now=T0 + 200, log=False)
        self.assertEqual(first.fingerprint, second.fingerprint)
        self.assertEqual(first.version_ids, second.version_ids)


class MigrationCase(unittest.TestCase):
    def _setup(self) -> OwnershipRegistry:
        _, registry = open_registry()
        registry.register(COMPRESSOR, "c1", "room-old", 10, effective="emergency", now=T0)
        return registry

    def test_dual_run_old_and_new_coexist_and_align(self) -> None:
        registry = self._setup()
        migration, new_version = registry.begin_migration(
            COMPRESSOR, "c1", "room-new", 20, by="eng", note="phase 3 expansion", now=T0 + 3600,
        )
        self.assertEqual(new_version.status, PLANNED)
        self.assertEqual(migration.status, "draft")

        registry.start_dual_run(migration.migration_id, now=T0 + 4 * 3600)
        view = registry.dual_view(migration.migration_id, as_of=T0 + 4 * 3600)
        self.assertEqual(view["status"], DUAL_RUN)
        self.assertEqual(view["old"]["owner_room_id"], "room-old")
        self.assertEqual(view["new"]["owner_room_id"], "room-new")
        self.assertEqual(view["authority"], "old")

        alignment = registry.check_alignment(
            migration.migration_id, as_of=T0 + 5 * 3600, now=T0 + 5 * 3600
        )
        self.assertTrue(alignment["aligned"])

    def test_cutover_at_shift_boundary_then_complete(self) -> None:
        registry = self._setup()
        migration, _ = registry.begin_migration(
            COMPRESSOR, "c1", "room-new", 20, now=T0 + 3600,
        )
        registry.start_dual_run(migration.migration_id, now=T0 + 4 * 3600)
        updated = registry.cutover(migration.migration_id, now=T0 + 5 * 3600)
        self.assertEqual(updated.cutover_at, T0 + 8 * 3600)
        self.assertIsNone(updated.completed_at)

        # Authority flips at the boundary when the shift clock runs; old
        # interval ends exactly where the new one starts.
        registry.activate_due(now=T0 + 8 * 3600)
        before = registry.query_subject(COMPRESSOR, "c1", T0 + 7 * 3600, now=T0 + 9 * 3600)
        after = registry.query_subject(COMPRESSOR, "c1", T0 + 8 * 3600, now=T0 + 9 * 3600)
        self.assertEqual(before.results[0].owner_room_id, "room-old")
        self.assertEqual(after.results[0].owner_room_id, "room-new")
        self.assertEqual(after.results[0].valid_from, before.results[0].valid_to)

        completed = registry.complete(migration.migration_id, now=T0 + 9 * 3600)
        self.assertEqual(completed.status, COMPLETED)

    def test_immediate_cutover(self) -> None:
        registry = self._setup()
        migration, _ = registry.begin_migration(
            COMPRESSOR, "c1", "room-new", 20, now=T0 + 3600,
        )
        registry.start_dual_run(migration.migration_id, now=T0 + 4 * 3600)
        updated = registry.cutover(migration.migration_id, immediate=True, now=T0 + 4 * 3600 + 10)
        self.assertEqual(updated.cutover_at, T0 + 4 * 3600 + 10)
        answer = registry.query_subject(COMPRESSOR, "c1", updated.cutover_at, now=updated.cutover_at)
        self.assertEqual(answer.results[0].owner_room_id, "room-new")

    def test_rollback_restores_old_ownership(self) -> None:
        registry = self._setup()
        migration, _ = registry.begin_migration(
            COMPRESSOR, "c1", "room-new", 20, now=T0 + 3600,
        )
        registry.start_dual_run(migration.migration_id, now=T0 + 4 * 3600)
        rolled = registry.rollback(migration.migration_id, by="chief", now=T0 + 5 * 3600)
        self.assertEqual(rolled.status, ROLLED_BACK)
        answer = registry.query_subject(COMPRESSOR, "c1", T0 + 6 * 3600, now=T0 + 6 * 3600)
        self.assertEqual(answer.results[0].owner_room_id, "room-old")

    def test_conflict_blocks_cutover(self) -> None:
        registry = self._setup()
        migration, new_version = registry.begin_migration(
            COMPRESSOR, "c1", "room-new", 20, now=T0 + 3600,
        )
        # A competing backdated claim arrives while dual run is open.
        registry.register(
            COMPRESSOR, "c1", "room-x", 30,
            effective="next_shift", valid_from=T0 + 4000, now=T0 + 5000,
        )
        registry.start_dual_run(migration.migration_id, now=T0 + 4 * 3600)
        with self.assertRaises(ValueError):
            registry.cutover(migration.migration_id, immediate=True, now=T0 + 5 * 3600)
        alignment = registry.check_alignment(migration.migration_id, as_of=T0 + 5 * 3600)
        self.assertFalse(alignment["aligned"])


class TraceCase(unittest.TestCase):
    def test_trace_records_versions_migrations_and_queries(self) -> None:
        _, registry = open_registry()
        registry.register(COMPRESSOR, "c1", "room-a", 1, effective="emergency", now=T0)
        registry.query_subject(COMPRESSOR, "c1", T0, now=T0)
        ops = [entry["op"] for entry in registry.trace()]
        self.assertIn("register", ops)
        self.assertIn("query", ops)
        query_entry = [entry for entry in registry.trace() if entry["op"] == "query"][0]
        self.assertIn("fingerprint", query_entry)
        self.assertEqual(query_entry["count"], 1)

    def test_trace_survives_reopen(self) -> None:
        tmp = tempfile.mkdtemp()
        store = Store.open(f"{tmp}/ownership.json")
        registry = OwnershipRegistry(store)
        registry.register(COMPRESSOR, "c1", "room-a", 1, effective="emergency", now=T0)
        reopened = OwnershipRegistry(Store.open(f"{tmp}/ownership.json"))
        self.assertEqual(len(reopened.history(COMPRESSOR, "c1")), 1)
        self.assertTrue(any(entry["op"] == "register" for entry in reopened.trace()))


if __name__ == "__main__":
    unittest.main()
