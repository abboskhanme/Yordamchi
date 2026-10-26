"""The schema decisions the migration tests hold the code to, each with where it was decided.

- `RETENTION` / `KEPT`: every table either has the `apply_retention` that applies its SPEC §4.2 period (TASKS R4), or a
  documented reason to keep its rows. A new table must land in one of them.
- `FOREIGN_KEYS` / `PENDING_FOREIGN_KEYS`: the ON DELETE action of every foreign key into a table that retention
  prunes rows from. A parent deleted after 30 days must not block on, or silently take, rows that live 90–400 days.
"""

from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True, slots=True)
class Retention:
    service: str  # the module whose `apply_retention(deps)` applies it
    rule: str  # SPEC §4.2
    deletes_rows: bool = True  # False: it only clears columns (the row stays)


INBOX: Final = "yordamchi.shared.inbox.service"
BURST: Final = "yordamchi.shared.burst.service"
VOICE: Final = "yordamchi.shared.voice.service"
OUTBOX: Final = "yordamchi.shared.outbox.service"
VAULT: Final = "yordamchi.shared.vault.service"
INTEGRATIONS: Final = "yordamchi.shared.integrations.service"
AUDIT: Final = "yordamchi.shared.audit.service"
COST: Final = "yordamchi.shared.cost.service"
HEALTH: Final = "yordamchi.shared.health.service"
TURNS: Final = "yordamchi.dev.turns.service"
QUESTIONS: Final = "yordamchi.dev.questions.service"
GATE: Final = "yordamchi.dev.gate.service"
ROUTING: Final = "yordamchi.dev.routing.service"
SERVERS: Final = "yordamchi.dev.servers.service"
NEWPROJECT: Final = "yordamchi.dev.newproject.service"
QA: Final = "yordamchi.personal.qa.service"

RETENTION: Final[dict[str, Retention]] = {
    "inbound_updates": Retention(INBOX, "payload nulled 7 d; row 30 d; a received row is never touched"),
    "bursts": Retention(BURST, "30 d after it closed; an assembling or closed burst is never touched"),
    "transcripts": Retention(VOICE, "text nulled 7 d; row 30 d; a pending row is never touched"),
    "outbox_messages": Retention(OUTBOX, "finished 30 d; pending or sending never touched"),
    "message_links": Retention(OUTBOX, "90 d"),
    "vault_secrets": Retention(VAULT, "a removed secret's ciphertext wiped 30 d after removal", deletes_rows=False),
    "integrations": Retention(INTEGRATIONS, "removed 30 d"),
    "oauth_clients": Retention(INTEGRATIONS, "with its integration (deleted first)"),
    "audit_events": Retention(AUDIT, "90 d; no UPDATE or DELETE before"),
    "usage_records": Retention(COST, "400 d"),
    "cap_notices": Retention(COST, "400 d"),
    "health_events": Retention(HEALTH, "90 d"),
    "turns": Retention(TURNS, "400 d once finished"),
    "deliveries": Retention(TURNS, "text nulled 30 d; the row goes with its turn (ON DELETE CASCADE)"),
    "turn_progress": Retention(TURNS, "7 d; the last 50 per turn"),
    "questions": Retention(QUESTIONS, "finished 90 d; an open question is never touched"),
    "decisions": Retention(GATE, "finished 90 d; pending or approved never touched"),
    "route_decisions": Retention(ROUTING, "90 d"),
    "clarifications": Retention(ROUTING, "finished 30 d; an open one is never touched"),
    "server_checks": Retention(SERVERS, "30 d"),
    "project_drafts": Retention(NEWPROJECT, "finished 400 d after it closed; an open draft is never touched"),
    "assistant_sessions": Retention(QA, "closed: texts nulled 90 d; the row once no answer points at it"),
    "answers": Retention(QA, "text_full nulled 30 d; row 400 d"),
}

KEPT: Final[dict[str, str]] = {
    "projects": "SPEC §4.2 forever: the registry; a vanished folder keeps its row with missing_since",
    "agent_sessions": "SPEC §4.2 forever: a closed or lost session still resumes by its session_id",
    "setting_versions": "SPEC §4.2 forever, append-only: the settings history that revert reads",
    "self_updates": "SPEC §4.2 forever: the history of what Yordamchi did to itself (UC-19)",
    "digests": "SPEC §4.2 one row per project, rewritten whole by every refresh (a project is kept forever)",
    "poll_offsets": "SPEC §4.2 '—': one row per bot, the next getUpdates offset",
    "kv_state": "SPEC §4.2 '—': a few keys per bot (pinned message id, CLI version, speech status), overwritten",
    "project_topics": "SPEC §4.2 '—' (0002): one row per project with a forum topic, rewritten when it is opened anew",
    "topic_chats": "SPEC §4.2 '—' (0002): one row per chat: is it a forum, and Telegram's latest refusal there",
}

# Retention that exists but that no bot runs yet, and the task that wires it. The test fails once it is wired, so the
# line goes away with the fix.
NOT_SCHEDULED_YET: Final[dict[str, str]] = {
    INTEGRATIONS: "P2 (T50) feature, merged early; T58 (P2 wiring) must add it to bots/dev/runtime.py DevApp.retain",
    NEWPROJECT: "P2 (T53) feature, merged early; T58 (P2 wiring) must add it to bots/dev/runtime.py DevApp.retain",
    QA: "personal bot only; T77 (bots/personal wiring) must give the personal bot its daily retention job",
}

# (child table, column) → (parent table, ON DELETE). The parents are pruned by retention; rows that point at them live
# as long or longer, so the decision is SET NULL (keep the child) or CASCADE (the child is part of the parent).
FOREIGN_KEYS: Final[dict[tuple[str, str], tuple[str, str]]] = {
    # docs/ASSUMPTIONS.md 2026-10-24 (T16): burst retention would otherwise scan or fail.
    ("inbound_updates", "burst_id"): ("bursts", "SET NULL"),
    # docs/PENDING_WIRING.md From T16: FKs to bursts.id must be SET NULL; bursts go after 30 d, these rows 90–400 d.
    ("turns", "burst_id"): ("bursts", "SET NULL"),
    ("deliveries", "burst_id"): ("bursts", "SET NULL"),
    ("route_decisions", "burst_id"): ("bursts", "SET NULL"),
    ("clarifications", "burst_id"): ("bursts", "SET NULL"),
    # dev/turns: a finished turn goes after 400 d "with its deliveries and steps".
    ("deliveries", "turn_id"): ("turns", "CASCADE"),
    ("turn_progress", "turn_id"): ("turns", "CASCADE"),
    # dev/questions: a turn is deleted 400 d after it ended, long after its questions (90 d).
    ("questions", "turn_id"): ("turns", "CASCADE"),
    # dev/newproject: a draft lives 400 d after it closed; its turns go 400 d after they were queued.
    ("project_drafts", "plan_turn_id"): ("turns", "SET NULL"),
    ("project_drafts", "build_turn_id"): ("turns", "SET NULL"),
}

# Decided, not in the schema yet: docs/PENDING_WIRING.md From T15 (outbox), From T24 (turns), From T29 (questions).
# Today these are plain integer columns. They must be added before T43 freezes `0001`: afterwards, adding a foreign
# key to an existing SQLite table is a table rebuild, which no expand-only revision may do.
PENDING_FOREIGN_KEYS: Final[dict[tuple[str, str], tuple[str, str]]] = {
    ("outbox_messages", "turn_id"): ("turns", "SET NULL"),
    ("message_links", "turn_id"): ("turns", "SET NULL"),
    ("message_links", "question_id"): ("questions", "SET NULL"),
}
WATCHED_PARENTS: Final = frozenset({"bursts", "turns", "questions"})

# A foreign key into a pruned table without an ON DELETE action, made safe by the parent's retention: it never deletes
# a parent row that a child still points at.
GUARDED_BY_RETENTION: Final[dict[tuple[str, str], str]] = {
    ("oauth_clients", "integration_id"): "integrations.store.delete_removed_before deletes the sealed grant first",
    ("answers", "assistant_session_id"): "qa.store.delete_sessions_closed_before keeps a session an answer points at",
}
