"""Append only persistence for ownership versions, migrations and the trace.

Everything is stored inside the shared key/value document: two JSON lists
(versions and migrations), a monotonic registration counter and an append
only trace list. Records are never physically deleted; a version ends its
life by gaining ``valid_to``/``superseded_at`` markers while its original
payload stays intact.
"""

from __future__ import annotations

import json

from coldstore.store.commands import append_command, load_commands
from coldstore.store.store import Store

from .report import Migration, Version

VERSIONS_KEY = "ownership:versions"
MIGRATIONS_KEY = "ownership:migrations"
SEQ_KEY = "ownership:seq"
TRACE_KEY = "ownership:trace"


def load_versions(store: Store) -> list[Version]:
    """Return every registered version in registration order."""

    return [_version(item) for item in load_commands(store, VERSIONS_KEY) if item]


def append_version(store: Store, version: Version) -> None:
    append_command(store, VERSIONS_KEY, json.dumps(version.as_dict(), ensure_ascii=False))


def save_versions(store: Store, versions: list[Version]) -> None:
    """Rewrite the version list, preserving append order."""

    payload = [json.dumps(version.as_dict(), ensure_ascii=False) for version in versions]
    store.put(VERSIONS_KEY, json.dumps(payload, ensure_ascii=False))


def load_migrations(store: Store) -> list[Migration]:
    """Return every migration in start order."""

    records: list[Migration] = []
    for item in load_commands(store, MIGRATIONS_KEY):
        try:
            payload = json.loads(item)
        except ValueError:
            continue
        if isinstance(payload, dict):
            records.append(_migration(payload))
    return records


def save_migrations(store: Store, migrations: list[Migration]) -> None:
    payload = [json.dumps(item.as_dict(), ensure_ascii=False) for item in migrations]
    store.put(MIGRATIONS_KEY, json.dumps(payload, ensure_ascii=False))


def next_seq(store: Store) -> int:
    """Hand out a monotonic registration sequence number."""

    raw, present = store.get(SEQ_KEY)
    value = 0
    if present:
        try:
            value = int(raw)
        except ValueError:
            value = 0
    value += 1
    store.put(SEQ_KEY, str(value))
    return value


def append_trace(store: Store, entry: dict[str, object]) -> None:
    append_command(store, TRACE_KEY, json.dumps(entry, ensure_ascii=False))


def load_trace(store: Store) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for item in load_commands(store, TRACE_KEY):
        try:
            payload = json.loads(item)
        except ValueError:
            continue
        if isinstance(payload, dict):
            records.append(payload)
    return records


def _version(raw: str) -> Version:
    payload = json.loads(raw)
    return Version(
        version_id=str(payload.get("version_id", "")),
        subject_kind=str(payload.get("subject_kind", "")),
        subject_id=str(payload.get("subject_id", "")),
        owner_room_id=str(payload.get("owner_room_id", "")),
        owner_zone=int(payload.get("owner_zone", 0)),
        valid_from=int(payload.get("valid_from", 0)),
        valid_to=_optional_int(payload.get("valid_to")),
        registered_at=int(payload.get("registered_at", 0)),
        registered_by=str(payload.get("registered_by", "")),
        effective=str(payload.get("effective", "")),
        status=str(payload.get("status", "")),
        supersedes=str(payload.get("supersedes", "")),
        superseded_at=_optional_int(payload.get("superseded_at")),
        superseded_by=str(payload.get("superseded_by", "")),
        migration_id=str(payload.get("migration_id", "")),
        reason=str(payload.get("reason", "")),
    )


def _migration(payload: dict[str, object]) -> Migration:
    return Migration(
        migration_id=str(payload.get("migration_id", "")),
        subject_kind=str(payload.get("subject_kind", "")),
        subject_id=str(payload.get("subject_id", "")),
        old_version_id=str(payload.get("old_version_id", "")),
        new_version_id=str(payload.get("new_version_id", "")),
        status=str(payload.get("status", "")),
        started_at=int(payload.get("started_at", 0)),
        started_by=str(payload.get("started_by", "")),
        dual_from=int(payload.get("dual_from", 0)),
        cutover_at=_optional_int(payload.get("cutover_at")),
        completed_at=_optional_int(payload.get("completed_at")),
        note=str(payload.get("note", "")),
    )


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    return int(value)
