"""Read models and validation rules for the attribution registry."""

from __future__ import annotations

from dataclasses import dataclass, field

EQUIPMENT_KINDS = ("comp", "ammonia", "fan")
EQUIPMENT_LABELS = {"comp": "机组", "ammonia": "氨检", "fan": "风机"}

# Migration lifecycle.  ``scheduled`` versions are already registered but do
# not become authoritative until their effective shift; ``quarantined``
# versions overlap another claim and must be confirmed before they can cut
# over; ``superseded`` versions used to be authoritative and stay queryable.
STATUS_ACTIVE = "active"
STATUS_SCHEDULED = "scheduled"
STATUS_QUARANTINED = "quarantined"
STATUS_SUPERSEDED = "superseded"
STATUS_CANCELLED = "cancelled"
STATUSES = (
    STATUS_ACTIVE,
    STATUS_SCHEDULED,
    STATUS_QUARANTINED,
    STATUS_SUPERSEDED,
    STATUS_CANCELLED,
)

# A change waits for the next shift by default.  An emergency change takes
# effect immediately but must carry two distinct confirmers.
MODE_NEXT_SHIFT = "next_shift"
MODE_IMMEDIATE = "immediate"
MODES = (MODE_NEXT_SHIFT, MODE_IMMEDIATE)


def validate_kind(kind: str) -> str:
    """Normalize an equipment kind, rejecting unknown ones."""

    normalized = kind.strip().lower()
    if normalized not in EQUIPMENT_KINDS:
        raise ValueError(f"equipment kind must be one of {', '.join(EQUIPMENT_KINDS)}")
    return normalized


def validate_zone(zone: int) -> int:
    """Attribution zones are positive partition numbers."""

    if not isinstance(zone, int) or isinstance(zone, bool) or zone <= 0:
        raise ValueError("zone must be a positive partition number")
    return zone


def validate_mode(mode: str) -> str:
    normalized = mode.strip().lower()
    if normalized not in MODES:
        raise ValueError(f"mode must be one of {', '.join(MODES)}")
    return normalized


@dataclass(frozen=True)
class Attribution:
    """One versioned claim: one piece of equipment attributed to one zone."""

    version: int
    kind: str
    equipment_id: str
    zone: int
    registered_at: int
    effective_at: int
    ends_at: int
    status: str
    operator: str
    confirmer: str
    reason: str
    mode: str
    supersedes: int
    created_by_registration: str

    def as_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "kind": self.kind,
            "kind_label": EQUIPMENT_LABELS.get(self.kind, self.kind),
            "equipment_id": self.equipment_id,
            "zone": self.zone,
            "registered_at": self.registered_at,
            "effective_at": self.effective_at,
            "ends_at": self.ends_at,
            "status": self.status,
            "operator": self.operator,
            "confirmer": self.confirmer,
            "reason": self.reason,
            "mode": self.mode,
            "supersedes": self.supersedes,
            "created_by_registration": self.created_by_registration,
        }


@dataclass(frozen=True)
class AttributionView:
    """The attribution of one piece of equipment as seen at one instant."""

    kind: str
    equipment_id: str
    zone: int
    version: int
    effective_at: int
    status: str

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "kind_label": EQUIPMENT_LABELS.get(self.kind, self.kind),
            "equipment_id": self.equipment_id,
            "zone": self.zone,
            "version": self.version,
            "effective_at": self.effective_at,
            "status": self.status,
        }


@dataclass(frozen=True)
class PointInTimeReport:
    """The full attribution picture as it was at ``as_of``.

    ``pending`` lists scheduled versions that have not cut over yet, which is
    what the next shift will inherit.  Quarantined overlaps are returned
    separately so a query never silently merges two competing claims.
    """

    as_of: int
    shift: int
    active: list[AttributionView]
    pending: list[AttributionView]
    quarantined: list[AttributionView]
    signature: str

    def as_dict(self) -> dict[str, object]:
        return {
            "as_of": self.as_of,
            "shift": self.shift,
            "active": [item.as_dict() for item in self.active],
            "pending": [item.as_dict() for item in self.pending],
            "quarantined": [item.as_dict() for item in self.quarantined],
            "count": len(self.active),
            "signature": self.signature,
        }


@dataclass(frozen=True)
class MigrationRecord:
    """One registration and the state its competing claim landed in."""

    registration: str
    version: int
    kind: str
    equipment_id: str
    zone: int
    registered_at: int
    effective_at: int
    status: str
    mode: str
    operator: str
    confirmer: str
    supersedes: int
    reason: str

    def as_dict(self) -> dict[str, object]:
        return {
            "registration": self.registration,
            "version": self.version,
            "kind": self.kind,
            "equipment_id": self.equipment_id,
            "zone": self.zone,
            "registered_at": self.registered_at,
            "effective_at": self.effective_at,
            "status": self.status,
            "mode": self.mode,
            "operator": self.operator,
            "confirmer": self.confirmer,
            "supersedes": self.supersedes,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class LedgerEntry:
    """One append only traceability record, hash chained to the one before."""

    seq: int
    at: int
    kind: str
    detail: str
    registration: str
    signature: str
    prev_signature: str

    def as_dict(self) -> dict[str, object]:
        return {
            "seq": self.seq,
            "at": self.at,
            "kind": self.kind,
            "detail": self.detail,
            "registration": self.registration,
            "signature": self.signature,
            "prev_signature": self.prev_signature,
        }


@dataclass(frozen=True)
class ConsistencyCheck:
    """Result of aligning two point in time pictures, usually new vs old."""

    left_as_of: int
    right_as_of: int
    moved: list[dict[str, object]] = field(default_factory=list)
    added: list[dict[str, object]] = field(default_factory=list)
    removed: list[dict[str, object]] = field(default_factory=list)
    aligned: bool = True

    def as_dict(self) -> dict[str, object]:
        return {
            "left_as_of": self.left_as_of,
            "right_as_of": self.right_as_of,
            "moved": self.moved,
            "added": self.added,
            "removed": self.removed,
            "moved_count": len(self.moved),
            "added_count": len(self.added),
            "removed_count": len(self.removed),
            "aligned": self.aligned,
        }
