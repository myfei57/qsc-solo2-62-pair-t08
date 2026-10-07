"""Attribution registry console endpoints."""

from __future__ import annotations

from typing import TYPE_CHECKING

from coldstore.attribution import MODE_NEXT_SHIFT

from .http import Request, RequestError, Response, json_response

if TYPE_CHECKING:  # pragma: no cover - imported only for type checkers
    from .server import Server


def _as_of(request: Request) -> int | None:
    if "as_of" not in request.payload and not request.query.get("as_of"):
        return None
    raw = request.str_field("as_of") or request.query.get("as_of", [""])[0]
    try:
        value = int(raw)
    except ValueError as exc:
        raise RequestError(400, "as_of must be an epoch second") from exc
    if value < 0:
        raise RequestError(400, "as_of must be non-negative")
    return value


def attribution_register(server: "Server", request: Request) -> Response:
    record = server.runtime.attribution.register(
        kind=request.str_field("kind"),
        equipment_id=request.str_field("id"),
        zone=request.int_field("zone"),
        operator=request.str_field("operator"),
        reason=request.str_field("reason"),
        mode=request.str_field("mode", MODE_NEXT_SHIFT),
        confirmer=request.str_field("confirmer"),
        confirm_handover=bool(request.payload.get("confirm_handover", False)),
    )
    return json_response(record.as_dict())


def attribution_point_in_time(server: "Server", request: Request) -> Response:
    report = server.runtime.attribution.point_in_time(_as_of(request))
    return json_response(report.as_dict())


def attribution_lookup(server: "Server", request: Request) -> Response:
    view = server.runtime.attribution.attribution_of(
        request.str_field("kind"),
        request.str_field("id"),
        _as_of(request),
    )
    return json_response({"attribution": view.as_dict() if view else None})


def attribution_zone(server: "Server", request: Request) -> Response:
    zone = request.int_field("zone")
    views = server.runtime.attribution.equipment_in_zone(zone, _as_of(request))
    return json_response({"zone": zone, "equipment": [view.as_dict() for view in views]})


def attribution_versions(server: "Server", request: Request) -> Response:
    chain = server.runtime.attribution.versions_for(
        request.str_field("kind"), request.str_field("id")
    )
    return json_response({"versions": [item.as_dict() for item in chain]})


def attribution_migrations(server: "Server", request: Request) -> Response:
    records = server.runtime.attribution.migrations()
    return json_response({"migrations": [item.as_dict() for item in records]})


def attribution_confirm(server: "Server", request: Request) -> Response:
    record = server.runtime.attribution.confirm_handover(
        request.int_field("version"), request.str_field("confirmer")
    )
    return json_response(record.as_dict())


def attribution_activate(server: "Server", request: Request) -> Response:
    records = server.runtime.attribution.activate_due()
    return json_response({"activated": [item.as_dict() for item in records]})


def attribution_align(server: "Server", request: Request) -> Response:
    check = server.runtime.attribution.align(
        request.int_field("left_as_of"), request.int_field("right_as_of")
    )
    return json_response(check.as_dict())


def attribution_ledger(server: "Server", request: Request) -> Response:
    entries = server.runtime.attribution.ledger()
    return json_response(
        {
            "entries": [entry.as_dict() for entry in entries],
            "count": len(entries),
            "intact": server.runtime.attribution.verify_ledger(),
        }
    )
