"""HTTP handlers for the versioned ownership registry."""

from __future__ import annotations

from typing import TYPE_CHECKING

from coldstore.ownership import KEEP_NEW, KEEP_OLD, validate_kind, validate_owner
from coldstore.room import validate_zone

from .http import Request, Response, json_response

if TYPE_CHECKING:  # pragma: no cover - imported only for type checkers
    from .server import Server


def _int_optional(request: Request, name: str) -> int | None:
    raw = request.str_field(name, "")
    if not raw:
        return None
    return int(raw)


def ownership_register(server: "Server", request: Request) -> Response:
    kind = request.str_field("kind")
    subject = request.str_field("subject")
    room_id = request.str_field("room")
    zone = request.int_field("zone")
    validate_kind(kind)
    validate_zone(zone)
    validate_owner(room_id, zone)
    version = server.runtime.ownership.register(
        kind,
        subject,
        room_id,
        zone,
        registered_by=request.str_field("by"),
        effective=request.str_field("effective", "next_shift"),
        valid_from=_int_optional(request, "valid_from"),
        supersedes=request.str_field("supersedes"),
        migration_id=request.str_field("migration_id"),
        reason=request.str_field("reason"),
    )
    return json_response({"version": version.as_dict()})


def ownership_query_subject(server: "Server", request: Request) -> Response:
    kind = request.str_field("kind")
    subject = request.str_field("subject")
    validate_kind(kind)
    result = server.runtime.ownership.query_subject(
        kind,
        subject,
        _int_optional(request, "as_of"),
        known_at=_int_optional(request, "known_at"),
    )
    return json_response(result.as_dict())


def ownership_query_zone(server: "Server", request: Request) -> Response:
    zone = request.int_field("zone")
    validate_zone(zone)
    result = server.runtime.ownership.query_zone(
        zone,
        _int_optional(request, "as_of"),
        known_at=_int_optional(request, "known_at"),
    )
    return json_response(result.as_dict())


def ownership_query_room(server: "Server", request: Request) -> Response:
    room_id = request.str_field("room")
    result = server.runtime.ownership.query_room(
        room_id,
        _int_optional(request, "as_of"),
        known_at=_int_optional(request, "known_at"),
    )
    return json_response(result.as_dict())


def ownership_history(server: "Server", request: Request) -> Response:
    kind = request.str_field("kind")
    subject = request.str_field("subject")
    validate_kind(kind)
    versions = server.runtime.ownership.history(kind, subject)
    return json_response({"versions": [version.as_dict() for version in versions]})


def ownership_conflicts(server: "Server", request: Request) -> Response:
    versions = server.runtime.ownership.conflicts()
    return json_response({"conflicts": [version.as_dict() for version in versions]})


def ownership_resolve(server: "Server", request: Request) -> Response:
    version_id = request.str_field("version_id")
    decision = request.str_field("decision")
    if decision not in (KEEP_NEW, KEEP_OLD):
        raise ValueError("decision must be keep_new or keep_old")
    version = server.runtime.ownership.resolve_conflict(version_id, decision, by=request.str_field("by"))
    return json_response({"version": version.as_dict()})


def ownership_migration_begin(server: "Server", request: Request) -> Response:
    zone = request.int_field("zone")
    validate_zone(zone)
    migration, version = server.runtime.ownership.begin_migration(
        request.str_field("kind"),
        request.str_field("subject"),
        request.str_field("room"),
        zone,
        old_version_id=request.str_field("old_version_id"),
        by=request.str_field("by"),
        note=request.str_field("note"),
    )
    return json_response({"migration": migration.as_dict(), "version": version.as_dict()})


def ownership_migration_dual_run(server: "Server", request: Request) -> Response:
    migration = server.runtime.ownership.start_dual_run(request.str_field("migration_id"))
    return json_response({"migration": migration.as_dict()})


def ownership_migration_cutover(server: "Server", request: Request) -> Response:
    migration = server.runtime.ownership.cutover(
        request.str_field("migration_id"),
        immediate=request.str_field("immediate", "") == "true",
        by=request.str_field("by"),
    )
    return json_response({"migration": migration.as_dict()})


def ownership_migration_complete(server: "Server", request: Request) -> Response:
    migration = server.runtime.ownership.complete(request.str_field("migration_id"))
    return json_response({"migration": migration.as_dict()})


def ownership_migration_rollback(server: "Server", request: Request) -> Response:
    migration = server.runtime.ownership.rollback(
        request.str_field("migration_id"), by=request.str_field("by")
    )
    return json_response({"migration": migration.as_dict()})


def ownership_migration_view(server: "Server", request: Request) -> Response:
    view = server.runtime.ownership.dual_view(
        request.str_field("migration_id"), _int_optional(request, "as_of")
    )
    return json_response(view)


def ownership_activate(server: "Server", request: Request) -> Response:
    activated = server.runtime.ownership.activate_due()
    return json_response({"activated": activated})


def ownership_trace(server: "Server", request: Request) -> Response:
    return json_response({"trace": server.runtime.ownership.trace()})
