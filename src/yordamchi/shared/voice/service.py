"""Speech to text for voice notes, and the speech keys behind it (SPEC §3.2 step 5, §2.7, §3.2.11; UC-05).

One STT job per voice part that the burst lists in `events.transcribe`, live or after a restart (`recover_bursts`):
1. a note whose row is already `done` (the old process died before handing the text on) reuses that text;
2. otherwise the note gets a `pending` row, and `_transcribe` downloads it, orders the providers (`domain.pick_stt`)
   and asks each in turn with this job's settings: languages, the vocabulary, model, mode and timeout. A quota, key,
   rate-limit or availability failure moves on to the next provider that has a key (`domain.falls_back`);
3. the row becomes `done` or `failed`, and an answered call's cost goes to `usage_records`, in one unit of work;
4. `burst.service.record_transcript` releases the burst's hold with the text, or with the "ovoz tushunilmadi"
   marker; then `wake` is set for the burst loop, and any burst that closed is handed to `notify` (the router);
5. a failed note is never silent: the owner is told why (UC-05 F).

The vocabulary is the owner's terms (`stt.extra_terms`, added by chat with `add_owner_term`) plus the names that the
bot's `named` sources return. `shared` cannot import `dev` (C1), so the dev bot passes its project registry in:
`named=(functools.partial(projects.service.list_projects, deps),)`; `Project` is a `domain.Named` as it is.

Speech keys (SPEC §3.2.11): `configure_speech_keys` checks each new key through the port, keeps one key per provider,
and tells the owner in one line what is active. `read_speech_key` is the `api_key` callable each STT adapter reads
the vault through.
"""

import asyncio
from collections.abc import Awaitable, Callable, Iterable, Sequence
from datetime import datetime

from yordamchi.core import redact
from yordamchi.core import strings as S
from yordamchi.core.errors import ErrorCode, ExternalError
from yordamchi.core.log import get_log
from yordamchi.core.text import BotText
from yordamchi.persistence import bulk
from yordamchi.shared.access.domain import OwnerOrigin
from yordamchi.shared.burst import service as burst
from yordamchi.shared.burst.domain import BurstPart
from yordamchi.shared.cost import service as cost
from yordamchi.shared.deps import SharedDeps
from yordamchi.shared.settings import defaults
from yordamchi.shared.settings import service as settings
from yordamchi.shared.settings.domain import Snapshot
from yordamchi.shared.vault import service as vault
from yordamchi.shared.vault.domain import SecretRef
from yordamchi.shared.voice import domain, store
from yordamchi.shared.voice.domain import (
    Attempt,
    FailReason,
    KeyOutcome,
    KeyVerdict,
    Named,
    SpeechKey,
    SttResult,
    SttStatus,
)

log = get_log(__name__)

# Hands the bot's registry to the vocabulary: the dev bot's projects, later the integrations.
type NamedSource = Callable[[], Awaitable[Iterable[Named]]]


# --- transcription --------------------------------------------------------------------------------------------------
async def transcribe_parts(
    deps: SharedDeps,
    parts: Sequence[BurstPart],
    *,
    wake: asyncio.Event,
    notify: burst.Notify,
    named: Sequence[NamedSource] = (),
) -> tuple[SttResult, ...]:
    """The STT jobs of `events.transcribe`, concurrently; results in the given order. The burst loop's `notify`
    starts this (live, and with the parts `recover_bursts` lists after a restart) without awaiting it. Provider
    failures never raise; anything that does is a bug, and it cancels the sibling jobs (TaskGroup), whose bursts
    then close when `burst.hold_max_s` runs out."""
    async with asyncio.TaskGroup() as group:
        jobs = [group.create_task(transcribe_part(deps, part, wake=wake, notify=notify, named=named)) for part in parts]
    return tuple(job.result() for job in jobs)


async def transcribe_part(
    deps: SharedDeps,
    part: BurstPart,
    *,
    wake: asyncio.Event,
    notify: burst.Notify,
    named: Sequence[NamedSource] = (),
) -> SttResult:
    """One voice note to text, recorded and handed to its burst; see the module docstring. Provider failures never
    raise: they end as a `failed` row, the marker in the burst and a message to the owner."""
    started = deps.clock.now()
    snap = await settings.snapshot(deps)
    async with deps.db.read() as s:
        row = await store.get_transcript(s, bot=deps.bot.value, update_id=part.update_id)
    done_text = row.text if row is not None and row.status == SttStatus.DONE.value else None
    if done_text is not None:  # the old process transcribed it and died before the burst heard
        result = SttResult(part.update_id, text=done_text)
    else:
        async with deps.db.uow() as s:
            await store.add_transcript(
                s, bot=deps.bot.value, update_id=part.update_id, status=SttStatus.PENDING.value, at=started
            )
        result = await _transcribe(deps, snap, part, named)
        await _record(deps, result, latency_ms=_ms_since(deps, started))
    text = result.text if result.text is not None else domain.marker(result)
    events = await burst.record_transcript(deps, update_id=part.update_id, text=text, ok=result.ok)
    wake.set()
    if events:
        notify(events)
    if not result.ok:
        target = domain.notice_target(snap, part.message_id)
        await _tell_owner(deps, target.chat_id, domain.failure_notice(result), reply_to=target.reply_to)
    log.info(
        "voice_transcribed",
        bot=deps.bot.value,
        update_id=part.update_id,
        status=result.status.value,
        provider=result.provider,
        fallbacks=len(result.failed),
        error_code=result.error_code,
        reused=done_text is not None,
        duration_ms=_ms_since(deps, started),
    )
    return result


async def _transcribe(deps: SharedDeps, snap: Snapshot, part: BurstPart, named: Sequence[NamedSource]) -> SttResult:
    attachment = part.content.attachment
    if attachment is None:
        return SttResult(part.update_id, reason=FailReason.NO_AUDIO)
    order = domain.pick_stt(snap[defaults.SPEECH_STT_PROVIDER], await _usable_providers(deps))
    if not order:
        return SttResult(part.update_id, reason=FailReason.NO_KEY)
    timeout = snap[defaults.TIMEOUTS_STT_S]
    try:
        audio = await deps.messenger.download(attachment.file_id, max_bytes=domain.MAX_AUDIO_BYTES, timeout=timeout)
    except ExternalError as error:
        log.warning("voice_download_failed", update_id=part.update_id, error_code=error.code.value)
        return SttResult(part.update_id, reason=FailReason.DOWNLOAD)
    entries = [entry for source in named for entry in await source()]
    vocabulary = domain.build_vocabulary(snap[defaults.STT_EXTRA_TERMS], entries)
    failed: list[Attempt] = []
    for provider in order:
        try:
            transcript = await deps.transcribers[provider].transcribe(
                audio,
                mime=attachment.mime or domain.VOICE_MIME,
                languages=snap[defaults.STT_LANGUAGES],
                vocabulary=vocabulary,
                model=domain.stt_model(provider, snap),
                mode=snap[defaults.STT_MODE],
                fallback_model=snap[defaults.STT_OPENROUTER_FALLBACK_MODEL],
                timeout=timeout,
            )
        except ExternalError as error:
            failed.append(Attempt(provider=provider, code=error.code))
            log.warning("stt_provider_failed", update_id=part.update_id, provider=provider, error_code=error.code.value)
            if domain.falls_back(error.code):
                continue
            break
        return domain.transcribed(part.update_id, transcript, failed=failed)
    return SttResult(part.update_id, reason=FailReason.PROVIDER, failed=tuple(failed))


async def _record(deps: SharedDeps, result: SttResult, *, latency_ms: int) -> None:
    """The row's outcome and, for an answered call, its usage, in one unit of work."""
    now = deps.clock.now()
    transcript = result.transcript
    async with deps.db.uow() as s:
        await store.set_transcript_result(
            s,
            bot=deps.bot.value,
            update_id=result.update_id,
            status=result.status.value,
            text=None if result.text is None else redact.scrubbed(result.text),
            provider=result.provider,
            provider_path=None if transcript is None else transcript.provider_path,
            model=None if transcript is None else transcript.model,
            audio_ms=None if transcript is None else transcript.audio_ms,
            latency_ms=latency_ms,
            error_code=result.error_code,
            at=now,
        )
        if transcript is not None:
            await cost.record_usage(s, bot=deps.bot, at=now, usage=domain.usage_of(transcript))


async def _usable_providers(deps: SharedDeps) -> tuple[str, ...]:
    """Providers with a key in the vault and an adapter in this bot."""
    keys = domain.current_keys(await vault.list_secrets(deps))
    return tuple(provider for provider in keys if provider in deps.transcribers)


def _ms_since(deps: SharedDeps, started: datetime) -> int:
    """Milliseconds on `deps.clock`, so tests measure the job on virtual time."""
    return max(0, round((deps.clock.now() - started).total_seconds() * 1000))


async def _tell_owner(deps: SharedDeps, chat_id: int, text: BotText, *, reply_to: int | None) -> bool:
    """One notice to the owner. A failed send is logged, never raised: the outcome is already recorded."""
    try:
        await deps.messenger.send_text(chat_id, text, reply_to=reply_to, timeout=domain.NOTICE_TIMEOUT_S)
    except ExternalError as error:
        log.warning("voice_notice_failed", error_code=error.code.value)
        return False
    return True


# --- the owner's terms ----------------------------------------------------------------------------------------------
async def add_owner_term(deps: SharedDeps, origin: OwnerOrigin, text: str) -> BotText:
    """ "bu so'zni to'g'ri yoz: …" (UC-05): the term joins `stt.extra_terms`, so the next voice note spells it exactly.

    Raises `SttTermInvalid` (empty, or longer than `domain.TERM_MAX_CHARS`). A term already there changes nothing.
    """
    term = domain.normalised_term(text)
    snap = await settings.snapshot(deps)
    terms = domain.with_term(snap[defaults.STT_EXTRA_TERMS], term)
    if terms is None:
        return S.STT_TERM_KNOWN(term=term)
    await settings.record_version(deps, origin, defaults.STT_EXTRA_TERMS, settings.as_json(terms), reason="change")
    log.info("stt_term_added", bot=deps.bot.value, terms=len(terms))
    return S.STT_TERM_ADDED(term=term)


# --- speech keys (SPEC §3.2.11) -------------------------------------------------------------------------------------
async def read_speech_key(deps: SharedDeps, provider: str) -> str:
    """The key `provider`'s STT adapter uses now: the newest active vault secret of its kind. "" when there is none,
    which the adapter treats as `AuthFailed`. This is the adapter's `api_key` callable, so a new key applies to the
    next call without a restart."""
    name = domain.current_keys(await vault.list_secrets(deps)).get(provider)
    return "" if name is None else await vault.open_secret(deps, name)


async def configure_speech_keys(deps: SharedDeps, origin: OwnerOrigin, secrets: Sequence[SecretRef]) -> BotText | None:
    """The speech keys among the secrets intake just stored (`Accepted.secrets`): check each through its adapter
    (≤ `timeouts.stt_s`), keep one key per provider, and tell the owner in one line what is active now, including
    a rejected key, a used-up quota or a free tier (UC-05 F: never silent). Returns that line, or None when no
    speech key was among `secrets`.

    A rejected key is removed from the vault; a working one retires the older keys of its kind. Any other outcome
    (quota, rate limit, no answer) keeps every key, and the newest is used.
    """
    keys = domain.speech_keys(secrets)
    if not keys:
        return None
    snap = await settings.snapshot(deps)
    outcomes: list[KeyOutcome] = []
    for key in keys:
        outcome = await _check_key(deps, snap, key)
        outcomes.append(outcome)
        for name in domain.keys_to_retire(outcome, await vault.list_secrets(deps)):
            await vault.remove_secret(deps, origin, name)
    order = domain.pick_stt(snap[defaults.SPEECH_STT_PROVIDER], await _usable_providers(deps))
    text = domain.speech_status(outcomes, order)
    await _tell_owner(deps, origin.chat_id, text, reply_to=None)
    log.info(
        "speech_keys_configured",
        bot=deps.bot.value,
        verdicts=",".join(f"{outcome.key.provider}:{outcome.verdict.value}" for outcome in outcomes),
        stt=",".join(order),
    )
    return text


async def _check_key(deps: SharedDeps, snap: Snapshot, key: SpeechKey) -> KeyOutcome:
    model = domain.stt_model(key.provider, snap)
    transcriber = deps.transcribers.get(key.provider)
    if transcriber is None:  # a bot built without this provider's adapter: nothing to check with
        log.warning("speech_key_unsupported", provider=key.provider)
        return domain.key_failed(key, ErrorCode.NOT_INSTALLED, model=model)
    value = await vault.open_secret(deps, key.name)
    try:
        check = await transcriber.validate_key(value, models=(model,), timeout=snap[defaults.TIMEOUTS_STT_S])
    except ExternalError as error:
        outcome = domain.key_failed(key, error.code, model=model)
    else:
        outcome = domain.key_checked(key, check, model=model)
    if outcome.verdict is not KeyVerdict.ACTIVE:
        log.warning("speech_key_not_active", provider=key.provider, verdict=outcome.verdict.value)
    return outcome


# --- retention ------------------------------------------------------------------------------------------------------
async def apply_retention(deps: SharedDeps) -> int:
    """SPEC §4.2: a finished transcript's text is nulled after 7 days and its row deleted after 30, in batches. A
    `pending` row is never touched. Returns how many rows changed."""
    now = deps.clock.now()
    finished = sorted(status.value for status in domain.TERMINAL)
    row_cutoff, text_cutoff = domain.row_cutoff(now), domain.text_cutoff(now)
    deleted = await bulk.run_in_batches(
        deps.db, lambda s, limit: store.delete_transcripts_before(s, row_cutoff, statuses=finished, limit=limit)
    )
    cleared = await bulk.run_in_batches(
        deps.db, lambda s, limit: store.clear_texts_before(s, text_cutoff, statuses=finished, limit=limit)
    )
    log.info("voice_retention_applied", rows_deleted=deleted, texts_cleared=cleared)
    return deleted + cleared
