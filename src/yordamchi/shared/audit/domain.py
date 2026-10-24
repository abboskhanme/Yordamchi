"""Audit rules, pure (SPEC §4.2 `audit_events`, NFR-SEC-10): who acts, what kinds exist, what `data_json` may hold.

An event says *that* something happened — ids, codes, sha256 digests, a scrubbed command — never *what was said*:
no message text, transcript, prompt or model output. Every string in `data` passes the scrubber the service hands in.
Rows are append-only and kept `AUDIT_KEEP`; nothing updates or deletes one before that.
"""

from collections.abc import Callable
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Final

from yordamchi.core import json_value
from yordamchi.core.json_value import JsonObject

AUDIT_KEEP: Final = timedelta(days=90)  # NFR-SEC-10


class AuditActor(StrEnum):
    OWNER = "owner"
    EXECUTOR = "executor"
    SYSTEM = "system"
    SUPERVISOR = "supervisor"


class AuditKind(StrEnum):
    EXECUTOR_INPUT = "executor_input"
    GATE_VERDICT = "gate_verdict"
    DECISION_ASKED = "decision_asked"
    DECISION_RESOLVED = "decision_resolved"
    GRANT_CONSUMED = "grant_consumed"
    SETTING_CHANGED = "setting_changed"
    INTEGRATION_CHANGED = "integration_changed"
    SECRET_ADDED = "secret_added"  # noqa: S105  # an audit kind, not a password
    SECRET_REMOVED = "secret_removed"  # noqa: S105  # an audit kind, not a password
    SESSION_OPENED = "session_opened"
    SESSION_CLOSED = "session_closed"
    SELF_UPDATE = "self_update"
    ACCESS_DROPPED = "access_dropped"


def clean_data(data: JsonObject, scrub: Callable[[str], str]) -> JsonObject:
    """The `data_json` of one event: a JSON object whose strings, keys included, have all been scrubbed."""
    return {scrub(key): json_value.map_strings(item, scrub) for key, item in data.items()}


def retention_cutoff(now: datetime) -> datetime:
    """Events strictly older than this may be deleted."""
    return now - AUDIT_KEEP
