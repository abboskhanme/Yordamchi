"""The previous-release DB for the P2 DoD (TASKS migration plan, T40/T60; CONVENTIONS §8): `fixtures/p1_release.db`.

What it is: a `dev.db` that Alembic migrated to `RELEASE_REVISION`, then filled the way the dev bot fills it, through
`tests/factories.py` and the feature stores. Every table holds rows, and every column holds a value in at least one
row. Wherever a table has a lifecycle there are both finished rows and rows that are still in flight (an open question,
a pending outbox message, an assembling burst…), so retention and migrations can be checked against both. Edge values
are on purpose: Telegram's negative supergroup ids, a micro-USD sum above 2**53 (a float would round it), Uzbek and
Cyrillic text with ʻ ’ «», empty strings, NULLs, microsecond timestamps, booleans both ways, blobs.

The ciphertexts are sealed with `FIXTURE_KEY`, a public test key: nothing in the file is a real secret, and no
plaintext key is stored (test_previous_release checks the bytes).

Regenerate, from the repo root, whenever `0001` changes (until T43 freezes it):

    uv run --locked python -m tests.migrations.p1_release

Once the chain has a revision after `RELEASE_REVISION` (P2's first), the file is the real previous release and the
builder refuses to run (`FixtureFrozen`): from then on it is only ever upgraded, never rebuilt.

A new table in `0001` needs a seeder here (`_seed_*`, called from `seed`) and its line in `inventory.RETENTION` or
`inventory.KEPT` (test_retention fails without one).
"""

import asyncio
import hashlib
import shutil
import sys
import tempfile
from contextlib import closing
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Final

from sqlalchemy import select

from tests.factories import (
    DEV_GROUP_ID,
    FAKE_KEYS,
    INTEGRATION_TOKEN,
    OWNER_ID,
    PERSONAL_GROUP_ID,
    Fakes,
    add_health_event,
    add_setting_row,
    add_usage,
    agent_question,
    make_decision,
    make_dev_deps,
    make_part,
    make_paths,
    make_project_record,
    make_question,
    make_turn_usage,
    make_usage,
    store_secret,
)
from tests.fakes.clock import DEFAULT_START, FakeClock
from tests.fakes.keystore import InMemoryKeystore
from tests.migrations.harness import P1_RELEASE, connect, read_tables, revisions_after, upgrade
from yordamchi.core.json_value import JsonObject, JsonValue
from yordamchi.core.paths import BotName
from yordamchi.dev.deps import DevDeps
from yordamchi.dev.digest import domain as digest_domain
from yordamchi.dev.digest import store as digest_store
from yordamchi.dev.gate import store as gate_store
from yordamchi.dev.newproject import store as newproject_store
from yordamchi.dev.newproject.domain import DraftPart, parts_json
from yordamchi.dev.projects import store as projects_store
from yordamchi.dev.projects.domain import DeployBranches, DescriptionSource
from yordamchi.dev.questions import store as questions_store
from yordamchi.dev.questions.domain import QuestionStatus
from yordamchi.dev.routing import store as routing_store
from yordamchi.dev.selfupdate import store as selfupdate_store
from yordamchi.dev.servers import store as servers_store
from yordamchi.dev.turns import domain as turns_domain
from yordamchi.dev.turns import store as turns_store
from yordamchi.dev.turns.domain import TurnStatus
from yordamchi.persistence.engine import open_database
from yordamchi.persistence.uow import Database
from yordamchi.personal.qa import store as qa_store
from yordamchi.shared.audit import service as audit_service
from yordamchi.shared.audit.domain import AuditActor, AuditKind
from yordamchi.shared.burst import domain as burst_domain
from yordamchi.shared.burst import store as burst_store
from yordamchi.shared.burst.domain import BurstStatus, CloseReason
from yordamchi.shared.cost import store as cost_store
from yordamchi.shared.cost.domain import Provider
from yordamchi.shared.health import store as health_store
from yordamchi.shared.inbox import domain as inbox_domain
from yordamchi.shared.inbox import store as inbox_store
from yordamchi.shared.inbox.domain import Attachment, Content, UpdateKind, UpdateStatus
from yordamchi.shared.integrations import store as integrations_store
from yordamchi.shared.outbox import domain as outbox_domain
from yordamchi.shared.outbox import store as outbox_store
from yordamchi.shared.outbox.domain import Op, Payload
from yordamchi.shared.settings.models import SettingVersion
from yordamchi.shared.vault import store as vault_store
from yordamchi.shared.vault.models import VaultSecret
from yordamchi.shared.voice import store as voice_store

RELEASE_REVISION: Final = "0001"
RELEASE_AT: Final = DEFAULT_START  # the previous release ran up to this moment (2026-10-23 09:00 UTC)
FIXTURE_KEY: Final = hashlib.sha256(b"yordamchi tests/migrations p1_release: a public test key").digest()
# Sealed with a random nonce on every build, so these differ between two otherwise equal builds.
RANDOM_COLUMNS: Final = frozenset(
    {
        ("vault_secrets", "ciphertext"),
        ("vault_secrets", "nonce"),
        ("oauth_clients", "ciphertext"),
        ("oauth_clients", "nonce"),
    }
)
BIG_MICROS: Final = 2**53 + 1  # 9 007 199 254 740 993: a float cannot hold it, so a REAL column would show
CHAT: Final = DEV_GROUP_ID
UZ_TEXT: Final = "Oʻzbekcha: «Login» sahifasi tuzatildi ✓ — testlar o’tdi"
RU_TEXT: Final = "почему сервер упал? — проверь логи"


class FixtureFrozen(RuntimeError):
    """The chain already has revisions after the release: the fixture is the real previous release now."""


def at(days: float = 0, *, seconds: float = 0, micros: int = 0) -> datetime:
    """`RELEASE_AT` moved by `days` and `seconds` (negative: earlier)."""
    return RELEASE_AT + timedelta(days=days, seconds=seconds, microseconds=micros)


@dataclass
class Ids:
    """Row ids the later seeders point at."""

    bursts: dict[str, int] = field(default_factory=dict)
    sessions: dict[str, int] = field(default_factory=dict)
    turns: dict[str, int] = field(default_factory=dict)
    questions: dict[str, int] = field(default_factory=dict)
    decisions: dict[str, int] = field(default_factory=dict)
    routes: dict[str, int] = field(default_factory=dict)
    outbox: dict[str, int] = field(default_factory=dict)
    secrets: dict[str, int] = field(default_factory=dict)


# --- projects -------------------------------------------------------------------------------------------------------
async def _seed_projects(db: Database) -> None:
    deploy = DeployBranches(branches=("main",), workflows=("deploy.yml", "tests.yml"))
    records = (
        replace(
            make_project_record("BonviHR", at=at(-30), description="Kadrlar tizimi: xodimlar, taʼtillar va oyliklar"),
            description_source=DescriptionSource.README,
            git_remote_url="https://github.com/abboskhanme/BonviHR.git",
            deploy=deploy,
            deploy_checked_sha="9f2c" * 16,
            last_seen_at=at(seconds=-60),
            last_activity_at=at(seconds=-3_600, micros=123_456),
        ),
        replace(
            make_project_record("Yordamchi", at=at(-20), description="Ikki Telegram bot: dev va shaxsiy yordamchi"),
            description_source=DescriptionSource.CLAUDE_MD,
            git_remote_url="git@github.com:abboskhanme/yordamchi.git",
            last_seen_at=at(seconds=-60),
        ),
        make_project_record("HRMentor-wt-agent-api", at=at(-10), is_worktree=True),  # description "" (none)
        make_project_record(
            "BonviCall Clone Archive", at=at(-40), is_archive_name=True, missing_since=at(-2, micros=500_001)
        ),
        replace(  # made by `dev/newproject` from the owner's "yangi loyiha" draft
            make_project_record("BonviKassa", at=at(-3), description="Kassa: chek, smena va kunlik hisobot"),
            description_source=DescriptionSource.SPEC,
            git_remote_url="https://github.com/abboskhanme/BonviKassa.git",
        ),
    )
    async with db.uow() as s:
        for record in records:
            await projects_store.set_project(s, record)


# --- turns: agent sessions ------------------------------------------------------------------------------------------
async def _seed_agent_sessions(db: Database, ids: Ids) -> None:
    async with db.uow() as s:
        live = await turns_store.add_agent_session(
            s,
            project_id="BonviHR",
            executor="claude",
            kind="coding",
            session_id="5b0d8c1e-2f6a-4c3b-9e7d-1a2b3c4d5e6f",
            status="live",
            policy="FULL",
            model="opus",
            at=at(-2),
        )
        await turns_store.set_agent_session(s, live.id, prompt_version_seen=7, last_active_at=at(seconds=-600))
        closed = await turns_store.add_agent_session(
            s,
            project_id="BonviHR",
            executor="claude",
            kind="investigation",
            session_id="0f9e8d7c-6b5a-4f3e-8d2c-1b0a9f8e7d6c",
            status="closed",
            policy="READ_ONLY",
            model="opus",
            at=at(-5),
        )
        await turns_store.set_agent_session(
            s, closed.id, is_current=False, mac_attached_at=at(-5, seconds=60), closed_at=at(-4), prompt_version_seen=5
        )
        ids.sessions.update(live=live.id, closed=closed.id)


# --- burst ----------------------------------------------------------------------------------------------------------
async def _seed_bursts(db: Database, ids: Ids) -> None:
    start = at(-2)
    routed_parts = (
        make_part(1001, text="BonviHR'da login sahifasini tuzat", start=start),
        make_part(1002, at_s=1.5, kind=UpdateKind.VOICE, duration_s=12, start=start),
    )
    async with db.uow() as s:
        routed = await burst_store.add_burst(
            s,
            bot=BotName.DEV.value,
            chat_id=CHAT,
            status=BurstStatus.CLOSED.value,
            opened_at=start,
            last_part_at=start + timedelta(seconds=1.5),
            closes_at=start + timedelta(seconds=3.5),
            closed_at=start + timedelta(seconds=3.5),
            close_reason=CloseReason.QUIET.value,
            input_json=burst_domain.parts_json(routed_parts, str),
        )
        await burst_store.set_burst_status(
            s, bot=BotName.DEV.value, burst_id=routed.id, from_status="closed", to_status="routed", route_id=None
        )
        cancelled = await burst_store.add_burst(
            s,
            bot=BotName.DEV.value,
            chat_id=CHAT,
            status=BurstStatus.CANCELLED.value,
            opened_at=at(-1),
            last_part_at=at(-1),
            closes_at=at(-1),
            closed_at=at(-1),
            close_reason=CloseReason.INTERRUPT.value,
            input_json=burst_domain.parts_json((make_part(1003, text="toʻxta", start=at(-1)),), str),
        )
        closed = await burst_store.add_burst(
            s,
            bot=BotName.DEV.value,
            chat_id=CHAT,
            status=BurstStatus.CLOSED.value,
            opened_at=at(seconds=-8),
            last_part_at=at(seconds=-6),
            closes_at=at(seconds=-4),
            closed_at=at(seconds=-4),
            close_reason=CloseReason.QUIET.value,
            input_json=burst_domain.parts_json((make_part(1004, text=RU_TEXT, start=at(seconds=-6)),), str),
        )
        assembling = await burst_store.add_burst(
            s,
            bot=BotName.DEV.value,
            chat_id=CHAT,
            status=BurstStatus.OPEN.value,
            opened_at=at(seconds=-1),
            last_part_at=at(seconds=-1),
            closes_at=at(seconds=9),
            input_json=burst_domain.parts_json((make_part(1005, start=at(seconds=-1)),), str),  # a bare photo
        )
        ids.bursts.update(routed=routed.id, cancelled=cancelled.id, closed=closed.id, assembling=assembling.id)


# --- inbox ----------------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class _Update:
    update_id: int
    kind: UpdateKind
    status: UpdateStatus
    received_at: datetime
    content: Content | None
    burst: str | None = None
    message_id: int | None = None
    media_group_id: str | None = None
    reply_to_message_id: int | None = None
    synthetic: bool = False


async def _seed_inbound_updates(db: Database, ids: Ids) -> None:
    voice = Attachment(file_id="AwACAgIAAxkBAAIBZ2", mime="audio/ogg", size_bytes=48_213, duration_s=12)
    photo = Attachment(file_id="AgACAgIAAxkBAAIBaB", mime="image/jpeg", size_bytes=2_097_152, file_name="xato.jpg")
    updates = (
        _Update(
            900_000_101,
            UpdateKind.TEXT,
            UpdateStatus.ATTACHED,
            at(-2),
            Content(text="BonviHR'da login sahifasini tuzat"),
            burst="routed",
            message_id=5101,
            reply_to_message_id=4990,
        ),
        _Update(
            900_000_102,
            UpdateKind.VOICE,
            UpdateStatus.ATTACHED,
            at(-2, seconds=1.5),
            Content(attachment=voice),
            burst="routed",
            message_id=5102,
        ),
        _Update(
            900_000_103,
            UpdateKind.PHOTO,
            UpdateStatus.ATTACHED,
            at(seconds=-6),
            Content(caption=UZ_TEXT, attachment=photo),
            burst="closed",
            message_id=5103,
            media_group_id="13579246801357924",
        ),
        _Update(900_000_104, UpdateKind.EDITED, UpdateStatus.IGNORED, at(-1), Content(text=""), message_id=5100),
        _Update(900_000_105, UpdateKind.OTHER, UpdateStatus.FAILED, at(-1, seconds=5), None, synthetic=True),
        _Update(
            900_000_106,
            UpdateKind.PHOTO,
            UpdateStatus.RECEIVED,
            at(seconds=-1),
            Content(attachment=photo),
            message_id=5106,
        ),
    )
    async with db.uow() as s:
        for update in updates:
            row_id = await inbox_store.add_inbound_update(
                s,
                bot=BotName.DEV.value,
                update_id=update.update_id,
                chat_id=CHAT,
                user_id=None if update.synthetic else OWNER_ID,
                message_id=update.message_id,
                kind=update.kind.value,
                media_group_id=update.media_group_id,
                reply_to_message_id=update.reply_to_message_id,
                synthetic=update.synthetic,
                status=UpdateStatus.RECEIVED.value,
                received_at=update.received_at,
            )
            assert row_id is not None
            if update.content is not None:
                payload = inbox_domain.payload_json(update.content, str)
                await inbox_store.set_update_payload(s, row_id, payload=payload, status=UpdateStatus.RECEIVED.value)
            if update.status is not UpdateStatus.RECEIVED:
                await inbox_store.set_update_status(
                    s,
                    bot=BotName.DEV.value,
                    update_ids=[update.update_id],
                    from_status=UpdateStatus.RECEIVED.value,
                    to_status=update.status.value,
                    burst_id=ids.bursts[update.burst] if update.burst is not None else None,
                )
        await inbox_store.set_poll_offset(s, bot=BotName.DEV.value, next_offset=900_000_107, at=at(seconds=-1))


# --- voice ----------------------------------------------------------------------------------------------------------
async def _seed_transcripts(db: Database) -> None:
    async with db.uow() as s:
        assert await voice_store.add_transcript(
            s, bot="dev", update_id=900_000_102, status="pending", at=at(-2, seconds=2)
        )
        await voice_store.set_transcript_result(
            s,
            bot="dev",
            update_id=900_000_102,
            status="done",
            text="va testlarni ham ishga tushir, iltimos",
            provider="google",
            provider_path="interactions",
            model="gemini-3.5-transcribe",
            audio_ms=12_000,
            latency_ms=1_840,
            error_code=None,
            at=at(-2, seconds=4),
        )
        assert await voice_store.add_transcript(s, bot="dev", update_id=900_000_090, status="pending", at=at(-3))
        await voice_store.set_transcript_result(
            s,
            bot="dev",
            update_id=900_000_090,
            status="failed",
            text=None,
            provider="openrouter",
            provider_path="chat_completions",
            model="google/gemini-3.5-transcribe",
            audio_ms=None,
            latency_ms=8_000,
            error_code="quota_exhausted",
            at=at(-3, seconds=8),
        )
        assert await voice_store.add_transcript(s, bot="dev", update_id=900_000_107, status="pending", at=at())


# --- routing --------------------------------------------------------------------------------------------------------
async def _seed_routing(db: Database, ids: Ids) -> None:
    async with db.uow() as s:
        routed = await routing_store.add_route_decision(
            s,
            burst_id=ids.bursts["routed"],
            at=at(-2, seconds=4),
            method="llm",
            intent="instruct_project",
            project_id="BonviHR",
            candidates=[
                {"project_id": "BonviHR", "confidence_milli": 930},
                {"project_id": "HRMentor-wt-agent-api", "confidence_milli": 120},
            ],
            confidence_milli=930,
            asked=False,
            model="haiku",
            latency_ms=1_360,
        )
        await routing_store.set_route_corrected(s, routed.id, corrected_to="Yordamchi")
        asked = await routing_store.add_route_decision(
            s,
            burst_id=ids.bursts["closed"],
            at=at(seconds=-3),
            method="context",
            intent="report_project",
            project_id=None,  # the question is still open
            candidates=[],
            confidence_milli=0,
            asked=True,
            model=None,
            latency_ms=0,
        )
        resolved = await routing_store.add_clarification(
            s,
            burst_id=ids.bursts["routed"],
            kind="which_project",
            candidates=["BonviHR", "HRMentor-wt-agent-api"],
            status="open",
            asked_at=at(-2, seconds=5),
        )
        await routing_store.set_clarification_message(s, resolved.id, message_id=5200)
        await routing_store.set_clarification_status(
            s, resolved.id, from_status="open", to_status="resolved", at=at(-2, seconds=40)
        )
        await routing_store.add_clarification(
            s,
            burst_id=ids.bursts["closed"],
            kind="which_running",
            candidates=["BonviHR", "Yordamchi"],
            status="open",
            asked_at=at(seconds=-3),
        )
        await burst_store.set_burst_status(
            s, bot="dev", burst_id=ids.bursts["routed"], from_status="routed", to_status="routed", route_id=routed.id
        )
        ids.routes.update(routed=routed.id, asked=asked.id)


# --- turns ----------------------------------------------------------------------------------------------------------
_MEDIA: Final[JsonValue] = {"images": [], "message_ids": [5101, 5102], "media_group_ids": [], "last_part_at": None}


async def _seed_turns(db: Database, ids: Ids) -> None:
    note = turns_domain.HandoffNote(
        project_id="BonviHR",
        turn_id=0,
        at=at(-2, seconds=900),
        block="Login tuzatildi. Keyingisi: parolni tiklash sahifasi.",
        files=("src/auth/login.py",),
        commits=("a1b2c3d",),
        progress=("Edit src/auth/login.py",),
    )
    async with db.uow() as s:
        done = await turns_store.add_turn(
            s,
            project_id="BonviHR",
            kind="instruct",
            status="queued",
            title="BonviHR'da login sahifasini tuzat",
            burst_id=ids.bursts["routed"],
            at=at(-2, seconds=5),
        )
        await turns_store.set_turn(
            s,
            done.id,
            agent_session_id=ids.sessions["live"],
            status=TurnStatus.DONE.value,
            started_at=at(-2, seconds=6),
            ended_at=at(-2, seconds=906),
            start_head="0" * 40,
            start_dirty=True,
            start_files_json=turns_domain.start_files_json(turns_domain.StartFiles(dirty=("README.md",))),
            end_head="a1b2c3d4" * 5,
            natija=UZ_TEXT,
            tests_status="passed",
            commits_json=["a1b2c3d4" * 5],
            pushed=True,
            changed_files_json=["src/auth/login.py", "tests/test_login.py"],
            steps=42,
            cost_usd_micros=BIG_MICROS,
            duration_ms=900_000,
            progress_message_id=5150,
            handoff_json=turns_domain.handoff_json(replace(note, turn_id=done.id)),
            revert_status="declined",
        )
        failed = await turns_store.add_turn(
            s, project_id="Yordamchi", kind="investigate", status="queued", title=RU_TEXT, burst_id=None, at=at(-1)
        )
        await turns_store.set_turn(
            s,
            failed.id,
            status=TurnStatus.FAILED.value,
            started_at=at(-1, seconds=1),
            ended_at=at(-1, seconds=181),
            limit_resets_at=at(-1, seconds=3_600),
            waiting_since=at(-1, seconds=60),
            error_code="usage_limit",
            steps=3,
            cost_usd_micros=0,
            pushed=False,
            start_dirty=False,
            tests_status="not_run",
        )
        waiting = await turns_store.add_turn(
            s,
            project_id="BonviHR",
            kind="instruct",
            status="queued",
            title="Parolni tiklash sahifasini qoʻsh",
            burst_id=None,
            at=at(seconds=-900),
        )
        await turns_store.set_turn(
            s,
            waiting.id,
            agent_session_id=ids.sessions["live"],
            status=TurnStatus.WAITING_QUESTION.value,
            started_at=at(seconds=-890),
            waiting_since=at(seconds=-300),
            steps=7,
            cost_usd_micros=125_000,
        )
        plan = await turns_store.add_turn(
            s,
            project_id="BonviKassa",
            kind="plan",
            status="queued",
            title="BonviKassa: reja",
            burst_id=None,
            at=at(-3, seconds=120),
        )
        await turns_store.set_turn(
            s,
            plan.id,
            status=TurnStatus.DONE.value,
            started_at=at(-3, seconds=121),
            ended_at=at(-3, seconds=721),
            natija="docs/SPEC.md va docs/TASKS.md tayyor",
            steps=18,
            cost_usd_micros=310_000,
        )
        queued = await turns_store.add_turn(  # the draft's build, queued behind the Mac's memory pressure
            s,
            project_id="BonviKassa",
            kind="build",
            status="queued",
            title="BonviKassa: birinchi versiya",
            burst_id=ids.bursts["routed"],
            at=at(seconds=-120),
        )
        await turns_store.set_turn(s, queued.id, queue_reason="pressure")
        ids.turns.update(done=done.id, failed=failed.id, waiting=waiting.id, plan=plan.id, queued=queued.id)

        deliveries = (  # turn, mode, source, text, status, burst, written (days, seconds from the release)
            ("done", "new_turn", "owner", "BonviHR'da login sahifasini tuzat", "delivered", "routed", (-2, 5)),
            ("done", "nudge", "system", "Natijani qisqa yoz", "delivered", None, (-2, 600)),
            ("failed", "new_turn", "owner", RU_TEXT, "failed", None, (-1, 1)),
            ("waiting", "new_turn", "owner", "Parolni tiklash sahifasini qoʻsh", "delivered", None, (0, -890)),
            ("plan", "new_turn", "system", "Reja tuz: kassa, smena, kunlik hisobot", "delivered", None, (-3, 121)),
            ("queued", "new_turn", "system", "Rejani bajar", "pending", "routed", (0, -120)),
        )
        for turn, mode, source, text, status, burst, (day, second) in deliveries:
            delivery = await turns_store.add_delivery(
                s,
                turn_id=ids.turns[turn],
                mode=mode,
                source=source,
                text=text,
                media=_MEDIA,
                burst_id=ids.bursts[burst] if burst is not None else None,
                at=at(day, seconds=second),
            )
            assert delivery is not None
            delivery.status = status
            if status != "pending":
                delivery.sent_at = at(day, seconds=second + 1)
        await s.flush()

        steps = (
            ("done", -2, "tool", "Edit src/auth/login.py"),
            ("done", -2, "text", "Testlarni ishga tushiryapman"),
            ("waiting", 0, "todo", "3/5: forma, validatsiya, xat"),
        )
        for turn, day, kind, summary in steps:
            await turns_store.add_progress(
                s, turn_id=ids.turns[turn], at=at(day, seconds=30), kind=kind, summary=summary, keep=50
            )


# --- newproject -----------------------------------------------------------------------------------------------------
async def _seed_project_drafts(db: Database, ids: Ids) -> None:
    building = (
        DraftPart(text="yangi loyiha: kassa dasturi — chek, smena", at=at(-3), burst_id=ids.bursts["routed"]),
        DraftPart(text="kunlik hisobot ham boʻlsin", at=at(-3, seconds=40)),
    )
    abandoned = (DraftPart(text="eski gʻoya: ombor hisobi", at=at(-12)),)
    async with db.uow() as s:
        draft = await newproject_store.add_draft(
            s, status="collecting", parts=parts_json(building), opened_at=at(-3), last_part_at=at(-3, seconds=40)
        )
        await newproject_store.set_draft(
            s,
            draft.id,
            status="building",
            proposed_name="BonviKassa",
            project_id="BonviKassa",
            plan_turn_id=ids.turns["plan"],
            build_turn_id=ids.turns["queued"],
            repo_url="https://github.com/abboskhanme/BonviKassa",
        )
        failed = await newproject_store.add_draft(
            s, status="collecting", parts=parts_json(abandoned), opened_at=at(-12), last_part_at=at(-12)
        )
        await newproject_store.set_draft(
            s, failed.id, status="failed", proposed_name="Ombor", error_code="github_repo_exists", closed_at=at(-10)
        )


# --- qa (personal) --------------------------------------------------------------------------------------------------
async def _seed_assistant(db: Database) -> None:
    """Only the personal bot writes these, in `personal.db`. They are seeded here as well because both files carry one
    schema and every revision runs on both: one fixture then guards every table's columns."""
    async with db.uow() as s:
        closed = await qa_store.add_session(
            s,
            chat_id=OWNER_ID,
            session_id="c7d1e2f3-a4b5-4c6d-8e9f-0a1b2c3d4e5f",
            at=at(-6),
            seed=None,
            prompt_version=3,
        )
        await qa_store.mark_session_answered(
            s, closed.id, at=at(-6, seconds=30), web_searches=2, prompt_version=3, summary="Kvant kompyuter: kubitlar"
        )
        await qa_store.mark_session_closed(s, closed.id, at=at(-5), reason="idle")
        live = await qa_store.add_session(
            s,
            chat_id=PERSONAL_GROUP_ID,
            session_id="d8e2f3a4-b5c6-4d7e-9f0a-1b2c3d4e5f60",
            at=at(-1),
            seed="Kvant kompyuter: kubitlar",
            prompt_version=4,
        )
        await qa_store.mark_session_answered(
            s, live.id, at=at(seconds=-600), web_searches=0, prompt_version=4, summary="Namoz vaqtlari, Toshkent"
        )
        expanded = await qa_store.add_answer(
            s,
            session_row_id=closed.id,
            chat_id=OWNER_ID,
            burst_id=7001,
            text_full=f"{UZ_TEXT}\n{RU_TEXT}",
            provider="claude",
            model="sonnet",
            voice_status="pending",
            first_text_ms=1_200,
            total_ms=4_300,
            fallback=False,
            at=at(-6, seconds=30),
        )
        await qa_store.set_answer_voice(s, expanded.id, status="sent", error_code=None, voice_ms=24_000)
        await qa_store.mark_answer_expanded(s, expanded.id, at=at(-6, seconds=90))
        fallback = await qa_store.add_answer(
            s,
            session_row_id=live.id,
            chat_id=PERSONAL_GROUP_ID,
            burst_id=7002,
            text_full=None,
            provider="google",
            model="gemini-3.8-flash",
            voice_status="pending",
            first_text_ms=None,
            total_ms=2_100,
            fallback=True,
            at=at(seconds=-600),
        )
        await qa_store.set_answer_voice(s, fallback.id, status="failed", error_code="tts_failed", voice_ms=None)


# --- questions ------------------------------------------------------------------------------------------------------
async def _seed_questions(db: Database, ids: Ids) -> None:
    async with db.uow() as s:
        answered = await make_question(s, turn_id=ids.turns["done"], tool_use_id="toolu_01A", at=at(-2, seconds=60))
        assert await questions_store.mark_question_answered(
            s,
            answered,
            from_status=QuestionStatus.OPEN.value,
            to_status=QuestionStatus.ANSWERED.value,
            answer={"answers": {"Qaysi bazadan foydalanamiz?": "PostgreSQL"}, "shown": "1"},
            at=at(-2, seconds=120),
        )
        await questions_store.set_answer_mode(s, answered, mode="callback")
        opened = await make_question(
            s,
            turn_id=ids.turns["waiting"],
            tool_use_id="toolu_02B",
            questions=(agent_question("Xat qaysi tilda boʻlsin?", (("Oʻzbek", "asosiy"), ("Rus", "ikkinchi"))),),
            at=at(seconds=-300),
        )
        ids.questions.update(answered=answered, open=opened)


# --- gate -----------------------------------------------------------------------------------------------------------
async def _seed_decisions(db: Database, ids: Ids) -> None:
    async with db.uow() as s:
        consumed = await make_decision(s, at=at(-2, seconds=200), status="pending")
        assert await gate_store.mark_decision_resolved(
            s,
            consumed.id,
            status="approved",
            at=at(-2, seconds=260),
            update_id=900_000_110,
            grant_expires_at=at(-2, seconds=7_460),
        )
        assert await gate_store.mark_decision_consumed(
            s, consumed.id, at=at(-2, seconds=300), payload={"reason": "remote_command", "session": "agent-session-7"}
        )
        older = await make_decision(
            s, at=at(seconds=-200), command="git push --force origin main", gate_class="force_push"
        )
        newer = await make_decision(s, at=at(seconds=-100), command="git push --force-with-lease origin main")
        older.status = "superseded"
        await gate_store.set_superseded_by(s, older.id, newer.id)
        await s.flush()
        ids.decisions.update(consumed=consumed.id, superseded=older.id, pending=newer.id)


# --- outbox ---------------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class _Message:
    kind: str
    op: Op
    payload: Payload
    priority: int
    status: str
    project: str | None = None
    turn: str | None = None


async def _seed_outbox(db: Database, ids: Ids) -> None:
    messages = (
        _Message("result", Op.SEND_TEXT, Payload(text=f"BonviHR · Natija: {UZ_TEXT}"), 2, "sent", "BonviHR", "done"),
        _Message("ack", Op.REACT, Payload(message_id=5101, emoji="👀"), 0, "sent"),
        _Message(
            "question", Op.SEND_TEXT, Payload(text="BonviHR · Savol: Xat qaysi tilda?"), 0, "sent", "BonviHR", "waiting"
        ),
        _Message(
            "progress",
            Op.EDIT_TEXT,
            Payload(text="3/5", target="progress:3:0", every_s=5.0),
            3,
            "pending",
            "BonviHR",
            "waiting",
        ),
        _Message("notice", Op.SEND_TEXT, Payload(text="Internet uzilgan edi 03:10–03:25"), 1, "failed"),
    )
    async with db.uow() as s:
        for message in messages:
            sent = message.status == "sent"
            row_id = await outbox_store.add_outbox_message(
                s,
                bot="dev",
                chat_id=CHAT,
                op=message.op.value,
                kind=message.kind,
                payload=outbox_domain.payload_json(message.op, message.payload, str),
                dedupe_key=f"{message.kind}:{ids.turns.get(message.turn or '', 0)}:0",
                coalesce_key=f"progress:{ids.turns['waiting']}" if message.kind == "progress" else None,
                priority=message.priority,
                next_attempt_at=at(-2 if sent else 0, seconds=-30),
                reply_to_message_id=5101 if message.kind == "result" else None,
                disable_notification=message.kind == "notice",
                project_id=message.project,
                turn_id=ids.turns[message.turn] if message.turn is not None else None,
                created_at=at(-2 if sent else 0, seconds=-30),
            )
            assert row_id is not None
            ids.outbox[message.kind] = row_id
        sent_ids = {ids.outbox[key] for key in ("result", "ack", "question")}
        for row in await outbox_store.list_outbox_messages(s, "dev"):
            if row.id in sent_ids:
                row.status, row.attempts, row.sent_at = "sent", 1, at(-2, seconds=-29)
            elif row.id == ids.outbox["notice"]:
                row.status, row.attempts, row.last_error_code = "failed", 30, "telegram_forbidden"
        await s.flush()
        links = (
            (5300, "result", "result", "BonviHR", "done", None, None),
            (5301, "question", "question", "BonviHR", "waiting", "open", None),
            (5302, "notice", "decision", "BonviHR", None, None, "pending"),
        )
        for message_id, outbox, kind, project, turn, question, decision in links:
            assert await outbox_store.add_message_link(
                s,
                bot="dev",
                chat_id=CHAT,
                message_id=message_id,
                outbox_id=ids.outbox[outbox],
                kind=kind,
                project_id=project,
                turn_id=ids.turns[turn] if turn is not None else None,
                question_id=ids.questions[question] if question is not None else None,
                decision_id=ids.decisions[decision] if decision is not None else None,
                created_at=at(-2, seconds=-28) if outbox == "result" else at(seconds=-290),
            )


# --- digest ---------------------------------------------------------------------------------------------------------
async def _seed_digests(db: Database, ids: Ids) -> None:
    commit = digest_domain.CommitLine(sha="a1b2c3d4" * 5, subject="fix(login): parol maydoni", at=at(-2, seconds=880))
    result = digest_domain.ResultLine(
        turn_id=ids.turns["done"], status=TurnStatus.DONE, title="Login", natija=UZ_TEXT, tests="passed", at=at(-2)
    )
    full: dict[str, object] = {
        "updated_at": at(seconds=-120),
        "checked_at": at(seconds=-60),
        "git_branch": "main",
        "git_head": "a1b2c3d4" * 5,
        "git_head_subject": "fix(login): parol maydoni",
        "git_head_at": at(-2, seconds=880),
        "git_upstream": "origin/main",
        "ahead": 0,
        "behind": 2,
        "dirty_count": 1,
        "untracked_count": 3,
        "git_error_code": None,
        "recent_commits_json": digest_domain.commits_json((commit,)),
        "recent_files_json": digest_domain.files_json(("src/auth/login.py",)),
        "recent_files_count": 1,
        "results_json": digest_domain.results_json((result,)),
        "last_natija": UZ_TEXT,
        "last_natija_at": at(-2, seconds=906),
        "last_turn_id": ids.turns["done"],
        "open_questions": 1,
        "pending_decisions": 1,
        "queued": 1,
        "parked": 0,
        "handoff_note": "Login tuzatildi. Keyingisi: parolni tiklash sahifasi.",
        "handoff_note_at": at(-2, seconds=906),
        "handoff_note_source": "executor",
        "handoff_turn_id": ids.turns["done"],
        "executor": "claude",
        "session_id": "5b0d8c1e-2f6a-4c3b-9e7d-1a2b3c4d5e6f",
    }
    unreadable: dict[str, object] = {
        **dict.fromkeys(full, None),
        "updated_at": at(-1),
        "checked_at": at(-1),
        "ahead": 0,
        "behind": 0,
        "dirty_count": 0,
        "untracked_count": 0,
        "git_error_code": "git_locked",
        "recent_commits_json": [],
        "recent_files_json": [],
        "recent_files_count": 0,
        "results_json": [],
        "open_questions": 0,
        "pending_decisions": 0,
        "queued": 0,
        "parked": 0,
        "executor": "claude",
    }
    async with db.uow() as s:
        await digest_store.set_digest(s, "BonviHR", full)
        await digest_store.set_digest(s, "Yordamchi", unreadable)


# --- servers --------------------------------------------------------------------------------------------------------
async def _seed_server_checks(db: Database) -> None:
    healthy: JsonValue = {"uptime_s": 1_047_840, "disk": {"/": 85, "/srv/data": 9}, "issues": [], "excerpts": []}
    down: JsonValue = {
        "issues": ["unreachable"],
        "alerted": {"unreachable": "2026-10-23T08:30:00+00:00"},
        "excerpts": ["…"],
    }
    async with db.uow() as s:
        await servers_store.add_server_check(
            s, host="bonvi-srv", at=at(seconds=-1_800), ok=True, summary=healthy, error_code=None, duration_ms=2_310
        )
        await servers_store.add_server_check(
            s,
            host="bonvi-srv",
            at=at(seconds=-30),
            ok=False,
            summary=down,
            error_code="ssh_unreachable",
            duration_ms=10_002,
        )


# --- selfupdate -----------------------------------------------------------------------------------------------------
async def _seed_self_updates(db: Database, ids: Ids) -> None:
    async with db.uow() as s:
        healthy = await selfupdate_store.add_self_update(
            s, status="working", kind="app", branch="selfupdate/7-holat-tezroq", base_commit="a" * 40, at=at(-3)
        )
        await selfupdate_store.set_self_update(
            s,
            healthy.id,
            status="healthy",
            commit="b" * 40,
            version="2026.10.20-bbbbbbb",
            changed_files_json=["src/yordamchi/shared/health/service.py"],
            touches_core=True,
            decision_id=ids.decisions["consumed"],
            turn_id=ids.turns["done"],
            tests_count=2_311,
            result_code="healthy",
            summary="holat? endi 1 s ichida javob beradi",
            requested_at=at(-3, seconds=600),
            finished_at=at(-3, seconds=1_500),
        )
        assert await selfupdate_store.mark_self_update_reported(s, healthy.id, at=at(-3, seconds=1_501))
        await selfupdate_store.add_self_update(
            s, status="working", kind="supervisor", branch="selfupdate/8-watchdog", base_commit=None, at=at(seconds=-60)
        )


# --- settings -------------------------------------------------------------------------------------------------------
async def _seed_settings(db: Database) -> None:
    await add_setting_row(db, "voice.enabled", value=True, at=at(-6))
    await add_setting_row(db, "voice.enabled", value=False, at=at(-5))
    await add_setting_row(db, "projects.aliases", value={"hr": "BonviHR", "bot": "Yordamchi"}, at=at(-4))
    async with db.uow() as s:  # the second `voice.enabled` row is the owner's revert, with his update id
        revert = await s.scalar(
            select(SettingVersion).where(SettingVersion.key == "voice.enabled", SettingVersion.version == 2)
        )
        assert revert is not None
        revert.reason, revert.origin_update_id = "revert", 900_000_050
        await s.flush()


# --- vault and integrations -----------------------------------------------------------------------------------------
async def _seed_vault(db: Database, keystore: InMemoryKeystore, ids: Ids) -> None:
    google = await store_secret(db, keystore, "google_api_key", FAKE_KEYS["google_api_key"], update_id=900_000_060)
    old = await store_secret(db, keystore, "openrouter_api_key", FAKE_KEYS["openrouter_api_key"], update_id=900_000_061)
    token = await store_secret(db, keystore, "generic_secret", INTEGRATION_TOKEN, update_id=900_000_062)
    async with db.uow() as s:
        row = await s.get(VaultSecret, old.id)
        assert row is not None
        await vault_store.mark_secret_removed(s, row, at=at(-1, micros=999_999))
    ids.secrets.update(google=google.id, removed=old.id, token=token.id)


async def _seed_integrations(db: Database, keystore: InMemoryKeystore, ids: Ids) -> None:
    async with db.uow() as s:
        await integrations_store.add_integration(
            s,
            slug="bonvi_hr",
            display_name="Bonvi HR",
            kind="mcp_http",
            url="https://hr.bonvi.example.uz/mcp",
            auth="bearer",
            bots=["dev", "personal"],
            tool_policy={"dev": "all", "personal": ["employees_list"]},
            tools_cache=[{"name": "employees_list", "read_only": True}, {"name": "salary_set", "read_only": False}],
            status="ok",
            created_at=at(-7),
            description="Kadrlar: faqat oʻqish",
            secret_id=ids.secrets["token"],
            last_validated_at=at(seconds=-3_600),
        )
        header = await integrations_store.add_integration(
            s,
            slug="markaz_api",
            display_name="Markaz API",
            kind="api_http",
            url="https://lc.wunderkind.example.uz/api",
            auth="header",
            bots=["dev"],
            tool_policy={"dev": "all"},
            tools_cache=[],
            status="auth_failed",
            created_at=at(-6),
            header_name="X-Api-Key",
            secret_id=ids.secrets["token"],
        )
        await integrations_store.set_status(
            s, header, status="auth_failed", status_code="integration_auth_failed", validated_at=at(seconds=-60)
        )
        oauth = await integrations_store.add_integration(
            s,
            slug="wunderkind_lc",
            display_name="Wunderkind LC",
            kind="mcp_http",
            url="https://lc.wunderkind.example.uz/mcp",
            auth="oauth",
            bots=["dev"],
            tool_policy={"dev": ["groups_list"]},
            tools_cache=[{"name": "groups_list", "read_only": True}],
            status="needs_reauth",
            created_at=at(-5),
        )
        removed = await integrations_store.add_integration(
            s,
            slug="eski_crm",
            display_name="Eski CRM",
            kind="mcp_http",
            url="https://crm.example.uz/mcp",
            auth="oauth",
            bots=["dev"],
            tool_policy={"dev": "all"},
            tools_cache=[],
            status="disabled",
            created_at=at(-9),
        )
        await integrations_store.mark_integration_removed(s, removed, at=at(-2))
        for row, failures in ((oauth, 3), (removed, 0)):
            client = await integrations_store.put_oauth_client(
                s, integration_id=row.id, issuer="https://auth.example.uz", client_id=f"yordamchi-{row.slug}", key_fp=""
            )
            sealed = keystore.seal(
                b'{"access_token": "fake", "refresh_token": "fake"}', aad=f"oauth_clients|{row.id}|{row.slug}".encode()
            )
            await integrations_store.set_grant(
                s,
                client,
                ciphertext=sealed.ciphertext,
                nonce=sealed.nonce,
                key_fp=sealed.key_fp,
                expires_at=at(seconds=3_600),
                refreshed_at=at(seconds=-60),
            )
            if failures:
                await integrations_store.set_refresh_failures(s, client, failures=failures)
                await integrations_store.mark_reauth_told(s, client, at=at(seconds=-30))


# --- audit, cost, health --------------------------------------------------------------------------------------------
async def _seed_audit(db: Database, ids: Ids) -> None:
    """The vault already audited its three secrets (`secret_added`); these are the rest of a day's trail."""
    events: tuple[tuple[AuditActor, AuditKind, str | None, int | None, JsonObject], ...] = (
        (AuditActor.SYSTEM, AuditKind.SESSION_OPENED, "agent_session", ids.sessions["live"], {"policy": "FULL"}),
        (AuditActor.EXECUTOR, AuditKind.GATE_VERDICT, "decision", ids.decisions["consumed"], {"class": "server_write"}),
        (AuditActor.OWNER, AuditKind.DECISION_RESOLVED, "decision", ids.decisions["consumed"], {"status": "approved"}),
        (AuditActor.SYSTEM, AuditKind.ACCESS_DROPPED, None, None, {"user_id": 200000002, "chat_id": -1009999999999}),
    )
    async with db.uow() as s:
        for index, (actor, kind, ref_type, ref_id, data) in enumerate(events):
            await audit_service.record_event(
                s,
                bot=BotName.DEV,
                at=at(-2, seconds=200 + index),
                actor=actor,
                kind=kind,
                data=data,
                project_id="BonviHR" if ref_type is not None else None,
                ref_type=ref_type,
                ref_id=str(ref_id) if ref_id is not None else None,
            )


async def _seed_cost(db: Database, ids: Ids) -> None:
    await add_usage(db, make_usage(), at=at(-2, seconds=4))
    await add_usage(
        db,
        make_turn_usage(turn_id=ids.turns["done"], cost_usd_micros=BIG_MICROS, duration_ms=900_000),
        make_usage(
            source="tts", provider=Provider.OPENROUTER, model="google/gemini-3.8-flash-tts", units={"characters": 380}
        ),
        at=at(-2, seconds=906),
    )
    async with db.uow() as s:
        assert await cost_store.add_cap_notice(
            s, provider="google", month="2026-10", level=80, cap_usd=40, sent_at=at(-1)
        )
        assert await cost_store.add_cap_notice(
            s, provider="openrouter", month="2026-09", level=100, cap_usd=10, sent_at=at(-30)
        )


async def _seed_health(db: Database) -> None:
    await add_health_event(db, at=at(-2), kind="started", data={"version": "2026.10.21-aaaaaaa", "pid": 4242})
    await add_health_event(db, at=at(-1, seconds=30), kind="wake_gap", data={"gap_s": 26_100})
    await add_health_event(db, at=at(seconds=-5), kind="clean_exit", data={})
    async with db.uow() as s:
        await health_store.set_kv(s, bot="dev", key="pinned_message_id", value=5001, at=at(-2))
        await health_store.set_kv(s, bot="dev", key="cli_version", value="2.1.310", at=at(-1))
        await health_store.set_kv(s, bot="dev", key="speech_status", value={"google": "ok", "openrouter": ""}, at=at())


async def seed(deps: DevDeps, keystore: InMemoryKeystore) -> None:
    """Fill a migrated `dev.db` with the release's rows, parents before children."""
    db, ids = deps.db, Ids()
    await _seed_projects(db)
    await _seed_agent_sessions(db, ids)
    await _seed_bursts(db, ids)
    await _seed_inbound_updates(db, ids)
    await _seed_transcripts(db)
    await _seed_routing(db, ids)
    await _seed_turns(db, ids)
    await _seed_project_drafts(db, ids)
    await _seed_questions(db, ids)
    await _seed_decisions(db, ids)
    await _seed_outbox(db, ids)
    await _seed_digests(db, ids)
    await _seed_server_checks(db)
    await _seed_self_updates(db, ids)
    await _seed_settings(db)
    await _seed_vault(db, keystore, ids)
    await _seed_integrations(db, keystore, ids)
    await _seed_audit(db, ids)
    await _seed_cost(db, ids)
    await _seed_health(db)
    await _seed_assistant(db)


def coverage_gaps(db_path: Path) -> list[str]:
    """Tables without rows and columns that are NULL in every row: a migration could lose either unnoticed."""
    gaps: list[str] = []
    for name, table in read_tables(db_path).items():
        if not table.rows:
            gaps.append(f"{name}: no rows")
            continue
        gaps.extend(
            f"{name}.{column}: NULL in every row"
            for index, column in enumerate(table.columns)
            if all(row[index] is None for row in table.rows)
        )
    return gaps


async def build(target: Path, *, workdir: Path) -> None:
    """Migrate an empty file to `RELEASE_REVISION`, seed it, and write it to `target` as one checkpointed file."""
    later = revisions_after(RELEASE_REVISION)
    if later:
        msg = (
            f"the chain already has {[script.revision for script in later]} after {RELEASE_REVISION}: "
            f"{P1_RELEASE.name} is the real previous release now and is only ever upgraded"
        )
        raise FixtureFrozen(msg)
    scratch = workdir / "db" / "dev.db"
    scratch.parent.mkdir(parents=True, exist_ok=True)
    scratch.unlink(missing_ok=True)
    upgrade(scratch, RELEASE_REVISION)
    keystore = InMemoryKeystore(FIXTURE_KEY)
    fakes = Fakes(clock=FakeClock(RELEASE_AT), keystore=keystore)
    db = open_database(scratch)
    try:
        await seed(make_dev_deps(db=db, paths=make_paths(workdir), fakes=fakes), keystore)
    finally:
        await db.dispose()
    with closing(connect(scratch)) as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.execute("PRAGMA journal_mode=DELETE")  # one self-contained file: `*.db-wal` is never committed
        connection.execute("VACUUM")
    gaps = coverage_gaps(scratch)
    if gaps:
        msg = "the release fixture leaves data out (extend the seeders):\n" + "\n".join(gaps)
        raise AssertionError(msg)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(scratch, target)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="yd-p1-release-") as workdir:
        asyncio.run(build(P1_RELEASE, workdir=Path(workdir)))  # noqa: TID251  # a process of its own: its one loop
    sys.stdout.write(f"wrote {P1_RELEASE}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
