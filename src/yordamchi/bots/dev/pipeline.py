"""The dev bot's chat pipeline: every closed burst, in order, through the deterministic pre-parse of SPEC §6.1 and
then routing (SPEC §3.2.1 steps 6–7, §3.2.2–§3.2.6; UC-02, UC-03, UC-10, UC-12, UC-13, UC-15).

Each burst takes exactly one of these paths, first match wins:
1. the self-test (a synthetic update: P2 probation; recorded, nothing else) and the interrupt words — "to'xta",
   "stop", "bas", "hammasini to'xtat", "yo'q, boshqacha qil: …" — which skip the window and every model (≤ 3 s).
   "kut" is none of them: it never stops anything, it holds the burst window for the next part (`shared/burst`);
2. commands that can never be an answer ("holat?", "/sozlamalar", "loyihalar", "xarajat?", and small talk:
   "salom", "qandaysan?", "rahmat", "nima qila olasan?": `intents.EARLY`); a bare "xo'p" / "ok" comes after
   binding (step 5) and gets no reply;
3. `routing.answer_clarification`: the answer to "BonviHR'gami yoki HRMentor'gami?";
4. `turns.answer_revert`, then `questions.answer_owner`: a wrong-target revert offer, a gate decision, an agent
   question (in that order, so a bare "ha" binds to at most one of them);
5. the other typed aliases and exact phrases of `bots/dev/intents.py`, the clear settings cues among them
   ("sozlama: …", "javoblaringni …", "ovozingni …"; one that names a project is work for it) and "X yangi sessiya";
6. `routing.correct_route`: "bu HRMentor uchun edi";
7. `turns.route_turn`: the work goes to a project (an investigation with "Tekshiryapman" first), or the report
   intents to `digest.answer_report` (≤ 4 s, never through the turn scheduler), or a message about none of his
   projects (`chat`: "Python'da decorator nima?") to `dev/chat`'s plain answer, or one routing question is asked.
   Words naming a watched server go to `dev/servers` instead: its status (a background job) or a read-only server
   investigation (T31).

Forum topics (D4 revised 2026-10-26; SPEC §3.2.16). A burst written in a project's own topic
(`topics.project_of_thread`) takes the same path with that project in mind: a bare "to'xta", "davom ettir", "qayta
yubor", "yangi sessiya" or `/hisobot` acts on it; it is the project an answer is for, so a "ha" or a "2" there binds
only that project's open items; no clarification is offered it; and step 7 sends it to that project with no routing
model (`turns.route_topic`, method `topic`). Every answer to a burst goes to the topic it was written in (General:
none). A burst in General, or in a topic the bot did not open, is routed as before; its work then goes on in the
project's own topic. With `topics.enabled` off every burst is read as one in General, and the bot's topics are set
aside at each pass.

Commands, interrupts and answers read only the owner's own words (`TurnInput.own_text`): a message he forwarded is
material, never an order (T33). Steps that need the owner's authority (4, 5) get the `OwnerOrigin` intake stored
the update with, judged again by today's access policy (`inbox.origin_of_update`); without one they are skipped.
Every burst is handled once: each
path records it (routed, merged or cancelled), and one that fails is not taken again by this process (`guarded()`
told the owner; the restart pass routes it once more, SPEC §2.7). Nothing here awaits a model or the executor
directly: each step is one feature call.
"""

import asyncio
from dataclasses import dataclass, field
from functools import partial
from typing import Final

from yordamchi.bots.dev import intents, jobs
from yordamchi.bots.dev.intents import Command, Intent
from yordamchi.core import aio
from yordamchi.core import strings as S
from yordamchi.core.log import get_log
from yordamchi.core.text import BotText, join
from yordamchi.dev.chat import service as chat
from yordamchi.dev.deps import DevDeps
from yordamchi.dev.digest import domain as digest_domain
from yordamchi.dev.digest import service as digest
from yordamchi.dev.digest.domain import ReportQuestion
from yordamchi.dev.digest.service import DigestState
from yordamchi.dev.gate import service as gate
from yordamchi.dev.projects import service as projects
from yordamchi.dev.questions import service as questions
from yordamchi.dev.questions.service import AnswerOutcome
from yordamchi.dev.routing import domain as routing_domain
from yordamchi.dev.routing import service as routing
from yordamchi.dev.routing.domain import ChoiceKind, CorrectionKind, Outcome, RouteFacts
from yordamchi.dev.routing.domain import Intent as Routed
from yordamchi.dev.routing.service import Correction, Resolution
from yordamchi.dev.routing.service import Routed as RoutedDecision
from yordamchi.dev.servers import domain as servers_domain
from yordamchi.dev.servers import service as servers
from yordamchi.dev.turns import domain as turns_domain
from yordamchi.dev.turns import service as turns
from yordamchi.dev.turns.domain import TurnKind, TurnRequest
from yordamchi.dev.turns.service import RevertOutcome, RevertReply, TurnsState
from yordamchi.shared.access.domain import OwnerOrigin
from yordamchi.shared.burst import service as burst
from yordamchi.shared.burst.domain import InterruptKind, TurnInput
from yordamchi.shared.cost import service as cost
from yordamchi.shared.health import service as health
from yordamchi.shared.health.service import HealthState
from yordamchi.shared.inbox import service as inbox
from yordamchi.shared.inbox.domain import UpdateKind
from yordamchi.shared.outbox import domain as outbox_domain
from yordamchi.shared.outbox import service as outbox
from yordamchi.shared.outbox.domain import About, Kind, Thread
from yordamchi.shared.settings import defaults
from yordamchi.shared.settings import service as settings
from yordamchi.shared.topics import service as topics
from yordamchi.shared.voice import service as voice

log = get_log(__name__)

BATCH: Final = 50  # closed bursts read per pass; the pass repeats until none is left
# A question the bot posts gets its Telegram message id once the outbox sent it; until then a reply cannot be bound
# to it (the newest open one still is). Mechanism, not behaviour: the outbox sends within seconds.
LINK_POLL_S: Final = 1.0
LINK_WAIT_S: Final = 120.0
# A command's own words: a text, or a voice note's transcript. A burst with more parts is a project message.
_COMMAND_KINDS: Final = frozenset({UpdateKind.TEXT, UpdateKind.VOICE, UpdateKind.EDITED})
_WORKING: Final = frozenset(turns_domain.WORKING)
_TURN_KINDS: Final = {Routed.INSTRUCT: TurnKind.INSTRUCT, Routed.INVESTIGATE: TurnKind.INVESTIGATE}
# The later-phase intents routing itself may return.
_ROUTED_LATER: Final = frozenset({Routed.NEW_PROJECT})
# The commands that change something on the owner's word, or show what only the owner may see.
_OWNER_ONLY: Final = frozenset(
    {
        Intent.SHOW_SETTINGS,
        Intent.CHANGE_SETTING,
        Intent.REVERT_SETTING,
        Intent.ADD_TERM,
        Intent.ALIAS_PROJECT,
        Intent.ARCHIVE_PROJECT,
        Intent.UNARCHIVE_PROJECT,
        Intent.DESCRIBE_PROJECT,
        Intent.WATCH_SERVER,
        Intent.UNWATCH_SERVER,
        Intent.OPEN_ALL_TOPICS,  # opens topics in the owner's group
    }
)


@dataclass(frozen=True, slots=True)
class Where:
    """Where a burst was written (D4 revised 2026-10-26): its forum topic (`thread.id` None: General, or a group with
    no Topics), and the project that topic belongs to (None: General, a topic the bot did not open, or
    `topics.enabled` off)."""

    thread: Thread
    project: str | None = None


GENERAL_BURST: Final = Where(Thread())


@dataclass(slots=True)
class Pipeline:
    """Built once by `bots/dev/runtime.py`. `restarted` lists the projects whose turns the last restart cut off, so a
    bare "davom ettir" right after "Qayta ishga tushdim…" knows what to continue."""

    deps: DevDeps
    turns: TurnsState
    digest: DigestState
    health: HealthState
    restarted: tuple[str, ...] = ()
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    failed: set[int] = field(default_factory=set)  # bursts that failed here: not taken again by this process

    def poke(self) -> None:
        """A burst closed: run a pass now."""
        self.wake.set()

    # --- the loop ---------------------------------------------------------------------------------------------------
    async def run(self) -> None:
        """The long-lived loop (`aio.forever("router", …)`): a pass at start (closed bursts a restart left: SPEC §2.7
        "route now"), then one after every `poke()`."""
        while True:
            self.wake.clear()
            await self.drain()
            await self.wake.wait()

    async def drain(self) -> int:
        """Handle every closed burst, interrupts first, one at a time, each under `guarded()`. Returns how many."""
        await self._follow_topics_setting()
        handled = 0
        while True:
            closed = await burst.list_closed_bursts(self.deps, limit=BATCH + len(self.failed))
            items = [item for item in closed if item.burst_id not in self.failed]
            if not items:
                return handled
            for item in items:
                done = await jobs.run(
                    self.deps, self.health, self.handle(item), job="route_burst", chat_id=item.chat_id
                )
                if done is None:
                    self.failed.add(item.burst_id)
                handled += 1

    async def handle(self, item: TurnInput) -> bool:  # noqa: PLR0911  # one return per path (module docstring)
        """One closed burst down the path of the module docstring. True once it was handled (whichever path)."""
        if item.synthetic:
            await burst.cancel_burst(self.deps, item.burst_id)  # the P2 self-test: nothing to route
            return True
        where = await self._where(item)
        if (kind := item.interrupt) is not None:
            await self._interrupt(item, kind, where)
            return True
        command = self._command_of(item)
        origin = await inbox.origin_of_update(self.deps, item.parts[-1].update_id)
        if command is not None and command.early and await self._command(item, command, origin, where):
            return True
        facts = await self._facts(item)
        # A clarification is asked where routing runs: never of a burst written in a project's own topic.
        resolution = await routing.answer_clarification(self.deps, item, facts) if where.project is None else None
        if resolution is not None:
            await self._resolved(item, resolution)
            if resolution.consumed:
                return True
        if origin is not None and await self._answered(item, origin, where):
            return True
        if command is not None and not command.early and await self._command(item, command, origin, where):
            return True
        correction = await routing.correct_route(self.deps, item, facts)
        if correction is not None:
            await self._corrected(item, correction)
            return True
        await self._route(item, facts, where)
        return True

    # --- forum topics (D4 revised 2026-10-26) ---------------------------------------------------------------------
    async def _where(self, item: TurnInput) -> Where:
        """The topic the burst was written in, and the project whose topic it is (module docstring)."""
        if item.thread_id is None:
            return GENERAL_BURST
        thread = Thread(item.thread_id)
        snap = await settings.snapshot(self.deps)
        if not snap[defaults.TOPICS_ENABLED]:
            return Where(thread)
        project = await topics.project_of_thread(self.deps, chat_id=item.chat_id, thread_id=item.thread_id)
        return Where(thread, project)

    async def _follow_topics_setting(self) -> None:
        """`topics.enabled` off: the bot's topics are set aside, so its projects' work goes to General from the next
        message on (a project's next work reopens its topic once the setting is on again)."""
        snap = await settings.snapshot(self.deps)
        if not snap[defaults.TOPICS_ENABLED]:
            await topics.set_topics_aside(self.deps, chat_id=snap[defaults.ACCESS_DEV_GROUP_ID])

    # --- facts and replies --------------------------------------------------------------------------------------
    async def _facts(self, item: TurnInput) -> RouteFacts:
        """What other features know about this burst (routing's `RouteFacts`): the projects of the bot messages it
        replies to, the projects waiting for an answer, and whether any question or decision is open."""
        reply_projects: dict[int, str] = {}
        for part in item.parts:
            if part.reply_to_message_id is None:
                continue
            link = await outbox.find_message_link(self.deps, chat_id=item.chat_id, message_id=part.reply_to_message_id)
            if link is not None and link.about.project_id is not None:
                reply_projects[part.reply_to_message_id] = link.about.project_id
        waiting = await questions.waiting_projects(self.deps)
        pending = await gate.pending_items(self.deps)
        return RouteFacts(
            reply_projects=reply_projects, open_questions=waiting, others_pending=bool(waiting or pending)
        )

    async def _reply(
        self, item: TurnInput, text: BotText, *, kind: Kind = Kind.REPORT, ref: str = "", project: str | None = None
    ) -> str:
        """Queue one answer to this burst, in the topic it was written in (General: none), also when it is about a
        project that has a topic of its own. Returns the outbox key of its first part."""
        snap = await settings.snapshot(self.deps)
        ref = ref or f"burst-{item.burst_id}"
        await outbox.enqueue_reply(
            self.deps,
            chat_id=item.chat_id,
            text=text,
            kind=kind,
            ref=ref,
            max_parts=snap[defaults.OUTBOX_MAX_PARTS],
            about=About(project_id=project),
            thread=Thread(item.thread_id),
        )
        return outbox_domain.dedupe_key(kind, ref, 0)

    def _link_when_sent(self, clarification_id: int, message_key: str) -> None:
        """Once the question went out, a reply to it binds to it (`routing.set_clarification_message`)."""
        job = jobs.run(self.deps, self.health, self._link(clarification_id, message_key), job="clarification_link")
        aio.spawn(f"clarification_link_{clarification_id}", job)

    async def _link(self, clarification_id: int, message_key: str) -> None:
        waited = 0.0
        while waited <= LINK_WAIT_S:
            delivery = await outbox.get_delivery(self.deps, message_key)
            if delivery is not None and delivery.message_id is not None:
                await routing.set_clarification_message(self.deps, clarification_id, delivery.message_id)
                return
            await self.deps.clock.sleep(LINK_POLL_S)
            waited += LINK_POLL_S
        log.warning("clarification_unlinked", clarification_id=clarification_id)

    # --- projects named or replied to ---------------------------------------------------------------------------
    async def _named(self, text: str) -> str | None:
        """The one registered project `text` names, or None."""
        named = routing_domain.find_mentions(text, await projects.list_projects(self.deps)).named
        return named[0] if len(named) == 1 else None

    async def _replied(self, item: TurnInput) -> str | None:
        for part in item.parts:
            if part.reply_to_message_id is None:
                continue
            link = await outbox.find_message_link(self.deps, chat_id=item.chat_id, message_id=part.reply_to_message_id)
            if link is not None and link.about.project_id is not None:
                return link.about.project_id
        return None

    async def _working(self) -> tuple[str, ...]:
        active = await turns.list_active_turns(self.deps)
        return tuple(dict.fromkeys(turn.project_id for turn in active if turn.status in _WORKING))

    # --- 1. interrupts (SPEC §3.2.5) ----------------------------------------------------------------------------
    async def _interrupt(self, item: TurnInput, kind: InterruptKind, where: Where) -> None:
        if not await burst.cancel_burst(self.deps, item.burst_id):
            return  # another pass handled it
        if kind is InterruptKind.HOLD:
            return  # a "kut" an older release closed as an interrupt (routed after the update): it stops nothing
        text = item.own_text  # an interrupt is the owner's own words (a forwarded "to'xta" is none, T33)
        if kind is InterruptKind.STOP_ALL:
            stopped = await turns.stop_all(self.deps, self.turns)
            if stopped.reply is not None:
                await self._reply(item, stopped.reply, kind=Kind.NOTICE)
            return
        instruction = _redirected(text) if kind is InterruptKind.REDIRECT else ""
        # In a project's topic, a bare "to'xta" stops that project, whatever else works.
        target = await self._replied(item) or await self._named(instruction) or where.project
        if target is None:
            working = await self._working()
            if len(working) > 1:
                await self._reply(item, S.WHICH_RUNNING(projects=", ".join(working)), kind=Kind.NOTICE)
                return
            target = working[0] if working else None
        if target is None:
            await self._reply(item, S.STOP_ALL_NOTHING(), kind=Kind.NOTICE)
            return
        if kind is InterruptKind.REDIRECT and instruction:
            request = TurnRequest(project_id=target, text=instruction, burst_id=item.burst_id)
            await turns.redirect_turn(self.deps, self.turns, request)
            return
        await self._stop(item, target)

    async def _stop(self, item: TurnInput, project: str) -> None:
        stopped = await turns.stop_turn(self.deps, self.turns, project)
        if stopped.reply is not None:
            await self._reply(item, stopped.reply, kind=Kind.NOTICE, project=project)

    # --- 2 and 5. commands (bots/dev/intents.py) ----------------------------------------------------------------
    @staticmethod
    def _command_of(item: TurnInput) -> Command | None:
        """A one-part burst in the owner's own words (`own_text`: a forwarded "holat?" or "sozlama: …" is someone
        else's, so material, never a command; T33)."""
        if len(item.parts) != 1 or item.parts[0].kind not in _COMMAND_KINDS:
            return None
        return intents.command_of(item.own_text)

    async def _command(self, item: TurnInput, command: Command, origin: OwnerOrigin | None, where: Where) -> bool:
        """Carry out one recognised command. False when it does not apply after all (the project it names is not
        one of ours, a settings cue sits in work for a named project, or it needs the owner's authority and there
        is none): the burst goes on down the path. A command that names no project acts on the topic's project."""
        project: str | None = None
        if command.project is not None:
            project = await self._named(command.project)
            if project is None and command.intent is Intent.REPORT:
                project = command.project  # `/hisobot Foo`: digest says no such project
            elif project is None:
                return False  # "serverni to'xtat" names no project: an instruction for routing
        # "OvozYoz'da ovozingni …": a cue word in work for a project, which routing sends there.
        named_work = command.implicit and await self._names_a_project(item.own_text)
        needs_origin = command.intent in _OWNER_ONLY
        if named_work or (needs_origin and origin is None):
            return False
        if not await burst.cancel_burst(self.deps, item.burst_id):
            return True
        log.info("command_matched", burst_id=item.burst_id, intent=command.intent.value)
        if not command.built:
            await self._reply(item, S.NOT_YET(phase=command.phase), kind=Kind.NOTICE)
            return True
        if origin is not None and needs_origin:
            await self._owner_command(item, command, origin, project)
            return True
        await self._plain_command(item, command, project, where)
        return True

    async def _plain_command(self, item: TurnInput, command: Command, project: str | None, where: Where) -> None:
        if await self._talk(item, command):
            return
        deps, intent = self.deps, command.intent
        project = project or where.project  # "to'xta", "davom ettir", "yangi sessiya", `/hisobot` in a project topic
        if intent is Intent.SHOW_HEALTH:
            text = await health.show_health(deps, self.health, work=partial(turns.work_status, deps))
            await self._reply(item, text)
        elif intent is Intent.LIST_PROJECTS:
            await self._reply(item, await projects.show_projects(deps))
        elif intent is Intent.SHOW_COST:  # "xarajat?" (UC-07): queued as a reply to the question itself
            await cost.show_cost(
                deps, chat_id=item.chat_id, ref=f"burst-{item.burst_id}", reply_to=item.parts[0].message_id
            )
        elif intent is Intent.NEW_SESSION:
            if project is None:
                await self._reply(item, S.WHICH_PROJECT_NAME(), kind=Kind.NOTICE)
                return
            text = await turns.new_session(deps, self.turns, project)
            await self._reply(item, text, kind=Kind.NOTICE, project=project)
        elif intent is Intent.REPORT:
            await self._report(item, project or "", project, Routed.REPORT if project else Routed.REPORT_ALL)
        elif intent is Intent.STOP_PROJECT:
            target = project or await self._replied(item)
            if target is None:
                working = await self._working()
                if len(working) != 1:
                    nothing = S.STOP_ALL_NOTHING() if not working else S.WHICH_RUNNING(projects=", ".join(working))
                    await self._reply(item, nothing, kind=Kind.NOTICE)
                    return
                target = working[0]
            await self._stop(item, target)
        elif intent in {Intent.CONTINUE_TURN, Intent.RETRY_DELIVERY}:
            target = project or await self._replied(item) or (self.restarted[0] if len(self.restarted) == 1 else None)
            if target is None:
                await self._reply(item, S.WHICH_PROJECT_NAME(), kind=Kind.NOTICE)
                return
            act = turns.continue_turn if intent is Intent.CONTINUE_TURN else turns.retry_delivery
            await self._reply(item, await act(deps, self.turns, target), kind=Kind.NOTICE, project=target)

    async def _talk(self, item: TurnInput, command: Command) -> bool:
        """Small talk — "salom", "qandaysan?", "rahmat", "nima qila olasan?" — answered from the catalog, no model, and
        never with project status (owner, 2026-10-24)."""
        if command.intent is Intent.GREETING:
            replies = {intents.GREETING_HOW: S.CHAT_HOW, intents.GREETING_SALAM: S.CHAT_SALAM}
            text = replies.get(command.argument, S.CHAT_HELLO)()
        elif command.intent is Intent.THANKS:
            text = S.CHAT_THANKS()
        elif command.intent is Intent.HELP:
            text = S.HELP()
        else:
            # a bare "xo'p" reaches here only when nothing was open to answer: no reply (the read mark shows ✓✓)
            return command.intent is Intent.ACKNOWLEDGE
        await self._reply(item, text, kind=Kind.NOTICE)
        return True

    async def _owner_command(self, item: TurnInput, command: Command, origin: OwnerOrigin, project: str | None) -> None:
        deps, intent = self.deps, command.intent
        ask = partial(gate.ask_setting_change, deps)
        if intent is Intent.SHOW_SETTINGS:
            await settings.show_settings(deps, origin)  # queues the summary and the full list itself
        elif intent is Intent.CHANGE_SETTING:  # queues its reply itself; `/sozlamalar <request>` hands over the request
            await settings.handle_request(deps, origin, command.argument or item.own_text, ask=ask)
        elif intent is Intent.REVERT_SETTING:
            await settings.revert_setting(deps, origin, command.argument or None, ask=ask)
        elif intent is Intent.ADD_TERM:
            await self._reply(item, await voice.add_owner_term(deps, origin, command.argument), kind=Kind.NOTICE)
        elif project is not None and intent is Intent.ALIAS_PROJECT:
            await self._reply(
                item, await projects.alias_project(deps, origin, project, command.argument), project=project
            )
        elif project is not None and intent in {Intent.ARCHIVE_PROJECT, Intent.UNARCHIVE_PROJECT}:
            archived = intent is Intent.ARCHIVE_PROJECT
            text = await projects.archive_project(deps, origin, project, archived=archived)
            await self._reply(item, text, project=project)
        elif project is not None and intent is Intent.DESCRIBE_PROJECT:
            text = await projects.describe_project(deps, origin, project, command.argument)
            await self._reply(item, text, project=project)
        elif intent is Intent.WATCH_SERVER:
            await self._reply(item, await servers.add_server_host(deps, origin, command.argument), kind=Kind.NOTICE)
        elif intent is Intent.UNWATCH_SERVER:
            await self._reply(item, await servers.remove_server_host(deps, origin, command.argument), kind=Kind.NOTICE)
        elif intent is Intent.OPEN_ALL_TOPICS:
            await self._open_all_topics(item)

    async def _open_all_topics(self, item: TurnInput) -> None:
        """ "hamma loyihalarga topic och": the start line now; the topics (paced, ≈ 2 s each) in a background job, so
        nothing else waits; then how many opened."""
        wanted = turns.topics_wanted(await projects.list_projects(self.deps))
        minutes = max(1, round(len(wanted) * turns_domain.OPEN_ALL_TOPICS_PAUSE_S / 60))
        await self._reply(item, S.TOPICS_ALL_STARTED(count=len(wanted), minutes=minutes), kind=Kind.NOTICE)

        async def open_them() -> None:
            done = await turns.open_all_topics(self.deps, wanted)
            if done is None:
                text = S.TOPICS_ALL_UNAVAILABLE()
            else:
                lines = [S.TOPICS_ALL_DONE(opened=done.opened, existing=done.existing)]
                if done.failed:
                    lines.append(S.TOPICS_ALL_SOME_FAILED(failed=done.failed))
                text = join(lines)
            await self._reply(item, text, kind=Kind.NOTICE, ref=f"burst-{item.burst_id}-topics")

        job = jobs.run(self.deps, self.health, open_them(), job="open_all_topics", chat_id=item.chat_id)
        aio.spawn(f"open_all_topics_{item.burst_id}", job)

    async def _names_a_project(self, text: str) -> bool:
        return bool(routing_domain.find_mentions(text, await projects.list_projects(self.deps)).named)

    # --- 3. a clarification answered ----------------------------------------------------------------------------
    async def _resolved(self, item: TurnInput, resolution: Resolution) -> None:
        """Routing resolved its project question with this burst: deliver the burst it asked about to the pick."""
        picked = resolution.choice is ChoiceKind.PICKED and resolution.project_id is not None
        asked = await burst.get_turn_input(self.deps, resolution.burst_id) if resolution.burst_id is not None else None
        if picked and resolution.intent is Routed.ANSWER and asked is not None and resolution.project_id is not None:
            # `dev/questions` asked which project's question an answer was for: it sends its own confirmation.
            await questions.answer_clarified(self.deps, self.turns, item, asked=asked, project_id=resolution.project_id)
            return
        project, intent = resolution.project_id, resolution.intent or Routed.INSTRUCT
        kind = _TURN_KINDS.get(intent)
        echo_ref = f"clarified-{resolution.clarification_id}"
        if picked and asked is not None and project is not None and kind is not None:
            # Work: its echo goes to the project's topic, and where he answered gets one line pointing there.
            images = await turns.save_images(self.deps, asked)
            request = turns_domain.request_from_input(
                asked, project, kind=kind, route_id=resolution.route_id, images=images
            )
            await turns.submit_routed(
                self.deps, self.turns, request, echo=resolution.reply, ref=echo_ref, from_thread=item.thread_id
            )
            return
        await self._reply(item, resolution.reply, kind=Kind.ECHO, ref=echo_ref)
        if not picked or asked is None or project is None:
            return
        if intent in digest_domain.REPORT_INTENTS:
            await self._report(asked, routing_domain.item_text(asked), project, intent, route_id=resolution.route_id)
        elif intent in _ROUTED_LATER:
            await self._reply(asked, S.NOT_YET(phase=intents.LATER_PHASE), kind=Kind.NOTICE)

    # --- 4. answers to open items -------------------------------------------------------------------------------
    async def _answered(self, item: TurnInput, origin: OwnerOrigin, where: Where) -> bool:
        """A wrong-target revert offer, a gate decision or an agent question took the burst. In a project's topic,
        that project counts as named: a bare answer there is for its items only."""
        named = routing_domain.find_mentions(item.own_text, await projects.list_projects(self.deps)).named
        if not named and where.project is not None:
            named = (where.project,)
        others = bool(await questions.open_items(self.deps)) or bool(await gate.pending_items(self.deps))
        reply = RevertReply.of(item, mentioned_projects=named, others_pending=others)  # own words only (T33)
        revert = await turns.answer_revert(self.deps, origin, reply)
        if revert.outcome is not RevertOutcome.NOT_FOR_ME:
            if await burst.cancel_burst(self.deps, item.burst_id) and revert.reply is not None:
                await self._reply(item, revert.reply, kind=Kind.NOTICE)
            return True
        answer = await questions.answer_owner(self.deps, self.turns, origin, item, topic_project=where.project)
        if answer is None:
            return False
        if answer.outcome is AnswerOutcome.ASKED and answer.clarification_id is not None and answer.message_key:
            self._link_when_sent(answer.clarification_id, answer.message_key)
        return True

    # --- 6. "bu X uchun edi" ------------------------------------------------------------------------------------
    async def _corrected(self, item: TurnInput, correction: Correction) -> None:
        snap = await settings.snapshot(self.deps)
        await outbox.enqueue_reply(
            self.deps,
            chat_id=snap[defaults.ACCESS_DEV_GROUP_ID],
            text=correction.reply,
            kind=Kind.ECHO,
            ref=f"correction-{correction.route_id or correction.to_project}-{correction.burst_id}",
            max_parts=snap[defaults.OUTBOX_MAX_PARTS],
            about=About(project_id=correction.to_project),
            thread=Thread(item.thread_id),
        )
        if correction.kind is CorrectionKind.MOVED and correction.from_project is not None:
            await turns.correct_wrong_target(
                self.deps,
                self.turns,
                from_project=correction.from_project,
                to_project=correction.to_project,
                burst_id=correction.burst_id,
            )

    # --- 7. routing ---------------------------------------------------------------------------------------------
    async def _route(self, item: TurnInput, facts: RouteFacts, where: Where) -> None:
        if where.project is not None:  # written in the project's own topic: its work, no routing model
            images = await turns.save_images(self.deps, item)
            await turns.route_topic(self.deps, self.turns, item, where.project, images=images)
            return
        text = routing_domain.item_text(item)
        snap = await settings.snapshot(self.deps)
        if servers_domain.mentioned_hosts(text, tuple(snap[defaults.SERVERS_HOSTS])):
            await self._route_server(item, facts, text)
            return
        images = await turns.save_images(self.deps, item)
        routed_turn = await turns.route_turn(self.deps, self.turns, item, facts, images=images)
        routed = routed_turn.routed
        if routed is None or routed_turn.submitted is not None:
            return  # handled by another pass, or delivered with its "→ X"
        decision = routed.decision
        if decision.outcome is Outcome.ASK:
            await self._ask(item, routed)
        elif decision.intent is Routed.SERVER_STATUS:
            self._server_status(item, text, ref=f"route-{routed.route_id}")
        elif decision.intent is Routed.CHAT:
            self._chat(item)
        elif decision.intent in digest_domain.REPORT_INTENTS:
            question = digest_domain.question_of(item, decision, route_id=routed.route_id)
            await self._answer_report(item, question)
        elif decision.intent in _ROUTED_LATER:
            await self._reply(item, S.NOT_YET(phase=intents.LATER_PHASE), kind=Kind.NOTICE)

    async def _ask(self, item: TurnInput, routed: RoutedDecision) -> None:
        """Routing's one question ("BonviHR'gami yoki HRMentor'gami?"); a reply to it binds once it is sent."""
        if routed.reply is None:
            return
        key = await self._reply(item, routed.reply, kind=Kind.QUESTION, ref=f"route-{routed.route_id}")
        if routed.clarification_id is not None:
            self._link_when_sent(routed.clarification_id, key)

    async def _route_server(self, item: TurnInput, facts: RouteFacts, text: str) -> None:
        """The words name a watched server (`servers.hosts`): its status comes from `dev/servers` (cached ≤ 30 min,
        else a fresh read-only check), and "why is it failing" becomes a read-only server investigation in the
        project's session (T31). Plain work on a project that mentions a server still goes to the project."""
        routed = await routing.route_burst(self.deps, item, facts)
        if routed is None:
            return
        decision, ref = routed.decision, f"route-{routed.route_id}"
        if decision.outcome is Outcome.ASK:
            await self._ask(item, routed)
        elif decision.intent is Routed.CHAT:  # a server named in passing ("bonvi-srv nima degani?")
            self._chat(item)
        elif decision.intent is Routed.INVESTIGATE and decision.project_id is not None:
            await servers.investigate_server(
                self.deps,
                self.turns,
                project_id=decision.project_id,
                question=text,
                chat_id=item.chat_id,
                ref=ref,
                burst_id=item.burst_id,
                from_thread=item.thread_id,
            )
        elif decision.intent in {Routed.SERVER_STATUS, Routed.INVESTIGATE, *digest_domain.REPORT_INTENTS}:
            self._server_status(item, text, ref=ref)
        elif (kind := _TURN_KINDS.get(decision.intent)) is not None and decision.project_id is not None:
            images = await turns.save_images(self.deps, item)
            merge = decision.outcome is Outcome.MERGE
            request = turns_domain.request_from_input(
                item, decision.project_id, kind=kind, merge=merge, route_id=routed.route_id, images=images
            )
            await turns.submit_routed(
                self.deps, self.turns, request, echo=routed.reply, ref=ref, from_thread=item.thread_id
            )
        elif decision.intent in _ROUTED_LATER:
            await self._reply(item, S.NOT_YET(phase=intents.LATER_PHASE), kind=Kind.NOTICE)

    def _chat(self, item: TurnInput) -> None:
        """Routing's `chat` (owner, 2026-10-24: talk like a plain assistant, not only about projects): `dev/chat`
        answers with no project, no turn and no report. A background job, as a server check is: the answer takes
        seconds, and a "to'xta" behind it must not wait. The typing hint stays up until the answer is sent."""
        answer = chat.answer_chat(self.deps, item)
        aio.spawn(f"chat_{item.burst_id}", jobs.run(self.deps, self.health, answer, job="chat", chat_id=item.chat_id))

    def _server_status(self, item: TurnInput, text: str, *, ref: str) -> None:
        """A background job: a fresh check can take up to `timeouts.server_check_s`, and the router must not wait
        (a "to'xta" behind it would). `dev/servers` sends "Tekshiryapman" first and the answer when it has it."""
        job = servers.answer_server_status(self.deps, chat_id=item.chat_id, ref=ref, text=text)
        aio.spawn(f"server_status_{item.burst_id}", jobs.run(self.deps, self.health, job, job="server_status"))

    async def _report(
        self, item: TurnInput, text: str, project: str | None, intent: Routed, *, route_id: int | None = None
    ) -> None:
        question = ReportQuestion(
            text=text,
            project_id=project,
            intent=intent,
            burst_id=item.burst_id,
            route_id=route_id,
            thread_id=item.thread_id,
        )
        await self._answer_report(item, question)

    async def _answer_report(self, item: TurnInput, question: ReportQuestion) -> None:
        answer = await digest.answer_report(self.deps, self.digest, question)
        if answer.needs_server_check:  # the digest has no server reader (T30 follow-up): `dev/servers` answers
            self._server_status(item, question.text, ref=f"report-{item.burst_id}")


_REDIRECT_SPLIT: Final = (":", "—", " - ")


def _redirected(text: str) -> str:
    """The new instruction of "yo'q, boshqacha qil: X": what follows the first colon (or dash)."""
    for mark in _REDIRECT_SPLIT:
        _head, found, rest = text.partition(mark)
        if found and rest.strip():
            return rest.strip()
    return ""
