"""Versioned attribution registry for warehouse zones.

Compressors (机组), ammonia detectors (氨检) and fans (风机) are attributed to
warehouse partition zones by versioned registrations.  Changes register at
once but take effect at the next shift boundary by default, earlier claims
win conflicts, and every registration, handover, activation, query and
consistency check lands in a hash chained traceability ledger.
"""

from .registry import (
    LEDGER_KEY,
    SHIFT_SECONDS,
    VERSIONS_KEY,
    AttributionRegistry,
    shift_boundary_after,
    shift_of,
)
from .report import (
    EQUIPMENT_KINDS,
    MODE_IMMEDIATE,
    MODE_NEXT_SHIFT,
    STATUS_ACTIVE,
    STATUS_CANCELLED,
    STATUS_QUARANTINED,
    STATUS_SCHEDULED,
    STATUS_SUPERSEDED,
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

__all__ = [
    "LEDGER_KEY",
    "SHIFT_SECONDS",
    "VERSIONS_KEY",
    "AttributionRegistry",
    "shift_boundary_after",
    "shift_of",
    "EQUIPMENT_KINDS",
    "MODE_IMMEDIATE",
    "MODE_NEXT_SHIFT",
    "STATUS_ACTIVE",
    "STATUS_CANCELLED",
    "STATUS_QUARANTINED",
    "STATUS_SCHEDULED",
    "STATUS_SUPERSEDED",
    "Attribution",
    "AttributionView",
    "ConsistencyCheck",
    "LedgerEntry",
    "MigrationRecord",
    "PointInTimeReport",
    "validate_kind",
    "validate_mode",
    "validate_zone",
]
