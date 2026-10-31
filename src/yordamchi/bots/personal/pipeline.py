"""The personal bot's chat pipeline: every closed burst, in order, through the deterministic pre-parse of SPEC §6.1,
then the assistant (SPEC §3.2.13, §3.2.14; UC-03, UC-06, UC-07, UC-09, UC-30–UC-34, UC-36).

Each burst takes exactly one of these paths, first match wins:
1. the self-test (a synthetic update: P2 probation; recorded, nothing else);
2. the interrupt words ("to'xta", "bas", "stop"): the answer being written stops, its voice note too (≤ 3 s);
   "hammasini to'xtat" also drops the questions waiting behind it. "yo'q, boshqacha qil: …" stops it and asks the
   new question. "kut" stops nothing;
3. "ha" / "yo'q" while one settings change waits for the owner's word (`ask_change`, SPEC §3.2.15 step 3); any other
   message drops the waiting change. Likewise "ikkinchisi" / "2" while voice samples wait (UC-34), and the OAuth
   page address the owner pastes back while a sign-in waits (SPEC §3.2.10 step 3; intake vaulted it);
4. the typed aliases and exact phrases of `bots/personal/intents.py`: `holat?`, the settings, "xarajat?", the hello,
   the integrations list and removal; the news and the voice samples wait in the work line;
5. everything else is **work**, done in arrival order by one worker so the conversation stays in order: its media
   are read first (`media.read_burst`: videos and links with "Ko'ryapman", photos saved for the image blocks), then
   `qa.answer` writes the text and its voice note. "to'liqroq" and "yangi suhbat" wait in the same line, so they act
   on the answer before them. The assistant gets the `yordamchi_media` tools for this answer (`media.toolset`), whose
   artefact jobs (a screenshot, a file, a video) run beside it and send their files when ready. Files the owner sent
   are read beside the media (`files.read_burst`, UC-37: texts, PDFs, audio transcripts), and the documents the
   assistant made with `yordamchi_files` (`files.toolset`) are queued after its answer. The owner's integrations
   are the `yordamchi_int` tools (`gateway.toolset`, T76): its connect tools take only a key that came in his own
   words in this burst.

Commands, interrupts and answers read only the owner's own words (`TurnInput.own_text`): a message he forwarded is
material, never an order (T33). A burst is marked done (`burst.cancel_burst`: the bot handled it itself) once its
path finished; a crash before that leaves it `closed`, so the restart pass answers it once more (SPEC §2.7), and the
assistant's own idempotency (`answers.burst_id`) keeps an answer that was already sent from going out twice. One that
failed is not taken again by this process (`guarded()` told the owner). Nothing here awaits a model directly: each
step is one feature call.
"""

import asyncio
from collections.abc import Coroutine
from dataclasses import dataclass, field, replace
from functools import partial
from typing import Final

from yordamchi.bots.personal import intents, jobs
from yordamchi.bots.personal.intents import Answer, Command, Intent
from yordamchi.core import aio
from yordamchi.core import strings as S
from yordamchi.core.log import get_log
from yordamchi.core.text import BotText, join
from yordamchi.personal.deps import PersonalDeps
from yordamchi.personal.files import domain as files_domain
from yordamchi.personal.files import service as files
from yordamchi.personal.gateway import service as gateway
from yordamchi.personal.media import service as media
from yordamchi.personal.news import service as news
from yordamchi.personal.qa import domain as qa_domain
from yordamchi.personal.qa import service as qa
from yordamchi.ports.assistant import AssistantToolset
from yordamchi.shared.access import service as access
from yordamchi.shared.access.domain import OwnerOrigin
from yordamchi.shared.burst import service as burst
from yordamchi.shared.burst.domain import InterruptKind, TurnInput
from yordamchi.shared.cost import service as cost
from yordamchi.shared.health import service as health
from yordamchi.shared.health.service import HealthState
from yordamchi.shared.inbox import service as inbox
from yordamchi.shared.inbox.domain import UpdateKind
from yordamchi.shared.integrations import service as integrations
from yordamchi.shared.integrations.service import IntegrationsState
from yordamchi.shared.outbox import service as outbox
from yordamchi.shared.outbox.domain import Kind
from yordamchi.shared.settings import defaults
from yordamchi.shared.settings import service as settings
from yordamchi.shared.settings.domain import PendingChange, Proposal
from yordamchi.shared.vault import service as vault
from yordamchi.shared.voice import service as voice
from yordamchi.shared.voice.domain import VoiceSample

log = get_log(__name__)

BATCH: Final = 50  # closed bursts read per pass; the pass repeats until none is left
# A command's own words: a text, or a voice note's transcript. A burst with more parts is work.
_COMMAND_KINDS: Final = frozenset({UpdateKind.TEXT, UpdateKind.VOICE, UpdateKind.EDITED})
OAUTH_CALLBACK_KIND: Final = "oauth_callback_url"  # the vault kind intake gives a pasted OAuth redirect (core.redact)


@dataclass(frozen=True, slots=True)
class _Waiting:
    """One weakening settings change asked about in the chat; "ha" applies it."""

    number: int
    change: PendingChange


@dataclass(slots=True)
class Pipeline:
    """Built once by `bots/personal/runtime.py`."""

    deps: PersonalDeps
    health: HealthState
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    work: asyncio.Queue[TurnInput] = field(default_factory=asyncio.Queue)
    taken: set[int] = field(default_factory=set)  # bursts this process took and has not finished
    current: asyncio.Task[None] | None = None  # the work being done (an answer, "to'liqroq")
    stopping: bool = False  # SIGTERM: work cut off now stays `closed` for the next start
    waiting: _Waiting | None = None
    asked: int = 0  # numbers the settings questions of this process
    integrations: IntegrationsState = field(default_factory=IntegrationsState)  # OAuth sign-ins waiting for a paste
    samples: tuple[VoiceSample, ...] = ()  # voice samples waiting for the owner's pick (UC-34)

    def poke(self) -> None:
        """A burst closed: run a pass now."""
        self.wake.set()

    # --- the router loop ----------------------------------------------------------------------------------------
    async def run(self) -> None:
        """The long-lived loop (`aio.forever("router", …)`): a pass at start (closed bursts a restart left: SPEC §2.7
        "route now"), then one after every `poke()`."""
        while True:
            self.wake.clear()
            await self.drain()
            await self.wake.wait()

    async def drain(self) -> int:
        """Handle every closed burst not taken yet, interrupts first, one at a time, each under `guarded()`."""
        handled = 0
        while True:
            closed = await burst.list_closed_bursts(self.deps, limit=BATCH + len(self.taken))
            items = [item for item in closed if item.burst_id not in self.taken]
            if not items:
                return handled
            for item in items:
                self.taken.add(item.burst_id)
                await jobs.run(self.deps, self.health, self.handle(item), job="personal_burst", chat_id=item.chat_id)
                handled += 1

    async def handle(self, item: TurnInput) -> None:
        """One closed burst down the path of the module docstring."""
        if item.synthetic:
            await self._done(item)  # the P2 self-test: nothing to answer
            return
        if (kind := item.interrupt) is not None:
            await self._interrupt(item, kind)
            return
        origin = await inbox.origin_of_update(self.deps, item.parts[-1].update_id)
        command = self._command_of(item)
        if self.waiting is not None and await self._waiting_answer(item, origin):
            return
        if self.samples and await self._sample_pick(item, origin):
            return
        if self.integrations.flows and origin is not None and await self._oauth_paste(item, origin):
            return
        if command is not None and await self._command(item, command, origin):
            return
        self.work.put_nowait(item)

    async def _done(self, item: TurnInput) -> None:
        """The burst is handled: never taken again, by this process or after a restart."""
        await burst.cancel_burst(self.deps, item.burst_id)
        self.taken.discard(item.burst_id)

    async def _reply(self, item: TurnInput, text: BotText, *, kind: Kind = Kind.REPORT, ref: str = "") -> None:
        snap = await settings.snapshot(self.deps)
        await outbox.enqueue_reply(
            self.deps,
            chat_id=item.chat_id,
            text=text,
            kind=kind,
            ref=ref or f"burst-{item.burst_id}",
            max_parts=snap[defaults.OUTBOX_MAX_PARTS],
        )

    @staticmethod
    def _command_of(item: TurnInput) -> Command | None:
        """A one-part burst in the owner's own words (a forwarded "holat?" is material, never a command; T33)."""
        if len(item.parts) != 1 or item.parts[0].kind not in _COMMAND_KINDS:
            return None
        return intents.command_of(item.own_text)

    # --- 2. interrupts (SPEC §3.2.5) ----------------------------------------------------------------------------
    async def _interrupt(self, item: TurnInput, kind: InterruptKind) -> None:
        if kind is InterruptKind.HOLD:
            await self._done(item)  # "kut" an older release closed as an interrupt: it stops nothing
            return
        stopped = self.stop_current()
        if kind is InterruptKind.REDIRECT:
            self.work.put_nowait(item)  # "yo'q, boshqacha qil: …" is the new question, words and all
            return
        if kind is InterruptKind.STOP_ALL:  # "hammasini to'xtat": the questions waiting behind it go too
            stopped = await self._drop_waiting_work() or stopped
        await self._done(item)
        await self._reply(item, S.PERSONAL_STOPPED() if stopped else S.PERSONAL_NOTHING_TO_STOP(), kind=Kind.NOTICE)

    async def _drop_waiting_work(self) -> bool:
        dropped = 0
        while not self.work.empty():
            await self._done(self.work.get_nowait())
            dropped += 1
        log.info("personal_work_dropped", dropped=dropped)
        return dropped > 0

    def stop_current(self) -> bool:
        """Stop the answer being written (its voice note too). False when there was none."""
        task = self.current
        if task is None or task.done():
            return False
        task.cancel()
        log.info("personal_work_stopped")
        return True

    # --- 3. a waiting settings change ---------------------------------------------------------------------------
    async def ask_change(self, change: PendingChange) -> int:
        """`settings.AskOwner` of the personal bot: ONE question in the chat; the next "ha" applies the change. Kept in
        memory: a restart forgets it, and the owner says it again (ASSUMPTIONS 2026-10-24)."""
        self.asked += 1
        snap = await settings.snapshot(self.deps)
        chat_id = await access.resolve_home_chat(self.deps)
        self.waiting = _Waiting(number=self.asked, change=change)
        await outbox.enqueue_reply(
            self.deps,
            chat_id=chat_id,
            text=S.SETTING_WEAKEN_QUESTION(decision_id=self.asked, change=change.restated),
            kind=Kind.DECISION,
            ref=f"settings-ask-{change.origin_update_id}",  # unique across restarts (the counter is not)
            max_parts=snap[defaults.OUTBOX_MAX_PARTS],
        )
        log.info("personal_change_asked", number=self.asked)
        return self.asked

    async def _waiting_answer(self, item: TurnInput, origin: OwnerOrigin | None) -> bool:
        """The owner's word on the waiting change; any other message drops it. True when the burst was the answer."""
        waiting, self.waiting = self.waiting, None
        if waiting is None:
            return False
        answer = intents.answer_of(item.own_text) if _one_text(item) else None
        if answer is None or origin is None:
            dropped = S.PERSONAL_CHANGE_DROPPED(number=waiting.number)
            await self._reply(item, dropped, kind=Kind.NOTICE, ref=f"settings-dropped-{item.burst_id}")
            log.info("personal_change_dropped", number=waiting.number)
            return False
        await self._done(item)
        if answer is Answer.YES:
            await settings.confirm_change(self.deps, origin, waiting.change)
        else:
            declined = S.SETTING_CHANGE_DECLINED(decision_id=waiting.number, change=waiting.change.restated)
            await self._reply(item, declined, kind=Kind.NOTICE)
        log.info("personal_change_answered", number=waiting.number, approved=answer is Answer.YES)
        return True

    async def _sample_pick(self, item: TurnInput, origin: OwnerOrigin | None) -> bool:
        """The owner's pick among the voice samples; any other message leaves the voice as it is."""
        samples, self.samples = self.samples, ()
        number = intents.sample_choice_of(item.own_text) if _one_text(item) else None
        sample = next((sample for sample in samples if sample.number == number), None)
        if sample is None or origin is None:
            await self._reply(item, S.VOICE_SAMPLE_DROPPED(), kind=Kind.NOTICE, ref=f"samples-dropped-{item.burst_id}")
            log.info("voice_samples_dropped", offered=len(samples))
            return False
        await self._done(item)
        proposals = tuple(Proposal(key=key, value=value) for key, value in sample.changes)
        await settings.apply_proposals(self.deps, origin, proposals, ask=self.ask_change)  # replies itself
        log.info("voice_sample_picked", number=sample.number, provider=sample.provider)
        return True

    async def _oauth_paste(self, item: TurnInput, origin: OwnerOrigin) -> bool:
        """The sign-in page address the owner pasted back (intake vaulted it): the waiting OAuth flow completes."""
        ids = {secret_id for part in item.own_parts for secret_id in part.content.secret_ids}
        if not ids:
            return False
        refs = [ref for ref in await vault.list_secrets(self.deps) if ref.id in ids and ref.kind == OAUTH_CALLBACK_KIND]
        if not refs:
            return False
        await self._done(item)
        reply = await integrations.complete_oauth(self.deps, self.integrations, origin, secret=refs[-1])
        await self._reply(item, reply, kind=Kind.NOTICE)
        return True

    # --- 4. commands (bots/personal/intents.py) -----------------------------------------------------------------
    async def _command(self, item: TurnInput, command: Command, origin: OwnerOrigin | None) -> bool:
        """Carry out one recognised command. False when it needs the owner's authority and there is none: the
        assistant then reads the message like any other."""
        if command.needs_origin and origin is None:
            return False
        log.info("command_matched", burst_id=item.burst_id, intent=command.intent.value)
        if command.conversation:
            self.work.put_nowait(item)  # behind the answer being written
            return True
        await self._done(item)
        if not command.built:
            await self._reply(item, S.NOT_YET(phase=command.phase), kind=Kind.NOTICE)
        elif origin is not None and command.needs_origin:
            await self._owner_command(item, command, origin)
        else:
            await self._plain_command(item, command)
        return True

    async def _plain_command(self, item: TurnInput, command: Command) -> None:
        deps = self.deps
        if command.intent is Intent.SHOW_HEALTH:
            await self._reply(item, await health.show_health(deps, self.health))
        elif command.intent is Intent.SHOW_COST:  # "xarajat?" (UC-07): queued as a reply to the question itself
            await cost.show_cost(
                deps, chat_id=item.chat_id, ref=f"burst-{item.burst_id}", reply_to=item.parts[0].message_id
            )
        elif command.intent is Intent.HELP:
            await self._reply(item, S.PERSONAL_HELLO())

    async def _owner_command(self, item: TurnInput, command: Command, origin: OwnerOrigin) -> None:
        deps, intent = self.deps, command.intent
        if intent is Intent.INTEGRATIONS:
            listed = await integrations.show_integrations(deps)
            await self._reply(item, join((listed, S.PERSONAL_INTEGRATION_HELP())))
        elif intent is Intent.INTEGRATION_REMOVE:
            removed = await integrations.remove_integration(deps, self.integrations, origin, command.argument)
            await self._reply(item, removed, kind=Kind.NOTICE)
        elif intent is Intent.INTEGRATION_RESTORE:
            await self._reply(item, await integrations.restore_integration(deps, origin, command.argument))
        elif intent is Intent.INTEGRATION_RECONNECT:
            reconnected = await integrations.reconnect_integration(deps, self.integrations, origin, command.argument)
            await self._reply(item, reconnected, kind=Kind.NOTICE)
        elif intent is Intent.SHOW_SETTINGS:
            await settings.show_settings(deps, origin)  # queues the summary and the full list itself
        elif intent is Intent.CHANGE_SETTING:  # queues its reply itself
            await settings.handle_request(deps, origin, command.argument or item.own_text, ask=self.ask_change)
        elif intent is Intent.REVERT_SETTING:
            await settings.revert_setting(deps, origin, command.argument or None, ask=self.ask_change)
        elif intent is Intent.ADD_TERM:
            await self._reply(item, await voice.add_owner_term(deps, origin, command.argument), kind=Kind.NOTICE)

    # --- 5. work, in arrival order ------------------------------------------------------------------------------
    async def run_work(self) -> None:
        """The long-lived worker (`aio.forever("answers", …)`): one burst at a time, each as its own task so
        "to'xta" can stop it without stopping the worker."""
        while True:
            item = await self.work.get()
            task = aio.spawn(f"answer_{item.burst_id}", self._work(item))
            self.current = task
            await asyncio.wait({task})  # a cancelled worker leaves `current` set, for `stop()` to cancel
            self.current = None
            if task.cancelled() and not self.stopping:  # "to'xta": the owner is done with it
                await jobs.run(self.deps, self.health, self._done(item), job="answer_stopped", chat_id=item.chat_id)

    async def _work(self, item: TurnInput) -> None:
        finished = await jobs.run(self.deps, self.health, self._do(item), job="answer", chat_id=item.chat_id)
        if finished is not None:
            await self._done(item)

    async def _do(self, item: TurnInput) -> bool:
        """One piece of work; True once it is done (what failed was told by `guarded()` and stays `closed`)."""
        command = self._command_of(item)
        intent = command.intent if command is not None else None
        if intent is Intent.MORE:
            await qa.more(self.deps, item.chat_id)
        elif intent is Intent.NEW_CONVERSATION:
            await qa.new_conversation(self.deps, item.chat_id)
        elif intent is Intent.VOICE_SAMPLES:
            self.samples = await voice.offer_samples(self.deps, chat_id=item.chat_id, ref=f"samples-{item.burst_id}")
        else:
            await self._answer(item, news_request=intent is Intent.NEWS)
        return True

    async def _answer(self, item: TurnInput, *, news_request: bool = False) -> None:
        """Media and files first (UC-31: "Ko'ryapman", then the material; UC-37: texts, PDFs, transcripts), then the
        assistant's text and voice (UC-30), then the documents it made. A news request (UC-33) is asked with the
        news brief around the owner's words."""
        found = await media.read_burst(self.deps, item)
        read = await files.read_burst(self.deps, item)
        for index, text in enumerate(dict.fromkeys((*found.failures, *read.failures))):
            await self._reply(item, text, kind=Kind.NOTICE, ref=f"media-{item.burst_id}-{index}")
        question = await news.news_question(self.deps, item) if news_request else qa_domain.question_of(item)
        files_only_failed = bool(read.failures) and question.empty and not found.material and not found.images
        if not read.read and (found.only_failed or files_only_failed):
            return  # nothing to answer from and nothing else asked: the failure notes were the reply (UC-31 F)
        question = replace(question, material=(*found.material, *read.material))
        made = files.MadeDocuments()
        turn = gateway.Turn(
            chat_id=item.chat_id,
            ref=f"qa-{item.burst_id}",
            origin=await inbox.origin_of_update(self.deps, item.parts[-1].update_id),
            own_secret_ids=frozenset(secret_id for part in item.own_parts for secret_id in part.content.secret_ids),
        )
        tools = await self.answer_tools(item.chat_id, ref=turn.ref, made=made, turn=turn)
        try:
            await qa.answer(self.deps, question, images=found.images, documents=read.pdfs, toolsets=tools)
        finally:
            await files.send_documents(
                self.deps, made, chat_id=item.chat_id, ref=f"doc-{item.burst_id}", reply_to=item.own_reply_to
            )

    async def answer_tools(
        self,
        chat_id: int,
        *,
        ref: str,
        made: files.MadeDocuments | None = None,
        turn: gateway.Turn | None = None,
    ) -> tuple[AssistantToolset, ...]:
        """Every in-process toolset an answer gets: `yordamchi_media`, `yordamchi_files` and `yordamchi_int`. `made`
        collects the documents of this answer and `turn` says who may connect what; a probe or warm-up passes
        neither (nothing is sent from those, and their connect tools refuse)."""
        snap = await settings.snapshot(self.deps)
        documents = files.toolset(
            self.deps,
            made=made if made is not None else files.MadeDocuments(),
            limits=files_domain.document_limits(snap),
        )
        owned = await gateway.toolset(
            self.deps, self.integrations, turn if turn is not None else gateway.Turn(chat_id=chat_id, ref=ref)
        )
        return (await self.media_tools(chat_id, ref=ref), documents, owned)

    async def media_tools(self, chat_id: int, *, ref: str) -> AssistantToolset:
        """This answer's `yordamchi_media` tools; their artefact jobs run beside it under `guarded()`."""
        snap = await settings.snapshot(self.deps)
        return media.toolset(
            self.deps,
            chat_id=chat_id,
            ref=ref,
            start=partial(self._start_job, chat_id),
            media_timeout_s=snap[defaults.TIMEOUTS_MEDIA_S],
        )

    def _start_job(self, chat_id: int, name: str, job: Coroutine[object, object, None]) -> None:
        aio.spawn(name, jobs.run(self.deps, self.health, job, job=name, chat_id=chat_id))

    async def stop(self) -> None:
        """Shutdown, after the worker loop was cancelled: the answer being written stops (SPEC §2.6); its burst stays
        `closed`, so the next start answers it."""
        self.stopping = True
        task = self.current
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


def _one_text(item: TurnInput) -> bool:
    return len(item.parts) == 1 and item.parts[0].kind in _COMMAND_KINDS
