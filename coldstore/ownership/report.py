"""Read models and validation for the versioned ownership registry."""

from __future__ import annotations

from dataclasses import dataclass, field

# Equipment kinds that can be owned by a room/zone partition.
COMPRESSOR = "compressor"
AMMONIA = "ammonia"
FAN = "fan"
KIND_ALL = (COMPRESSOR, AMMONIA, FAN)

# Version lifecycle.
PLANNED = "planned"
ACTIVE = "active"
SUPERSEDED = "superseded"
CONFLICT = "conflict"
ABORTED = "aborted"

# Migration lifecycle.
DRAFT = "draft"
MIGRATING = "migrating"
DUAL_RUN = "dual_run"
COMPLETED = "completed"
ROLLED_BACK = "rolled_back"

# Conflict resolution decisions.
KEEP_NEW = "keep_new"
KEEP_OLD = "keep_old"


@dataclass(frozen=True)
class Version:
    """One immutable ownership fact, valid over a half open business interval.

    ``valid_from``/``valid_to`` carry business time (shift boundaries);
    ``registered_at``/``superseded_at`` carry system (transaction) time. The
    two clocks are independent, so a late correction can be backdated without
    rewriting what the ownership page showed earlier.
    """

    version_id: str
    subject_kind: str
    subject_id: str
    owner_room_id: str
    owner_zone: int
    valid_from: int
    valid_to: int | None
    registered_at: int
    registered_by: str
    effective: str
    status: str
    supersedes: str
    superseded_at: int | None
    superseded_by: str
    migration_id: str
    reason: str

    def as_dict(self) -> dict[str, object]:
        return {
            "version_id": self.version_id,
            "subject_kind": self.subject_kind,
            "subject_id": self.subject_id,
            "owner_room_id": self.owner_room_id,
            "owner_zone": self.owner_zone,
            "valid_from": self.valid_from,
            "valid_to": self.valid_to,
            "registered_at": self.registered_at,
            "registered_by": self.registered_by,
            "effective": self.effective,
            "status": self.status,
            "supersedes": self.supersedes,
            "superseded_at": self.superseded_at,
            "superseded_by": self.superseded_by,
            "migration_id": self.migration_id,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class OwnershipView:
    """Ownership returned for one point in business time."""

    subject_kind: str
    subject_id: str
    owner_room_id: str
    owner_zone: int
    version_id: str
    status: str
    valid_from: int
    valid_to: int | None

    def as_dict(self) -> dict[str, object]:
        return {
            "subject_kind": self.subject_kind,
            "subject_id": self.subject_id,
            "owner_room_id": self.owner_room_id,
            "owner_zone": self.owner_zone,
            "version_id": self.version_id,
            "status": self.status,
            "valid_from": self.valid_from,
            "valid_to": self.valid_to,
        }


@dataclass(frozen=True)
class QueryResult:
    """A point in time ownership answer with its alignment fingerprint."""

    as_of: int
    known_as_of: int
    results: list[OwnershipView]
    version_ids: list[str] = field(default_factory=list)
    fingerprint: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "as_of": self.as_of,
            "known_as_of": self.known_as_of,
            "results": [item.as_dict() for item in self.results],
            "version_ids": self.version_ids,
            "fingerprint": self.fingerprint,
        }


@dataclass(frozen=True)
class Migration:
    """A cross period move during which old and new ownership coexist."""

    migration_id: str
    subject_kind: str
    subject_id: str
    old_version_id: str
    new_version_id: str
    status: str
    started_at: int
    started_by: str
    dual_from: int
    cutover_at: int | None
    completed_at: int | None
    note: str

    def as_dict(self) -> dict[str, object]:
        return {
            "migration_id": self.migration_id,
            "subject_kind": self.subject_kind,
            "subject_id": self.subject_id,
            "old_version_id": self.old_version_id,
            "new_version_id": self.new_version_id,
            "status": self.status,
            "started_at": self.started_at,
            "started_by": self.started_by,
            "dual_from": self.dual_from,
            "cutover_at": self.cutover_at,
            "completed_at": self.completed_at,
            "note": self.note,
        }


def validate_kind(kind: str) -> None:
    """Reject unknown ownership subject kinds."""

    if kind not in KIND_ALL:
        raise ValueError(f"subject kind must be one of {', '.join(KIND_ALL)}")


def validate_owner(room_id: str, zone: int) -> None:
    """Every owned unit must point at a real room/partition and zone."""

    if not room_id:
        raise ValueError("owner room id is required")
    if zone < 0:
        raise ValueError("zone must be non-negative")
    if zone > 100_000:
        raise ValueError("zone exceeds the supported range")


def validate_effective(effective: str) -> None:
    """Only the two documented activation modes are allowed."""

    if effective not in ("next_shift", "emergency"):
        raise ValueError("effective must be next_shift or emergency")
