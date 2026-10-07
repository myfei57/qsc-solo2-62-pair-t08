"""Versioned ownership registry.

Rooms (库房) own three kinds of equipment subjects: compressor banks,
ammonia monitors and fan groups. Every ownership change is registered as a
new immutable version on a half open business time interval; the two clocks
below are deliberately independent:

* business time  -- ``valid_from`` / ``valid_to`` (shift boundaries)
* system time    -- ``registered_at`` / ``superseded_at``

That separation is what lets the ownership page return the ownership that
was in force at any past point, while a late backfilled correction does not
rewrite what the page showed last week.

Versions are append only. A change closes the previous version by writing
its end markers; the old payload is never deleted. Cross period expansions
run as migrations with an explicit dual run window in which both the old
and the new ownership answer are returned and checked for alignment.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import replace

from coldstore.store.store import Store

from . import ledger
from .report import (
    ABORTED,
    ACTIVE,
    COMPLETED,
    CONFLICT,
    DUAL_RUN,
    DRAFT,
    KEEP_NEW,
    KEEP_OLD,
    MIGRATING,
    PLANNED,
    ROLLED_BACK,
    SUPERSEDED,
    Migration,
    OwnershipView,
    QueryResult,
    Version,
    validate_effective,
    validate_kind,
    validate_owner,
)
from .shiftclock import effective_time, next_shift_start

LIVE_STATUSES = (PLANNED, ACTIVE)


class OwnershipRegistry:
    """Thread safe, store backed ownership registration."""

    def __init__(self, store: Store, auditor: object | None = None) -> None:
        self._store = store
        self._auditor = auditor
        self._lock = store._lock  # reuse the document lock

    # ------------------------------------------------------------------
   # Registration
    # ------------------------------------------------------------------
    def register(
        self,
        subject_kind: str,
        subject_id: str,
        owner_room_id: str,
        owner_zone: int,
        *,
        registered_by: str = "",
        effective: str = "next_shift",
        valid_from: int | None = None,
        supersedes: str = "",
        migration_id: str = "",
        reason: str = "",
        now: int | None = None,
    ) -> Version:
        """Register a new ownership version.

        Planned changes become effective at the next shift boundary. An
        emergency change is effective immediately but must name the version
        it replaces. Backdated corrections pass an explicit ``valid_from``.
        """

        validate_kind(subject_kind)
        validate_owner(owner_room_id, owner_zone)
        validate_effective(effective)
        at = self._now(now)
        start = valid_from if valid_from is not None else effective_time(at, effective)
        if start < 0:
            raise ValueError("valid_from must be non-negative")

        with self._lock:
            versions = ledger.load_versions(self._store)
            collisions = self._collisions(versions, subject_kind, subject_id, start)
            unhandled = [item for item in collisions if item.version_id != supersedes]

            # Ending a live version at or before it began would invert the
            # history chain; such a registration always parks as a conflict.
            inverting = any(item.valid_from >= start for item in unhandled)

            if effective == "emergency" and start <= at and not inverting:
                active = [item for item in collisions if item.status == ACTIVE]
                if active and not supersedes:
                    raise ValueError("emergency change must name the superseded version")
                if supersedes and supersedes not in {item.version_id for item in active}:
                    raise ValueError("supersedes does not name a live version at this time")

            status = ACTIVE if start <= at else PLANNED
            routine = (
                start > at
                and len(unhandled) == 1
                and unhandled[0].status == ACTIVE
                and unhandled[0].valid_to is None
            )
            if unhandled and (inverting or not routine):
                # Concurrent/backdated collision that is not an ordinary
                # scheduled succession: park the newcomer instead of
                # silently changing history.
                status = CONFLICT

            seq = ledger.next_seq(self._store)
            version = Version(
                version_id=f"own-{seq:08d}",
                subject_kind=subject_kind,
                subject_id=subject_id,
                owner_room_id=owner_room_id,
                owner_zone=owner_zone,
                valid_from=start,
                valid_to=None,
                registered_at=at,
                registered_by=registered_by,
                effective=effective,
                status=status,
                supersedes=supersedes,
                superseded_at=None,
                superseded_by="",
                migration_id=migration_id,
                reason=reason,
            )

            if status != CONFLICT:
                for item in collisions:
                    self._close(versions, item.version_id, start, at, registered_by)
            versions.append(version)
            ledger.save_versions(self._store, versions)

            self._trace(
                "register",
                at=at,
                version_id=version.version_id,
                kind=subject_kind,
                subject=subject_id,
                owner=owner_room_id,
                zone=owner_zone,
                valid_from=start,
                status=status,
                supersedes=supersedes,
                migration_id=migration_id,
            )
            return version

    def activate_due(self, now: int | None = None) -> list[str]:
        """Activate planned versions whose shift boundary has arrived."""

        at = self._now(now)
        activated: list[str] = []
        with self._lock:
            versions = ledger.load_versions(self._store)
            for index, version in enumerate(versions):
                if version.status != PLANNED or version.valid_from > at:
                    continue
                versions[index] = replace(version, status=ACTIVE)
                activated.append(version.version_id)
                # The predecessor stays business-authoritative right up to the
                # boundary, so it is only now marked superseded (transaction time).
                for other_index, other in enumerate(versions):
                    if (
                        other.subject_kind == version.subject_kind
                        and other.subject_id == version.subject_id
                        and other.valid_to == version.valid_from
                        and other.status == ACTIVE
                    ):
                        versions[other_index] = replace(
                            other, status=SUPERSEDED, superseded_at=at, superseded_by=version.registered_by
                        )
            if activated:
                ledger.save_versions(self._store, versions)
                for version_id in activated:
                    self._trace("activate", at=at, version_id=version_id)
        return activated

    # ------------------------------------------------------------------
    # Conflicts
    # ------------------------------------------------------------------
    def resolve_conflict(
        self,
        version_id: str,
        decision: str,
        *,
        by: str = "",
        now: int | None = None,
    ) -> Version:
        """Resolve a parked conflict by keeping the new or the old claim."""

        if decision not in (KEEP_NEW, KEEP_OLD):
            raise ValueError("decision must be keep_new or keep_old")
        at = self._now(now)
        with self._lock:
            versions = ledger.load_versions(self._store)
            version = self._require_version(versions, version_id)
            if version.status != CONFLICT:
                raise ValueError(f"version {version_id} is not in conflict")

            if decision == KEEP_OLD:
                updated = replace(
                    version,
                    status=ABORTED,
                    valid_to=version.valid_from,
                    superseded_at=at,
                    superseded_by=by,
                )
            else:
                collisions = self._collisions(
                    versions, version.subject_kind, version.subject_id, version.valid_from
                )
                inverting = [item for item in collisions if item.valid_from >= version.valid_from]
                if inverting:
                    raise ValueError(
                        "keeping a predating claim would invert later versions; "
                        "register explicit backdated corrections instead"
                    )
                for item in collisions:
                    self._close(versions, item.version_id, version.valid_from, at, by)
                status = ACTIVE if version.valid_from <= at else PLANNED
                updated = replace(version, status=status)
            self._put(versions, updated)
            ledger.save_versions(self._store, versions)
            self._trace(
                "resolve_conflict",
                at=at,
                version_id=version_id,
                decision=decision,
                result=updated.status,
            )
            return updated

    def conflicts(self) -> list[Version]:
        """Every version parked because of an unresolved overlap."""

        return [version for version in ledger.load_versions(self._store) if version.status == CONFLICT]

    # ------------------------------------------------------------------
    # Cross period migration with old/new coexistence
    # ------------------------------------------------------------------
    def begin_migration(
        self,
        subject_kind: str,
        subject_id: str,
        new_owner_room_id: str,
        new_owner_zone: int,
        *,
        old_version_id: str = "",
        by: str = "",
        note: str = "",
        now: int | None = None,
    ) -> tuple[Migration, Version]:
        """Register a planned new ownership and open a migration envelope."""

        at = self._now(now)
        with self._lock:
            versions = ledger.load_versions(self._store)
            if old_version_id:
                old = self._require_version(versions, old_version_id)
            else:
                old = self._authoritative(versions, subject_kind, subject_id, at)
                if old is None:
                    raise ValueError(f"no existing ownership for {subject_kind} {subject_id}")
            if old.subject_kind != subject_kind or old.subject_id != subject_id:
                raise ValueError("old version does not belong to this subject")

            seq = ledger.next_seq(self._store)
            migration_id = f"mig-{seq:08d}"
            start = next_shift_start(at)
            new_version = Version(
                version_id=f"own-{seq:08d}",
                subject_kind=subject_kind,
                subject_id=subject_id,
                owner_room_id=new_owner_room_id,
                owner_zone=new_owner_zone,
                valid_from=start,
                valid_to=None,
                registered_at=at,
                registered_by=by,
                effective="next_shift",
                status=PLANNED,
                supersedes=old.version_id,
                superseded_at=None,
                superseded_by="",
                migration_id=migration_id,
                reason=note,
            )
            migration = Migration(
                migration_id=migration_id,
                subject_kind=subject_kind,
                subject_id=subject_id,
                old_version_id=old.version_id,
                new_version_id=new_version.version_id,
                status=DRAFT,
                started_at=at,
                started_by=by,
                dual_from=0,
                cutover_at=None,
                completed_at=None,
                note=note,
            )
            migrations = ledger.load_migrations(self._store)
            migrations.append(migration)
            versions.append(new_version)
            ledger.save_migrations(self._store, migrations)
            ledger.save_versions(self._store, versions)
            self._trace(
                "migration_begin",
                at=at,
                migration_id=migration_id,
                kind=subject_kind,
                subject=subject_id,
                old=old.version_id,
                new=new_version.version_id,
            )
            return migration, new_version

    def start_dual_run(self, migration_id: str, *, now: int | None = None) -> Migration:
        """Put a migration into dual run so old and new answers coexist."""

        at = self._now(now)
        with self._lock:
            migrations = ledger.load_migrations(self._store)
            migration = self._require_migration(migrations, migration_id)
            if migration.status not in (DRAFT, MIGRATING):
                raise ValueError(f"migration {migration_id} cannot enter dual run from {migration.status}")
            updated = replace(migration, status=DUAL_RUN, dual_from=at)
            self._put_migration(migrations, updated)
            ledger.save_migrations(self._store, migrations)
            self._trace("migration_dual_run", at=at, migration_id=migration_id)
            return updated

    def cutover(
        self,
        migration_id: str,
        *,
        immediate: bool = False,
        by: str = "",
        now: int | None = None,
    ) -> Migration:
        """Make the new ownership authoritative at a shift boundary."""

        at = self._now(now)
        with self._lock:
            migrations = ledger.load_migrations(self._store)
            migration = self._require_migration(migrations, migration_id)
            if migration.status != DUAL_RUN:
                raise ValueError("cutover requires an active dual run")
            versions = ledger.load_versions(self._store)
            new = self._require_version(versions, migration.new_version_id)
            old = self._require_version(versions, migration.old_version_id)
            blocking = [
                version.version_id
                for version in versions
                if version.status == CONFLICT
                and version.subject_kind == migration.subject_kind
                and version.subject_id == migration.subject_id
            ]
            if new.status == CONFLICT or blocking:
                raise ValueError("unresolved conflict on the subject; resolve before cutover")
            boundary = at if immediate else next_shift_start(at)

            # Snap the planned new version to the cutover boundary and close
            # the old version there, so the two intervals meet without a gap
            # or an overlap. Both statuses only flip when the boundary is now.
            updated_new = replace(
                new,
                valid_from=boundary,
                status=ACTIVE if boundary <= at else PLANNED,
            )
            self._put(versions, updated_new)
            self._close(versions, old.version_id, boundary, at, by)
            ledger.save_versions(self._store, versions)

            updated = replace(migration, cutover_at=boundary)
            self._put_migration(migrations, updated)
            ledger.save_migrations(self._store, migrations)
            self._trace(
                "migration_cutover",
                at=at,
                migration_id=migration_id,
                boundary=boundary,
                immediate=immediate,
            )
            return updated

    def complete(self, migration_id: str, *, now: int | None = None) -> Migration:
        """Finish a migration after alignment has been verified."""

        at = self._now(now)
        with self._lock:
            migrations = ledger.load_migrations(self._store)
            migration = self._require_migration(migrations, migration_id)
            if migration.cutover_at is None:
                raise ValueError("cannot complete before cutover")
            alignment = self.check_alignment(migration_id, as_of=at, now=at)
            if not alignment["aligned"]:
                raise ValueError("old and new ownership answers are not aligned")
            updated = replace(migration, status=COMPLETED, completed_at=at)
            self._put_migration(migrations, updated)
            ledger.save_migrations(self._store, migrations)
            self._trace("migration_complete", at=at, migration_id=migration_id)
            return updated

    def rollback(self, migration_id: str, *, by: str = "", now: int | None = None) -> Migration:
        """Abandon the new ownership and restore the old one."""

        at = self._now(now)
        with self._lock:
            migrations = ledger.load_migrations(self._store)
            migration = self._require_migration(migrations, migration_id)
            if migration.status == COMPLETED:
                raise ValueError("completed migrations cannot be rolled back")
            versions = ledger.load_versions(self._store)
            new = self._require_version(versions, migration.new_version_id)
            old = self._require_version(versions, migration.old_version_id)
            self._put(
                versions,
                replace(
                    new,
                    status=ABORTED,
                    valid_to=new.valid_from,
                    superseded_at=at,
                    superseded_by=by,
                ),
            )
            restored = replace(old, valid_to=None, status=ACTIVE if old.valid_from <= at else old.status)
            self._put(versions, restored)
            ledger.save_versions(self._store, versions)

            updated = replace(migration, status=ROLLED_BACK)
            self._put_migration(migrations, updated)
            ledger.save_migrations(self._store, migrations)
            self._trace(
                "migration_rollback",
                at=at,
                migration_id=migration_id,
                by=by,
                restored=old.version_id,
            )
            return updated

    def check_alignment(self, migration_id: str, *, as_of: int, now: int | None = None) -> dict[str, object]:
        """Compare the old and new answers and confirm they agree.

        During dual run the two ownerships coexist on purpose: the old side
        stays authoritative until the cutover boundary and the new side takes
        over exactly there. Alignment means both versions resolve, cover the
        point without a gap or an overlap from their own side of the boundary,
        and no competing claim has parked the subject in conflict.
        """

        at = self._now(now)
        with self._lock:
            migrations = ledger.load_migrations(self._store)
            migration = self._require_migration(migrations, migration_id)
            versions = ledger.load_versions(self._store)
            old_version = self._require_version(versions, migration.old_version_id)
            new_version = self._require_version(versions, migration.new_version_id)
            boundary = migration.cutover_at or new_version.valid_from
            conflicts = [
                item.version_id
                for item in versions
                if item.status == CONFLICT
                and item.subject_kind == migration.subject_kind
                and item.subject_id == migration.subject_id
            ]

            # Before cutover the predecessor is still open; cutover closes it
            # exactly at the boundary. Either shape is aligned.
            old_ok = old_version.status in (ACTIVE, SUPERSEDED) and old_version.valid_from < boundary
            if at >= boundary:
                old_ok = old_ok and old_version.valid_to == boundary
            new_ok = (
                new_version.status in (PLANNED, ACTIVE)
                and new_version.valid_from == boundary
            )
            subject_versions = [
                v
                for v in versions
                if v.subject_kind == migration.subject_kind and v.subject_id == migration.subject_id
            ]
            side_old = self._pick(subject_versions, boundary - 1, at)
            if at >= boundary:
                side_new = self._pick(subject_versions, boundary, at)
            else:
                side_new = next(
                    (v for v in subject_versions if v.version_id == new_version.version_id), None
                )
            pages_ok = side_old is not None and side_old.version_id == old_version.version_id
            # The new side is only required to resolve through the live query
            # once the boundary has arrived; before then it is merely staged.
            if at >= boundary:
                pages_ok = (
                    pages_ok
                    and side_new is not None
                    and side_new.version_id == new_version.version_id
                )
            else:
                pages_ok = pages_ok and side_new is not None and side_new.status == PLANNED
            aligned = old_ok and new_ok and pages_ok and not conflicts
            return {
                "migration_id": migration_id,
                "as_of": as_of,
                "boundary": boundary,
                "aligned": aligned,
                "old": _view(old_version).as_dict(),
                "new": _view(new_version).as_dict(),
                "conflicts": conflicts,
                "checked_at": at,
            }

    def migrations(self, status: str = "") -> list[Migration]:
        migrations = ledger.load_migrations(self._store)
        if status:
            migrations = [item for item in migrations if item.status == status]
        return migrations

    # ------------------------------------------------------------------
    # Queries -- the ownership page
    # ------------------------------------------------------------------
    def query_subject(
        self,
        subject_kind: str,
        subject_id: str,
        as_of: int | None = None,
        *,
        known_at: int | None = None,
        log: bool = True,
        now: int | None = None,
    ) -> QueryResult:
        """Return the ownership of one unit at a business time point."""

        validate_kind(subject_kind)
        at = self._now(now)
        point = at if as_of is None else as_of
        known = at if known_at is None else known_at
        with self._lock:
            versions = [
                version
                for version in ledger.load_versions(self._store)
                if version.subject_kind == subject_kind
                and version.subject_id == subject_id
                and version.registered_at <= known
                and version.status not in (ABORTED, CONFLICT)
            ]
            result = self._pick(versions, point, known)
            answer = self._result(point, known, result)
            if log:
                self._trace_query(answer, subject=subject_kind + ":" + subject_id, at=at)
            return answer

    def query_zone(
        self,
        zone: int,
        as_of: int | None = None,
        *,
        known_at: int | None = None,
        log: bool = True,
        now: int | None = None,
    ) -> QueryResult:
        """Return every unit owned by a partition at a business time point."""

        return self._query_owner(zone=zone, as_of=as_of, known_at=known_at, log=log, now=now)

    def query_room(
        self,
        room_id: str,
        as_of: int | None = None,
        *,
        known_at: int | None = None,
        log: bool = True,
        now: int | None = None,
    ) -> QueryResult:
        """Return every unit owned by a room at a business time point."""

        return self._query_owner(room_id=room_id, as_of=as_of, known_at=known_at, log=log, now=now)

    def history(self, subject_kind: str, subject_id: str) -> list[Version]:
        """Full registration history for one unit, oldest first."""

        validate_kind(subject_kind)
        return [
            version
            for version in ledger.load_versions(self._store)
            if version.subject_kind == subject_kind and version.subject_id == subject_id
        ]

    def dual_view(self, migration_id: str, as_of: int | None = None) -> dict[str, object]:
        """Return both the old and new answer while a migration coexists."""

        point = self._now(None) if as_of is None else as_of
        with self._lock:
            migrations = ledger.load_migrations(self._store)
            migration = self._require_migration(migrations, migration_id)
            versions = ledger.load_versions(self._store)
            old = self._require_version(versions, migration.old_version_id)
            new = self._require_version(versions, migration.new_version_id)
            return {
                "migration_id": migration_id,
                "status": migration.status,
                "as_of": point,
                "old": _view(old).as_dict(),
                "new": _view(new).as_dict(),
                "authority": "old" if migration.cutover_at is None or point < migration.cutover_at else "new",
                "aligned": self.check_alignment(migration_id, as_of=point, now=point)["aligned"],
            }

    def trace(self) -> list[dict[str, object]]:
        """The append only attribution trace ledger."""

        return ledger.load_trace(self._store)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _query_owner(
        self,
        *,
        zone: int | None = None,
        room_id: str | None = None,
        as_of: int | None,
        known_at: int | None,
        log: bool,
        now: int | None,
    ) -> QueryResult:
        at = self._now(now)
        point = at if as_of is None else as_of
        known = at if known_at is None else known_at
        with self._lock:
            candidates = [
                version
                for version in ledger.load_versions(self._store)
                if version.registered_at <= known
                and version.status not in (ABORTED, CONFLICT)
                and (zone is None or version.owner_zone == zone)
                and (room_id is None or version.owner_room_id == room_id)
            ]
            results: list[OwnershipView] = []
            for kind in ("compressor", "ammonia", "fan"):
                for subject_id in sorted({v.subject_id for v in candidates if v.subject_kind == kind}):
                    chain = [v for v in candidates if v.subject_kind == kind and v.subject_id == subject_id]
                    picked = self._pick(chain, point, known)
                    if picked is not None:
                        owner_match = (zone is None or picked.owner_zone == zone) and (
                            room_id is None or picked.owner_room_id == room_id
                        )
                        if owner_match:
                            results.append(_view(picked))
            answer = self._result(point, known, results)
            if log:
                self._trace_query(answer, subject=f"zone:{zone}" if zone is not None else f"room:{room_id}", at=at)
            return answer

    def _pick(self, versions: list[Version], point: int, known: int) -> Version | None:
        """Resolve the authoritative version for a business point as known at a system time.

        Each version is evaluated against the interval the system knew at
        ``known``: when its supersession had not yet been recorded, the end
        was still open. That interval closes to the recorded ``valid_to``
        only once ``superseded_at`` is in the past, which keeps last week's
        ownership page unchanged after a correction registered this week.
        """

        live: list[Version] = []
        for version in versions:
            if version.status in (ABORTED, CONFLICT):
                continue
            if version.registered_at > known:
                continue
            # A version whose business start is still in the future cannot be
            # authoritative; the predecessor keeps answering until then.
            if version.valid_from > point:
                continue
            if version.superseded_at is not None and version.superseded_at <= known:
                known_end = version.valid_to
            else:
                known_end = None
            if known_end is not None and known_end <= point:
                continue
            live.append(version)
        if not live:
            return None
        # Newest registration wins; sequence is embedded in the version id.
        return max(live, key=lambda item: (item.registered_at, item.version_id))

    def _result(self, point: int, known: int, picked: Version | None | list[OwnershipView]) -> QueryResult:
        if isinstance(picked, list):
            results = picked
        else:
            results = [] if picked is None else [_view(picked)]
        version_ids = [item.version_id for item in results]
        digest_src = "|".join(
            f"{item.subject_kind}:{item.subject_id}={item.owner_room_id}@{item.owner_zone}:{item.version_id}"
            for item in results
        )
        fingerprint = hashlib.sha256(digest_src.encode("utf-8")).hexdigest()[:16]
        return QueryResult(
            as_of=point,
            known_as_of=known,
            results=results,
            version_ids=version_ids,
            fingerprint=fingerprint,
        )

    def _collisions(
        self, versions: list[Version], kind: str, subject_id: str, start: int
    ) -> list[Version]:
        """Live versions whose business interval overlaps an interval starting at ``start``."""

        return [
            version
            for version in versions
            if version.subject_kind == kind
            and version.subject_id == subject_id
            and version.status in LIVE_STATUSES
            and (version.valid_to is None or version.valid_to > start)
        ]

    def _authoritative(
        self, versions: list[Version], kind: str, subject_id: str, point: int
    ) -> Version | None:
        chain = [
            version
            for version in versions
            if version.subject_kind == kind and version.subject_id == subject_id
        ]
        return self._pick(chain, point, point)

    def _close(
        self, versions: list[Version], version_id: str, boundary: int, at: int, by: str
    ) -> None:
        """Set the business end of a version.

        A version closed at a boundary still in the future stays ACTIVE until
        then; its transaction-time supersession is stamped on activation or
        cutover, so point-in-time-as-known queries keep returning it right up
        to the boundary.
        """

        version = self._require_version(versions, version_id)
        if boundary <= version.valid_from:
            # A version cannot end at or before it began; overlaps that would
            # require this must be parked as conflicts instead.
            raise ValueError("cannot close an ownership version at or before its start")
        if version.valid_to is not None and version.valid_to <= boundary:
            return
        if boundary <= at:
            updated = replace(
                version,
                valid_to=boundary,
                superseded_at=at,
                superseded_by=by,
                status=SUPERSEDED,
            )
        else:
            updated = replace(version, valid_to=boundary)
        self._put(versions, updated)

    @staticmethod
    def _put(versions: list[Version], updated: Version) -> None:
        for index, version in enumerate(versions):
            if version.version_id == updated.version_id:
                versions[index] = updated
                return
        raise ValueError(f"version {updated.version_id} not found")

    @staticmethod
    def _require_version(versions: list[Version], version_id: str) -> Version:
        for version in versions:
            if version.version_id == version_id:
                return version
        raise ValueError(f"ownership version {version_id} not found")

    @staticmethod
    def _require_migration(migrations: list[Migration], migration_id: str) -> Migration:
        for migration in migrations:
            if migration.migration_id == migration_id:
                return migration
        raise ValueError(f"migration {migration_id} not found")

    @staticmethod
    def _put_migration(migrations: list[Migration], updated: Migration) -> None:
        for index, migration in enumerate(migrations):
            if migration.migration_id == updated.migration_id:
                migrations[index] = updated
                return
        raise ValueError(f"migration {updated.migration_id} not found")

    def _trace_query(self, answer: QueryResult, *, subject: str, at: int) -> None:
        self._trace(
            "query",
            at=at,
            subject=subject,
            as_of=answer.as_of,
            known_as_of=answer.known_as_of,
            count=len(answer.results),
            fingerprint=answer.fingerprint,
            version_ids=",".join(answer.version_ids),
        )

    def _trace(self, op: str, **fields: object) -> None:
        entry: dict[str, object] = {"op": op}
        entry.update(fields)
        ledger.append_trace(self._store, entry)
        if self._auditor is not None:
            self._auditor.record(f"ownership:{op}", str(fields))

    @staticmethod
    def _now(now: int | None) -> int:
        return int(time.time()) if now is None else now


def _view(version: Version) -> OwnershipView:
    return OwnershipView(
        subject_kind=version.subject_kind,
        subject_id=version.subject_id,
        owner_room_id=version.owner_room_id,
        owner_zone=version.owner_zone,
        version_id=version.version_id,
        status=version.status,
        valid_from=version.valid_from,
        valid_to=version.valid_to,
    )
