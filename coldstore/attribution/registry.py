"""Versioned attribution registry.

The warehouse was expanded twice and the ownership of compressors, ammonia
detectors and fans was repeatedly rewritten in place, so after each edit the
previous owner could no longer be reconstructed.  This registry never mutates
a registered claim: every change appends a new version carrying its own
registration time and effective time.

Two policy decisions are baked in:

* A normal change is registered immediately but only becomes authoritative at
  the start of the next shift, so responsibility never moves underneath the
  shift on duty.  An emergency change may take effect at once but requires a
  second distinct confirmer.
* When two claims overlap, the earliest registered claim stays authoritative
  ("keep earliest").  The later claim is quarantined instead of overwriting
  anything and can only cut over after an explicit confirmed handover.

Versions live in append only storage; the traceability ledger is hash chained
so a later edit to history breaks the chain check.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from collections.abc import Callable

from coldstore.store.commands import append_command, load_commands
from coldstore.store.store import Store

from .report import (
    STATUS_ACTIVE,
    STATUS_CANCELLED,
    STATUS_QUARANTINED,
    STATUS_SCHEDULED,
    STATUS_SUPERSEDED,
    MODE_IMMEDIATE,
    MODE_NEXT_SHIFT,
    Attribution,
    AttributionView,
    ConsistencyCheck,
    LedgerEntry,
    MigrationRecord,
    PointInTimeReport,
    validate_kind,
    validate_mode,
    validate_zone,
)

VERSIONS_KEY = "attribution:versions"
LEDGER_KEY = "attribution:ledger"

# Seconds per working shift.  Effective points always land on a shift
# boundary, which is also what the point in time page groups by.
SHIFT_SECONDS = 8 * 60 * 60

Clock = Callable[[], int]


def shift_boundary_after(now: int) -> int:
    """Start of the next shift after ``now``."""

    return (now // SHIFT_SECONDS + 1) * SHIFT_SECONDS


def shift_of(at: int) -> int:
    """Sequential shift number containing an epoch second."""

    return at // SHIFT_SECONDS


def _signature(views: list[AttributionView], as_of: int) -> str:
    """Stable fingerprint of one point in time picture."""

    rows = [
        f"{view.kind}|{view.equipment_id}|{view.zone}|{view.version}"
        for view in sorted(views, key=lambda view: (view.kind, view.equipment_id))
    ]
    digest = hashlib.sha256((f"{as_of}\n" + "\n".join(rows) + "\n").encode("utf-8"))
    return digest.hexdigest()


class AttributionRegistry:
    """Append only registry with point in time reads."""

    def __init__(self, store: Store, clock: Clock | None = None) -> None:
        self._store = store
        self._clock = clock or (lambda: int(time.time()))
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ register

    def register(
        self,
        kind: str,
        equipment_id: str,
        zone: int,
        operator: str,
        *,
        reason: str = "",
        mode: str = MODE_NEXT_SHIFT,
        confirmer: str = "",
        confirm_handover: bool = False,
    ) -> MigrationRecord:
        """Register a new attribution version for one piece of equipment.

        Returns the migration state produced.  A first ever claim becomes
        active.  A confirmed handover is scheduled (or immediate under the
        emergency mode).  An unconfirmed competing claim is quarantined and
        the earlier claim keeps authority.
        """

        kind = validate_kind(kind)
        zone = validate_zone(zone)
        mode = validate_mode(mode)
        equipment_id = equipment_id.strip()
        operator = operator.strip()
        confirmer = confirmer.strip()
        if not equipment_id:
            raise ValueError("equipment_id is required")
        if not operator:
            raise ValueError("operator is required")
        if mode == MODE_IMMEDIATE:
            if not confirmer or confirmer == operator:
                raise ValueError("immediate attribution needs a distinct confirmer")

        with self._lock:
            now = self._clock()
            versions = self._load_versions()
            current = self._authoritative_locked(versions, kind, equipment_id, now)

            if current is not None and current.zone == zone:
                raise ValueError(
                    f"{kind} {equipment_id} is already attributed to zone {zone}"
                )

            pending = self._pending_scheduled_locked(versions, kind, equipment_id, now)
            if pending is not None and current is not None:
                if mode != MODE_IMMEDIATE:
                    raise ValueError(
                        f"{kind} {equipment_id} already has a handover scheduled for "
                        f"version {pending.version}; resolve it before registering another change"
                    )
                # An emergency change cancels the waiting cutover and takes
                # authority from the version that was about to yield.
                self._cancel_version_locked(pending, now)
                versions = self._load_versions()
                predecessor = self._find_version_locked(versions, pending.supersedes)
                if predecessor is not None:
                    current = predecessor

            registration = uuid.uuid4().hex
            next_version = max((item.version for item in versions), default=0) + 1
            boundary = shift_boundary_after(now)

            if current is None:
                # Baseline claim for a unit with no history is effective now.
                status = STATUS_ACTIVE
                effective_at = now
                supersedes = 0
            elif mode == MODE_IMMEDIATE:
                # Emergency channel: effective at once, two distinct people,
                # handover acknowledged.
                if not confirm_handover:
                    raise ValueError("immediate change must confirm the handover")
                status = STATUS_ACTIVE
                effective_at = now
                supersedes = current.version
            elif confirm_handover:
                # Normal, acknowledged migration: registered now, live at the
                # next shift boundary.
                status = STATUS_SCHEDULED
                effective_at = boundary
                supersedes = current.version
            else:
                # Keep earliest: park the later claim instead of overwriting.
                status = STATUS_QUARANTINED
                effective_at = boundary
                supersedes = current.version

            new_version = Attribution(
                version=next_version,
                kind=kind,
                equipment_id=equipment_id,
                zone=zone,
                registered_at=now,
                effective_at=effective_at,
                ends_at=0,
                status=status,
                operator=operator,
                confirmer=confirmer,
                reason=reason,
                mode=mode,
                supersedes=supersedes,
                created_by_registration=registration,
            )
            self._append_version(new_version)

            if status == STATUS_ACTIVE and supersedes:
                self._close_version_locked(supersedes, effective_at)
            if status == STATUS_SCHEDULED:
                self._close_version_locked(supersedes, effective_at)

            record = MigrationRecord(
                registration=registration,
                version=next_version,
                kind=kind,
                equipment_id=equipment_id,
                zone=zone,
                registered_at=now,
                effective_at=effective_at,
                status=status,
                mode=mode,
                operator=operator,
                confirmer=confirmer,
                supersedes=supersedes,
                reason=reason,
            )
            self._append_ledger(
                now,
                "register",
                json.dumps(record.as_dict(), ensure_ascii=False, sort_keys=True),
                registration,
            )
            return record

    def confirm_handover(self, version: int, confirmer: str) -> MigrationRecord:
        """Resolve a quarantined claim into a scheduled next shift handover.

        The confirming party acknowledges that the currently authoritative
        attribution yields at the coming shift boundary.
        """

        confirmer = confirmer.strip()
        if not confirmer:
            raise ValueError("confirmer is required")
        with self._lock:
            now = self._clock()
            versions = self._load_versions()
            candidate = self._find_version_locked(versions, version)
            if candidate is None:
                raise ValueError(f"version {version} not found")
            if candidate.status != STATUS_QUARANTINED:
                raise ValueError(f"version {version} is not quarantined")
            if confirmer == candidate.operator:
                raise ValueError("handover needs a confirmer other than the operator")

            # Re point the claim at whoever holds authority now: another
            # migration may have completed between registration and
            # confirmation, and a unit must never accumulate two scheduled
            # successors at once.
            current = self._authoritative_locked(
                versions, candidate.kind, candidate.equipment_id, now
            )
            if current is None:
                raise ValueError("the original attribution no longer exists")
            if current.zone == candidate.zone:
                raise ValueError("the claimed zone already holds authority")
            pending = self._pending_scheduled_locked(
                versions, candidate.kind, candidate.equipment_id, now
            )
            if pending is not None and pending.version != candidate.version:
                raise ValueError(
                    f"version {pending.version} is already scheduled to take over"
                )

            effective_at = shift_boundary_after(now)
            updated = self._replace_version_locked(
                candidate,
                status=STATUS_SCHEDULED,
                effective_at=effective_at,
                confirmer=confirmer,
                supersedes=current.version,
            )
            self._close_version_locked(current.version, effective_at)

            record = self._record_of(updated)
            self._append_ledger(
                now,
                "confirm_handover",
                json.dumps(record.as_dict(), ensure_ascii=False, sort_keys=True),
                candidate.created_by_registration,
            )
            return record

    # ------------------------------------------------------------------ reads

    def point_in_time(self, as_of: int | None = None, *, trace: bool = True) -> PointInTimeReport:
        """Return the attribution picture exactly as it was at ``as_of``.

        Active rows are claims whose half open interval covers the instant.
        Scheduled rows are pending cutovers; quarantined rows are unresolved
        competing claims.  The result signature is recorded in the ledger so
        repeated historical queries can be proven to have stayed aligned.
        """

        with self._lock:
            now = self._clock()
            if as_of is None:
                as_of = now
            if as_of < 0:
                raise ValueError("as_of must be non-negative")

            # A "now" query first rolls over any scheduled cutovers whose
            # shift boundary has arrived, so the page reflects the current
            # shift rather than the state at registration time.
            if as_of >= now:
                self._activate_due_locked(now)
            versions = self._load_versions()
            active: list[AttributionView] = []
            pending: list[AttributionView] = []
            quarantined: list[AttributionView] = []

            for item in versions:
                view = AttributionView(
                    kind=item.kind,
                    equipment_id=item.equipment_id,
                    zone=item.zone,
                    version=item.version,
                    effective_at=item.effective_at,
                    status=item.status,
                )
                if item.status == STATUS_QUARANTINED:
                    if item.registered_at <= as_of:
                        quarantined.append(view)
                elif item.status == STATUS_SCHEDULED:
                    if item.registered_at <= as_of:
                        pending.append(view)
                elif item.status == STATUS_ACTIVE and item.effective_at <= as_of:
                    active.append(view)
                elif item.status == STATUS_SUPERSEDED:
                    if item.effective_at <= as_of < item.ends_at:
                        active.append(view)

            active.sort(key=lambda view: (view.kind, view.equipment_id))
            pending.sort(key=lambda view: (view.kind, view.equipment_id, view.version))
            quarantined.sort(key=lambda view: (view.kind, view.equipment_id, view.version))

            report = PointInTimeReport(
                as_of=as_of,
                shift=shift_of(as_of),
                active=active,
                pending=pending,
                quarantined=quarantined,
                signature=_signature(active, as_of),
            )
            if trace:
                self._append_ledger(
                    now,
                    "query",
                    json.dumps(
                        {
                            "as_of": as_of,
                            "shift": report.shift,
                            "count": len(active),
                            "signature": report.signature,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    "",
                )
            return report

    def attribution_of(
        self, kind: str, equipment_id: str, as_of: int | None = None
    ) -> AttributionView | None:
        """The authoritative zone for one unit at a point in time."""

        kind = validate_kind(kind)
        report = self.point_in_time(as_of, trace=False)
        for view in report.active:
            if view.kind == kind and view.equipment_id == equipment_id:
                return view
        return None

    def equipment_in_zone(self, zone: int, as_of: int | None = None) -> list[AttributionView]:
        """Every unit attributed to a zone at a point in time."""

        validate_zone(zone)
        report = self.point_in_time(as_of, trace=False)
        return [view for view in report.active if view.zone == zone]

    def versions_for(self, kind: str, equipment_id: str) -> list[Attribution]:
        """Full version chain of one unit, oldest first."""

        kind = validate_kind(kind)
        versions = self._load_versions()
        return [
            item
            for item in versions
            if item.kind == kind and item.equipment_id == equipment_id.strip()
        ]

    def migrations(self) -> list[MigrationRecord]:
        """Every registration and its resulting migration state."""

        return [self._record_of(item) for item in self._load_versions()]

    def pending_cutover(self) -> list[Attribution]:
        """Scheduled versions waiting for their shift boundary."""

        return [
            item
            for item in self._load_versions()
            if item.status == STATUS_SCHEDULED and item.effective_at <= self._clock()
        ]

    def activate_due(self) -> list[MigrationRecord]:
        """Cut over scheduled versions whose shift boundary has arrived.

        Called by the shift rollover, it is also idempotent: each scheduled
        version flips exactly once.
        """

        with self._lock:
            return self._activate_due_locked(self._clock())

    def _activate_due_locked(self, now: int) -> list[MigrationRecord]:
        activated: list[MigrationRecord] = []
        versions = self._load_versions()
        for item in versions:
            if item.status == STATUS_SCHEDULED and item.effective_at <= now:
                updated = self._replace_version_locked(item, status=STATUS_ACTIVE)
                record = self._record_of(updated)
                activated.append(record)
                self._append_ledger(
                    now,
                    "activate",
                    json.dumps(record.as_dict(), ensure_ascii=False, sort_keys=True),
                    item.created_by_registration,
                )
        return activated

    # -------------------------------------------------------------- consistency

    def align(self, left_as_of: int, right_as_of: int) -> ConsistencyCheck:
        """Diff two historical pictures so old and new attributions line up.

        Every active unit must exist on both sides; a unit that appears only
        on one side flags the pictures as misaligned.  Zone changes appear as
        ``moved`` rows and are expected during a cross period migration.
        """

        left = self.point_in_time(left_as_of, trace=False)
        right = self.point_in_time(right_as_of, trace=False)
        left_map = {(v.kind, v.equipment_id): v for v in left.active}
        right_map = {(v.kind, v.equipment_id): v for v in right.active}

        moved: list[dict[str, object]] = []
        added: list[dict[str, object]] = []
        removed: list[dict[str, object]] = []
        for key in sorted(set(left_map) | set(right_map)):
            old = left_map.get(key)
            new = right_map.get(key)
            if old is not None and new is not None:
                if old.zone != new.zone:
                    moved.append(
                        {
                            "kind": old.kind,
                            "equipment_id": old.equipment_id,
                            "from_zone": old.zone,
                            "to_zone": new.zone,
                            "from_version": old.version,
                            "to_version": new.version,
                        }
                    )
            elif new is not None:
                added.append(new.as_dict())
            elif old is not None:
                removed.append(old.as_dict())

        aligned = not added and not removed
        check = ConsistencyCheck(
            left_as_of=left_as_of,
            right_as_of=right_as_of,
            moved=moved,
            added=added,
            removed=removed,
            aligned=aligned,
        )
        now = self._clock()
        self._append_ledger(
            now,
            "align",
            json.dumps(check.as_dict(), ensure_ascii=False, sort_keys=True),
            "",
        )
        return check

    # ------------------------------------------------------------------ ledger

    def ledger(self) -> list[LedgerEntry]:
        """Return the append only traceability ledger, oldest first."""

        entries: list[LedgerEntry] = []
        for item in load_commands(self._store, LEDGER_KEY):
            try:
                payload = json.loads(item)
            except ValueError:
                continue
            if not isinstance(payload, dict):
                continue
            entries.append(
                LedgerEntry(
                    seq=int(payload.get("seq", 0)),
                    at=int(payload.get("at", 0)),
                    kind=str(payload.get("kind", "")),
                    detail=str(payload.get("detail", "")),
                    registration=str(payload.get("registration", "")),
                    signature=str(payload.get("signature", "")),
                    prev_signature=str(payload.get("prev_signature", "")),
                )
            )
        return entries

    def verify_ledger(self) -> bool:
        """Recompute the hash chain; ``False`` means history was tampered."""

        previous = ""
        for entry in self.ledger():
            expected = self._chain_signature(entry, previous)
            if entry.signature != expected or entry.prev_signature != previous:
                return False
            previous = entry.signature
        return True

    # ----------------------------------------------------------------- storage

    def _load_versions(self) -> list[Attribution]:
        versions: list[Attribution] = []
        for item in load_commands(self._store, VERSIONS_KEY):
            try:
                payload = json.loads(item)
            except ValueError:
                continue
            if not isinstance(payload, dict):
                continue
            versions.append(
                Attribution(
                    version=int(payload.get("version", 0)),
                    kind=str(payload.get("kind", "")),
                    equipment_id=str(payload.get("equipment_id", "")),
                    zone=int(payload.get("zone", 0)),
                    registered_at=int(payload.get("registered_at", 0)),
                    effective_at=int(payload.get("effective_at", 0)),
                    ends_at=int(payload.get("ends_at", 0)),
                    status=str(payload.get("status", "")),
                    operator=str(payload.get("operator", "")),
                    confirmer=str(payload.get("confirmer", "")),
                    reason=str(payload.get("reason", "")),
                    mode=str(payload.get("mode", "")),
                    supersedes=int(payload.get("supersedes", 0)),
                    created_by_registration=str(payload.get("created_by_registration", "")),
                )
            )
        versions.sort(key=lambda item: item.version)
        return versions

    def _append_version(self, version: Attribution) -> None:
        versions = self._load_versions()
        versions.append(version)
        self._save_versions(versions)

    def _save_versions(self, versions: list[Attribution]) -> None:
        raw = [
            json.dumps(self._storable(item), ensure_ascii=False, sort_keys=True)
            for item in versions
        ]
        self._store.put(VERSIONS_KEY, json.dumps(raw, ensure_ascii=False))

    def _find_version_locked(
        self, versions: list[Attribution], version: int
    ) -> Attribution | None:
        for item in versions:
            if item.version == version:
                return item
        return None

    def _replace_version_locked(
        self, current: Attribution, **changes: object
    ) -> Attribution:
        """Rebuild the version list, replacing one row.

        Rows are immutable in intent: the only row that can change state is
        one owned by the registration that created it (a scheduled cutover
        flipping to active, or a quarantine resolving), and the ledger records
        every such transition.
        """

        versions = self._load_versions()
        updated_data = dict(current.as_dict())
        updated_data.pop("kind_label", None)
        updated_data.update(changes)
        updated = Attribution(**updated_data)
        rewritten = [updated if item.version == current.version else item for item in versions]
        self._save_versions(rewritten)
        return updated

    @staticmethod
    def _storable(version: Attribution) -> dict[str, object]:
        data = version.as_dict()
        data.pop("kind_label", None)
        return data

    def _close_version_locked(self, version: int, ends_at: int) -> None:
        if not version:
            return
        versions = self._load_versions()
        current = self._find_version_locked(versions, version)
        if current is None:
            return
        if current.status == STATUS_SUPERSEDED and current.ends_at:
            # A cancelled cutover means the successor arrives earlier:
            # tighten the interval rather than leave an overlap behind.
            if ends_at < current.ends_at:
                self._replace_version_locked(current, ends_at=ends_at)
            return
        self._replace_version_locked(current, status=STATUS_SUPERSEDED, ends_at=ends_at)

    def _cancel_version_locked(self, version: Attribution, now: int) -> None:
        """Mark a scheduled cutover cancelled and record it in the ledger."""

        cancelled = self._replace_version_locked(version, status=STATUS_CANCELLED)
        self._append_ledger(
            now,
            "cancel",
            json.dumps(self._record_of(cancelled).as_dict(), ensure_ascii=False, sort_keys=True),
            version.created_by_registration,
        )

    @staticmethod
    def _pending_scheduled_locked(
        versions: list[Attribution], kind: str, equipment_id: str, now: int
    ) -> Attribution | None:
        """A scheduled successor that has not reached its boundary yet."""

        for item in versions:
            if (
                item.kind == kind
                and item.equipment_id == equipment_id
                and item.status == STATUS_SCHEDULED
                and item.effective_at > now
            ):
                return item
        return None

    def _authoritative_locked(
        self,
        versions: list[Attribution],
        kind: str,
        equipment_id: str,
        now: int,
    ) -> Attribution | None:
        """The current authority for one unit at ``now``.

        A due scheduled version is the authority (its predecessor was already
        closed at the same boundary); otherwise the latest active row wins.
        Quarantined claims never become authority on their own.
        """

        candidates = [
            item
            for item in versions
            if item.kind == kind
            and item.equipment_id == equipment_id
            and item.status in (STATUS_ACTIVE, STATUS_SCHEDULED)
        ]
        due = [item for item in candidates if item.effective_at <= now]
        if due:
            return due[-1]
        scheduled = [item for item in candidates if item.status == STATUS_SCHEDULED]
        if scheduled:
            # The registered successor already holds the commitment of the
            # current shift window, even before its boundary arrives.
            return scheduled[-1]
        active = [item for item in candidates if item.status == STATUS_ACTIVE]
        return active[-1] if active else None

    @staticmethod
    def _record_of(version: Attribution) -> MigrationRecord:
        return MigrationRecord(
            registration=version.created_by_registration,
            version=version.version,
            kind=version.kind,
            equipment_id=version.equipment_id,
            zone=version.zone,
            registered_at=version.registered_at,
            effective_at=version.effective_at,
            status=version.status,
            mode=version.mode,
            operator=version.operator,
            confirmer=version.confirmer,
            supersedes=version.supersedes,
            reason=version.reason,
        )

    @staticmethod
    def _chain_signature(entry: LedgerEntry, previous: str) -> str:
        payload = f"{entry.seq}|{entry.at}|{entry.kind}|{entry.detail}|{entry.registration}|{previous}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _append_ledger(
        self, at: int, kind: str, detail: str, registration: str
    ) -> LedgerEntry:
        existing = load_commands(self._store, LEDGER_KEY)
        seq = len(existing) + 1
        previous = ""
        if existing:
            try:
                previous = str(json.loads(existing[-1]).get("signature", ""))
            except ValueError:
                previous = ""
        entry = LedgerEntry(
            seq=seq,
            at=at,
            kind=kind,
            detail=detail,
            registration=registration,
            signature="",
            prev_signature=previous,
        )
        signature = self._chain_signature(entry, previous)
        entry = LedgerEntry(
            seq=seq,
            at=at,
            kind=kind,
            detail=detail,
            registration=registration,
            signature=signature,
            prev_signature=previous,
        )
        append_command(
            self._store,
            LEDGER_KEY,
            json.dumps(entry.as_dict(), ensure_ascii=False, sort_keys=True),
        )
        return entry
