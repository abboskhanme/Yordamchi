"""Speech rules, pure (SPEC §3.2 step 5, §3.2.11; UC-05).

A voice note becomes text in this order:
1. `pick_stt` orders the providers that have a key in the vault and an adapter: under `auto` a direct Google key goes
   first, because it spells names better, and OpenRouter is its fallback; `speech.stt_provider` can put either first.
2. `build_vocabulary` lists the terms STT must spell exactly: the owner's own terms (`stt.extra_terms`, newest first),
   then every name and alias the bot's registries hand in as `Named` entries (the dev bot's projects). `shared` cannot
   import `dev` (C1), so the dev bot injects its registry; the personal bot has none yet.
3. The service asks each provider in turn; `falls_back` says which failures move on to the next one.
4. A note that failed stays in its burst as `marker(...)`, and the owner gets `failure_notice(...)` (UC-05 F).

A new speech key (SPEC §3.2.11) is judged by `key_checked` / `key_failed`; `keys_to_retire` says which vault entries
go, and `speech_status` is the one line the owner gets about what is active now.
"""

import itertools
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Final, Protocol

from yordamchi.core import strings as S
from yordamchi.core.errors import ErrorCode, ExternalError, SttTermInvalid
from yordamchi.core.paths import BotName
from yordamchi.core.text import BotText, join
from yordamchi.ports.speech import SpeechAudio, SpeechKeyCheck, Transcript
from yordamchi.shared.cost.domain import Provider, Usage, audio_cost_micros
from yordamchi.shared.settings import defaults
from yordamchi.shared.settings.domain import Snapshot
from yordamchi.shared.vault.domain import SecretRef

GOOGLE: Final = "google"
OPENROUTER: Final = "openrouter"
STT_ORDER: Final = (GOOGLE, OPENROUTER)  # SPEC §3.2.11 step 3, STT under `auto`: Google → OpenRouter → none
# The vault kind of each provider's key (the `core.redact` pattern); a second key of a kind is stored as `<kind>_2`.
KEY_KINDS: Final[Mapping[str, str]] = {GOOGLE: "google_api_key", OPENROUTER: "openrouter_api_key"}
VOCABULARY_LIMIT: Final = 1000  # Gemini `custom_vocabulary` takes ≤ 1,000 terms (SPEC §3.2 step 5)
TERM_MAX_CHARS: Final = 80
MAX_AUDIO_BYTES: Final = 20 * 1024 * 1024  # Telegram hands bots files of ≤ 20 MB
VOICE_MIME: Final = "audio/ogg"  # a Telegram voice note is OGG/Opus
# Bound on one notice to the owner. SPEC §4.3 has no key for it; T15's outbox takes these sends over.
NOTICE_TIMEOUT_S: Final = 5.0
USAGE_SOURCE: Final = "stt"
TEXT_KEEP: Final = timedelta(days=7)  # SPEC §4.2 `transcripts`: "text 7 d"
ROW_KEEP: Final = timedelta(days=30)  # "row 30 d"


class SttStatus(StrEnum):
    PENDING = "pending"  # the job started; a restart runs it again
    DONE = "done"
    FAILED = "failed"


TERMINAL: Final = frozenset({SttStatus.DONE, SttStatus.FAILED})


class FailReason(StrEnum):
    """Why a voice note has no transcript. Stored in `transcripts.error_code`, except `PROVIDER`, which stores the
    last provider's `ErrorCode` instead."""

    NO_KEY = "stt_no_key"  # no speech key in the vault (or no adapter for it)
    NO_AUDIO = "stt_no_audio"  # the part carries no file
    DOWNLOAD = "stt_download_failed"  # Telegram did not hand the file over
    NO_SPEECH = "stt_no_speech"  # the provider answered with an empty transcript
    PROVIDER = "stt_provider_failed"  # every provider tried failed: `SttResult.failed` says how


# --- providers ------------------------------------------------------------------------------------------------------
# A failure the next provider may not share moves the note on to it (SPEC §3.2.11 step 6). A timeout does not: the
# note's time budget (UC-05: 5 s) is already spent, and OpenRouter's Gemini runs on the same Google backend.
FALLBACK_CODES: Final = frozenset(
    {
        ErrorCode.AUTH_FAILED,
        ErrorCode.QUOTA_EXHAUSTED,
        ErrorCode.RATE_LIMITED,
        ErrorCode.PROVIDER_UNAVAILABLE,
        ErrorCode.NOT_INSTALLED,  # OpenRouter without ffmpeg
    }
)


def pick_stt(preference: str, available: Iterable[str]) -> tuple[str, ...]:
    """The STT providers to try for one note, in order. `preference` is `speech.stt_provider`: a named provider goes
    first and the other stays as its fallback; `auto` (or anything else) is Google, then OpenRouter. Only providers
    in `available` (a key in the vault and an adapter) are listed; none → ()."""
    usable = set(available)
    first = (preference,) if preference in STT_ORDER else ()
    order = (*first, *(provider for provider in STT_ORDER if provider not in first))
    return tuple(provider for provider in order if provider in usable)


def falls_back(code: ErrorCode) -> bool:
    return code in FALLBACK_CODES


def stt_model(provider: str, snap: Snapshot) -> str:
    """The model setting of `provider`'s STT path."""
    return snap[defaults.STT_GOOGLE_MODEL] if provider == GOOGLE else snap[defaults.STT_OPENROUTER_MODEL]


def stt_timeout(snap: Snapshot, duration_s: int | None) -> float:
    """The bound of one transcription call: `timeouts.stt_s` plus `timeouts.stt_per_audio_s` per second of audio
    (Telegram's `duration`; unknown counts as 0), so a two-minute note is not cut off at the short-note bound."""
    seconds = max(duration_s or 0, 0)
    return snap[defaults.TIMEOUTS_STT_S] + seconds * snap[defaults.TIMEOUTS_STT_PER_AUDIO_S]


# --- vocabulary -----------------------------------------------------------------------------------------------------
class Named(Protocol):
    """Something the owner calls by name. `dev.projects.domain.Project` is one as it is: its folder name and aliases."""

    @property
    def id(self) -> str: ...

    @property
    def aliases(self) -> Sequence[str]: ...


def build_vocabulary(
    owner_terms: Sequence[str], named: Iterable[Named], *, limit: int = VOCABULARY_LIMIT
) -> tuple[str, ...]:
    """The terms STT must spell exactly (UC-05), most important first, because prompt-based providers read only the
    first 200: the owner's own terms, newest first (he added each one after a misspelling), then each name followed by
    its aliases, in the order given. One line each, single spaces, de-duplicated without regard to case (the first
    spelling wins), at most `limit`."""
    candidates = itertools.chain(reversed(owner_terms), *((entry.id, *entry.aliases) for entry in named))
    seen: set[str] = set()
    terms: list[str] = []
    for raw in candidates:
        term = " ".join(raw.split())
        key = term.casefold()
        if not term or key in seen:
            continue
        seen.add(key)
        terms.append(term)
        if len(terms) == limit:
            break
    return tuple(terms)


_TERM_WRAPPERS: Final = " \"'`«»“”„‘’"
_TERM_TAIL: Final = ".,;:!?"


def normalised_term(raw: str) -> str:
    """The term the owner asked for ("bu so'zni to'g'ri yoz: «BonviHR»."): one line, single spaces, without wrapping
    quotes or a final full stop. Raises `SttTermInvalid` when nothing is left or it is longer than `TERM_MAX_CHARS`."""
    term = " ".join(raw.split()).strip(_TERM_WRAPPERS).rstrip(_TERM_TAIL).strip(_TERM_WRAPPERS)
    if not term or len(term) > TERM_MAX_CHARS:
        raise SttTermInvalid(limit=TERM_MAX_CHARS)
    return term


def with_term(terms: Sequence[str], term: str, *, limit: int = VOCABULARY_LIMIT) -> list[str] | None:
    """`stt.extra_terms` with `term` as its newest entry, or None when that exact spelling is already there. Another
    spelling of the same term is replaced; past `limit`, the oldest terms go."""
    if term in terms:
        return None
    kept = [existing for existing in terms if existing.casefold() != term.casefold()]
    return [*kept, term][-limit:]


# --- one note's outcome ---------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Attempt:
    """One provider's failed try at a note."""

    provider: str
    code: ErrorCode


@dataclass(frozen=True, slots=True)
class SttResult:
    """What became of one voice note. `transcript` is the provider's answer, when one came back (it may be empty)."""

    update_id: int
    text: str | None = field(default=None, repr=False)
    reason: FailReason | None = None
    failed: tuple[Attempt, ...] = ()  # providers that failed first, in the order tried
    transcript: Transcript | None = field(default=None, repr=False)

    @property
    def ok(self) -> bool:
        return self.reason is None

    @property
    def status(self) -> SttStatus:
        return SttStatus.DONE if self.ok else SttStatus.FAILED

    @property
    def provider(self) -> str | None:
        """The provider that answered, else the last one that failed."""
        if self.transcript is not None:
            return self.transcript.provider
        return self.failed[-1].provider if self.failed else None

    @property
    def error_code(self) -> str | None:
        if self.reason is FailReason.PROVIDER and self.failed:
            return self.failed[-1].code.value
        return None if self.reason is None else self.reason.value


def transcribed(update_id: int, transcript: Transcript, *, failed: Sequence[Attempt] = ()) -> SttResult:
    """A provider answered. An empty transcript (silence, noise) is a failure the owner hears about."""
    text = transcript.text.strip()
    if not text:
        return SttResult(update_id, reason=FailReason.NO_SPEECH, failed=tuple(failed), transcript=transcript)
    return SttResult(update_id, text=text, failed=tuple(failed), transcript=transcript)


_LABELS: Final[Mapping[str, S.Msg]] = {GOOGLE: S.SPEECH_GOOGLE, OPENROUTER: S.SPEECH_OPENROUTER}
_PROBLEMS: Final[Mapping[ErrorCode, S.Msg]] = {
    ErrorCode.QUOTA_EXHAUSTED: S.STT_PROBLEM_QUOTA,
    ErrorCode.AUTH_FAILED: S.STT_PROBLEM_AUTH,
    ErrorCode.RATE_LIMITED: S.STT_PROBLEM_RATE_LIMITED,
    ErrorCode.CALL_TIMED_OUT: S.STT_PROBLEM_TIMEOUT,
    ErrorCode.COST_CAP_REACHED: S.COST_STT_PROBLEM_CAP,  # skipped: its monthly cap is used up (T51)
}
_REASONS: Final[Mapping[FailReason, S.Msg]] = {
    FailReason.NO_KEY: S.STT_REASON_NO_KEY,
    FailReason.NO_AUDIO: S.STT_REASON_NO_AUDIO,
    FailReason.DOWNLOAD: S.STT_REASON_DOWNLOAD,
    FailReason.NO_SPEECH: S.STT_REASON_NO_SPEECH,
}


def provider_label(provider: str) -> BotText:
    return _LABELS[provider]()


def problem_text(code: ErrorCode) -> BotText:
    return _PROBLEMS.get(code, S.STT_PROBLEM_UNAVAILABLE)()


def reason_text(result: SttResult) -> BotText:
    """Why the note failed, plainly: each provider that failed and how, or the one reason there was."""
    if result.reason is FailReason.PROVIDER:
        lines = (
            S.STT_REASON_PROVIDER(provider=provider_label(attempt.provider), problem=problem_text(attempt.code))
            for attempt in result.failed
        )
        return join(lines, "; ")
    if result.reason is None:
        msg = f"note {result.update_id} was transcribed"
        raise ValueError(msg)
    return _REASONS[result.reason]()


def marker(result: SttResult) -> BotText:
    """The text a failed note keeps in its burst (`[ovoz tushunilmadi: …]`)."""
    return S.VOICE_NOT_UNDERSTOOD_MARKER(reason=reason_text(result))


def failure_notice(result: SttResult) -> BotText:
    """The owner's message about a failed note. When a key would fix it, it says which keys work."""
    rejected = bool(result.failed) and all(attempt.code is ErrorCode.AUTH_FAILED for attempt in result.failed)
    needs_key = result.reason is FailReason.NO_KEY or (result.reason is FailReason.PROVIDER and rejected)
    entry = S.STT_FAILED_NEEDS_KEY if needs_key else S.STT_FAILED
    return entry(reason=reason_text(result))


@dataclass(frozen=True, slots=True)
class Notice:
    chat_id: int
    reply_to: int | None


def notice_target(snap: Snapshot, message_id: int | None) -> Notice:
    """Where the owner hears about a failed note. The dev bot serves one group, so the notice replies to the note
    there. A burst part does not carry its chat (pending wiring), and the personal bot also serves an optional group,
    so it writes to the owner's private chat without a reply."""
    if snap.bot is BotName.DEV:
        return Notice(chat_id=snap[defaults.ACCESS_DEV_GROUP_ID], reply_to=message_id)
    return Notice(chat_id=snap[defaults.ACCESS_OWNER_ID], reply_to=None)


def usage_of(transcript: Transcript, *, google_usd_micros_per_min: int = 0) -> Usage:
    """The `usage_records` row of one answered STT call. A call is paid when its cost is above 0. Google reports no
    cost, so its call is priced from the audio length at `cost.google_stt_usd_micros_per_min` (T51; 0 = unpaid)."""
    reported = transcript.cost_usd_micros
    if reported is None and transcript.provider == GOOGLE:
        reported = audio_cost_micros(transcript.audio_ms, google_usd_micros_per_min)
    amount = reported or 0
    return Usage(
        source=USAGE_SOURCE,
        provider=Provider(transcript.provider),
        model=transcript.model,
        is_paid_api=amount > 0,
        cost_usd_micros=amount,
        duration_ms=transcript.latency_ms,
        units={"audio_ms": transcript.audio_ms, **transcript.units},
    )


# --- speech keys (SPEC §3.2.11) -------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class SpeechKey:
    provider: str
    name: str  # its vault name: `google_api_key`, `google_api_key_2`, …


def provider_of_kind(kind: str) -> str | None:
    return next((provider for provider, key_kind in KEY_KINDS.items() if key_kind == kind), None)


def speech_keys(secrets: Iterable[SecretRef]) -> tuple[SpeechKey, ...]:
    """The speech keys among `secrets`, once each, in the given order."""
    found: dict[str, SpeechKey] = {}
    for ref in secrets:
        provider = provider_of_kind(ref.kind)
        if provider is not None and ref.name not in found:
            found[ref.name] = SpeechKey(provider=provider, name=ref.name)
    return tuple(found.values())


def current_keys(secrets: Iterable[SecretRef]) -> dict[str, str]:
    """provider → the vault name of the key in use: the newest active secret of its kind."""
    keys: dict[str, str] = {}
    for ref in sorted(secrets, key=lambda ref: ref.id):
        provider = provider_of_kind(ref.kind)
        if provider is not None:
            keys[provider] = ref.name
    return keys


class KeyVerdict(StrEnum):
    ACTIVE = "active"  # it works: used from the next voice note; older keys of its kind are retired
    REJECTED = "rejected"  # the provider refused it: removed from the vault, never used
    NO_MODEL = "no_model"  # valid, but the configured STT model is not open to it
    OUT_OF_QUOTA = "out_of_quota"  # valid, but its quota or credit is used up
    RATE_LIMITED = "rate_limited"  # valid, but rate limited right now
    UNCHECKED = "unchecked"  # no answer now (network, timeout): kept and used


@dataclass(frozen=True, slots=True)
class KeyOutcome:
    key: SpeechKey
    verdict: KeyVerdict
    model: str  # the STT model it was checked for
    is_free_tier: bool = False
    error_code: ErrorCode | None = None


_ERROR_VERDICTS: Final[Mapping[ErrorCode, KeyVerdict]] = {
    ErrorCode.AUTH_FAILED: KeyVerdict.REJECTED,
    ErrorCode.QUOTA_EXHAUSTED: KeyVerdict.OUT_OF_QUOTA,
    ErrorCode.RATE_LIMITED: KeyVerdict.RATE_LIMITED,
}


def key_checked(key: SpeechKey, check: SpeechKeyCheck, *, model: str) -> KeyOutcome:
    """The provider answered the check. A spent OpenRouter limit (`limit_remaining` ≤ 0) counts as out of quota."""
    if model not in check.available_models:
        verdict = KeyVerdict.NO_MODEL
    elif check.limit_remaining_usd is not None and check.limit_remaining_usd <= 0:
        verdict = KeyVerdict.OUT_OF_QUOTA
    else:
        verdict = KeyVerdict.ACTIVE
    return KeyOutcome(key=key, verdict=verdict, model=model, is_free_tier=check.is_free_tier is True)


def key_failed(key: SpeechKey, code: ErrorCode, *, model: str) -> KeyOutcome:
    return KeyOutcome(key=key, verdict=_ERROR_VERDICTS.get(code, KeyVerdict.UNCHECKED), model=model, error_code=code)


def keys_to_retire(outcome: KeyOutcome, active: Iterable[SecretRef]) -> tuple[str, ...]:
    """Vault names to remove after `outcome`: a rejected key itself; for a working key, every older key of its kind,
    so exactly one key per provider is in use. Any other outcome keeps everything."""
    if outcome.verdict is KeyVerdict.REJECTED:
        return (outcome.key.name,)
    if outcome.verdict is KeyVerdict.ACTIVE:
        kind = KEY_KINDS[outcome.key.provider]
        return tuple(ref.name for ref in active if ref.kind == kind and ref.name != outcome.key.name)
    return ()


def _outcome_line(outcome: KeyOutcome) -> BotText | None:
    provider = provider_label(outcome.key.provider)
    match outcome.verdict:
        case KeyVerdict.ACTIVE:
            return S.SPEECH_FREE_TIER(provider=provider) if outcome.is_free_tier else None
        case KeyVerdict.REJECTED:
            return S.SPEECH_KEY_REJECTED(provider=provider)
        case KeyVerdict.NO_MODEL:
            return S.SPEECH_KEY_NO_MODEL(provider=provider, model=outcome.model)
        case KeyVerdict.OUT_OF_QUOTA:
            return S.SPEECH_KEY_OUT_OF_QUOTA(provider=provider)
        case KeyVerdict.RATE_LIMITED:
            return S.SPEECH_KEY_RATE_LIMITED(provider=provider)
        case KeyVerdict.UNCHECKED:
            code = outcome.error_code or ErrorCode.PROVIDER_UNAVAILABLE
            return S.SPEECH_KEY_UNCHECKED(provider=provider, problem=problem_text(code))


def speech_status(outcomes: Sequence[KeyOutcome], order: Sequence[str]) -> BotText:
    """The one line after new speech keys: what is active now (the STT order), then what is wrong with a key or worth
    knowing about it (a free tier runs out fast). Never silent: a rejected key and no working key both say so."""
    if not order:
        status = S.SPEECH_NONE()
    else:
        providers = provider_label(order[0])
        if len(order) > 1:
            providers = S.SPEECH_WITH_FALLBACK(provider=providers, fallback=provider_label(order[1]))
        connected = any(outcome.verdict is KeyVerdict.ACTIVE for outcome in outcomes)
        status = (S.SPEECH_CONNECTED if connected else S.SPEECH_ACTIVE)(providers=providers)
    details = [line for outcome in outcomes if (line := _outcome_line(outcome)) is not None]
    return join([status, *details], " ")


# --- retention ------------------------------------------------------------------------------------------------------
def text_cutoff(now: datetime) -> datetime:
    """A finished transcript's text is nulled once `created_at` is strictly older than this."""
    return now - TEXT_KEEP


def row_cutoff(now: datetime) -> datetime:
    """A finished transcript row is deleted once `created_at` is strictly older than this."""
    return now - ROW_KEEP


# --- voice_tts --------------------------------------------------------------------------------------------------------
# Text to a voice note (SPEC §3.2.11 step 3, §3.2.13 step 6; UC-30 voice). The order under `auto` is the owner's
# decision of 2026-10-24: OpenRouter (Gemini TTS) → Google direct, only with its own key → Edge (free, no key) → text
# only. `speech.tts_provider` can put one provider first; the others stay as its fallbacks; "off" is text only.
EDGE: Final = "edge"
TTS_ORDER: Final = (OPENROUTER, GOOGLE, EDGE)
KEYLESS_TTS: Final = frozenset({EDGE})  # usable with an adapter alone
EDGE_MODEL: Final = "edge-tts"  # Edge has no model; this names its `usage_records` rows
TTS_USAGE_SOURCE: Final = "tts"
TTS_OFF: Final = "off"
FEMALE: Final = "female"
ENCODER: Final = "ffmpeg"  # the `provider` param of an encode failure (`adapters/tts_common.py`)
VOICE_FILE_MIME: Final = "audio/ogg"
NOTICE_BUCKET: Final = "%Y%m%d%H"  # one notice per reason per clock hour (SPEC §3.2.11 step 6)

# The 30 Gemini TTS voices by gender (cloud.google.com/text-to-speech/docs/gemini-tts, checked 2026-10-24). A voice
# that is not listed (an extended-library or custom voice) is the owner's explicit choice and is kept as it is.
GEMINI_FEMALE_VOICES: Final = frozenset(
    {
        "achernar", "aoede", "autonoe", "callirrhoe", "despina", "erinome", "gacrux",
        "kore", "laomedeia", "leda", "pulcherrima", "sulafat", "vindemiatrix", "zephyr",
    }
)  # fmt: skip
GEMINI_MALE_VOICES: Final = frozenset(
    {
        "achird", "algenib", "algieba", "alnilam", "charon", "enceladus", "fenrir", "iapetus",
        "orus", "puck", "rasalgethi", "sadachbia", "sadaltager", "schedar", "umbriel", "zubenelgenubi",
    }
)  # fmt: skip


def pick_tts(preference: str, available: Iterable[str]) -> tuple[str, ...]:
    """The TTS providers to try for one note, in order. `preference` is `speech.tts_provider`: a named provider goes
    first, the rest keep the `auto` order behind it; "off" → (). Only providers in `available` (an adapter, plus a key
    for OpenRouter and Google) are listed."""
    if preference == TTS_OFF:
        return ()
    usable = set(available)
    first = (preference,) if preference in TTS_ORDER else ()
    order = (*first, *(provider for provider in TTS_ORDER if provider not in first))
    return tuple(provider for provider in order if provider in usable)


def tts_model(provider: str, snap: Snapshot) -> str:
    if provider == OPENROUTER:
        return snap[defaults.TTS_OPENROUTER_MODEL]
    if provider == GOOGLE:
        return snap[defaults.TTS_GOOGLE_MODEL]
    return EDGE_MODEL


def tts_voice(provider: str, snap: Snapshot) -> str:
    """The voice for `provider` from this job's settings, so "ovozingni ayolga o'zgartir" (`voice.gender`) applies to
    the next note. Edge: its female or male voice. Gemini (direct or OpenRouter): `voice.name`, unless it is a known
    voice of the other gender, then `tts.female_voice` / `tts.male_voice`."""
    female = snap[defaults.VOICE_GENDER] == FEMALE
    if provider == EDGE:
        return snap[defaults.TTS_EDGE_VOICE] if female else snap[defaults.TTS_EDGE_MALE_VOICE]
    name = snap[defaults.VOICE_NAME].strip()
    other = GEMINI_MALE_VOICES if female else GEMINI_FEMALE_VOICES
    if not name or name.casefold() in other:
        return snap[defaults.TTS_FEMALE_VOICE] if female else snap[defaults.TTS_MALE_VOICE]
    return name


# --- voice samples (UC-34 "ovozing yoqmadi") ------------------------------------------------------------------------
MALE: Final = "male"


@dataclass(frozen=True, slots=True)
class VoiceSample:
    """One numbered sample: the voice it is spoken in, and the settings that make it the bot's voice."""

    number: int
    provider: str
    voice: str
    changes: tuple[tuple[str, str], ...]  # (setting key, value), applied as one request when the owner picks it


def gemini_gender(voice: str) -> str:
    return MALE if voice.casefold() in GEMINI_MALE_VOICES else FEMALE


def sample_voices(provider: str, snap: Snapshot) -> tuple[VoiceSample, ...]:
    """The samples for `provider` (the one the next note would use). Edge has one Uzbek voice per gender, so it
    offers both; Gemini (direct or OpenRouter) offers the first `voice.sample_count` of `voice.sample_voices` that
    differ from the voice in use, each with its gender, so picking one also sets `voice.gender`."""
    if provider == EDGE:
        pairs = ((snap[defaults.TTS_EDGE_VOICE], FEMALE), (snap[defaults.TTS_EDGE_MALE_VOICE], MALE))
        return tuple(
            VoiceSample(number=index, provider=provider, voice=voice, changes=((defaults.VOICE_GENDER.key, gender),))
            for index, (voice, gender) in enumerate(pairs, start=1)
        )
    current = tts_voice(provider, snap).casefold()
    names = dict.fromkeys(
        name.strip() for name in snap[defaults.VOICE_SAMPLE_VOICES].split(",") if name.strip().casefold() != current
    )
    chosen = tuple(names)[: snap[defaults.VOICE_SAMPLE_COUNT]]
    return tuple(
        VoiceSample(
            number=index,
            provider=provider,
            voice=name,
            changes=((defaults.VOICE_NAME.key, name), (defaults.VOICE_GENDER.key, gemini_gender(name))),
        )
        for index, name in enumerate(chosen, start=1)
    )


def pick_sample(samples: Sequence[VoiceSample], number: int) -> VoiceSample | None:
    return next((sample for sample in samples if sample.number == number), None)


# --- the spoken text -------------------------------------------------------------------------------------------------
_CODE_BLOCK: Final = re.compile(r"```.*?(?:```|$)", re.DOTALL)
_INLINE_CODE: Final = re.compile(r"`[^`\n]*`")
_MD_LINK: Final = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_URL: Final = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
_LINE_MARK: Final = re.compile(r"^\s*(?:#{1,6}\s+|>+\s*|[-*+•]\s+|\d+[.)]\s+)", re.MULTILINE)
_EMPHASIS: Final = re.compile(r"(\*{1,3}|_{2,3}|~~)")
_SENTENCE_END: Final = re.compile(r"[.!?…](?=\s|$)")
_SPACE_BEFORE_MARK: Final = re.compile(r"\s+([.,!?;:…])")
_INVISIBLE: Final = frozenset({"\u200d", "\ufe0f", "\ufe0e"})  # zero-width joiner, variation selectors
_SYMBOLS: Final = frozenset({"So", "Sk"})  # emoji and pictographs; skin-tone and other modifier symbols


def _drop_symbols(text: str) -> str:
    """Emoji, pictographs and their modifiers, joiners and variation selectors: nothing a voice should read."""
    return "".join(char for char in text if char not in _INVISIBLE and unicodedata.category(char) not in _SYMBOLS)


def speakable(text: str, max_words: int) -> str:
    """The voice script as it should be spoken (SPEC §3.2.13 step 6): without code, URLs, markdown or emoji, on one
    line, and at most `max_words` words (`voice.max_words`, ~55–70 ≈ 20–30 s), cut at the last sentence end that
    fits. A first sentence longer than that is cut at the word limit and closed with a full stop."""
    cleaned = _CODE_BLOCK.sub(" ", text)
    cleaned = _INLINE_CODE.sub(" ", cleaned)
    cleaned = _MD_LINK.sub(r"\1", cleaned)
    cleaned = _URL.sub(" ", cleaned)
    cleaned = _LINE_MARK.sub("", cleaned)
    cleaned = _EMPHASIS.sub("", cleaned)
    cleaned = _SPACE_BEFORE_MARK.sub(r"\1", " ".join(_drop_symbols(cleaned).split()))
    words = cleaned.split(" ")
    if not cleaned or len(words) <= max_words:
        return cleaned
    head = " ".join(words[:max_words])
    ends = [match.end() for match in _SENTENCE_END.finditer(head)]
    if ends:
        return head[: ends[-1]].strip()
    return head.rstrip(",;:—–- ") + "."


# --- one note's outcome ----------------------------------------------------------------------------------------------
class SkipReason(StrEnum):
    """Why an answer has no voice note."""

    DISABLED = "disabled"  # voice.enabled is off (or speech.tts_provider is "off"): the owner's choice, no notice
    EMPTY = "empty"  # nothing speakable was left
    SUMMARY = "summary"  # the spoken summary could not be made
    NO_PROVIDER = "no_provider"  # no TTS adapter is usable
    NO_FFMPEG = "no_ffmpeg"  # the encoder is missing: every provider needs it
    ENCODE = "encode"  # ffmpeg failed on the audio
    PROVIDER = "provider"  # every provider tried failed


SILENT_SKIPS: Final = frozenset({SkipReason.DISABLED, SkipReason.EMPTY})


@dataclass(frozen=True, slots=True)
class TtsAttempt:
    """One provider's failed try at a note. `encoder` marks a failure of ffmpeg, not of the provider."""

    provider: str
    code: ErrorCode
    encoder: bool = False


def tts_attempt(provider: str, error: ExternalError) -> TtsAttempt:
    encoder = error.code is ErrorCode.NOT_INSTALLED or error.params.get("provider") == ENCODER
    return TtsAttempt(provider=provider, code=error.code, encoder=encoder)


def tts_falls_back(attempt: TtsAttempt) -> bool:
    """Every provider failure moves on to the next provider: the text is already out, and a late voice is still a
    voice. An encoder failure does not: every provider's audio goes through the same ffmpeg."""
    return not attempt.encoder


@dataclass(frozen=True, slots=True)
class TtsResult:
    """What became of one voice note: the note, or why there is none; `failed` lists the providers that failed
    first, in the order tried. `latency_ms` is the synthesis call that answered, fallbacks excluded."""

    audio: SpeechAudio | None = field(default=None, repr=False)
    reason: SkipReason | None = None
    failed: tuple[TtsAttempt, ...] = ()
    latency_ms: int = 0

    @property
    def ok(self) -> bool:
        return self.audio is not None

    @property
    def provider(self) -> str | None:
        if self.audio is not None:
            return self.audio.provider
        return self.failed[-1].provider if self.failed else None


def stopped(attempt: TtsAttempt, failed: Sequence[TtsAttempt]) -> TtsResult:
    """The chain stopped at an encoder failure."""
    reason = SkipReason.NO_FFMPEG if attempt.code is ErrorCode.NOT_INSTALLED else SkipReason.ENCODE
    return TtsResult(reason=reason, failed=tuple(failed))


_TTS_LABELS: Final[Mapping[str, S.Msg]] = {
    GOOGLE: S.SPEECH_GOOGLE,
    OPENROUTER: S.SPEECH_OPENROUTER,
    EDGE: S.SPEECH_EDGE,
}
_SKIP_TEXTS: Final[Mapping[SkipReason, S.Msg]] = {
    SkipReason.SUMMARY: S.TTS_REASON_SUMMARY,
    SkipReason.NO_PROVIDER: S.TTS_REASON_NO_PROVIDER,
    SkipReason.NO_FFMPEG: S.TTS_REASON_NO_FFMPEG,
    SkipReason.ENCODE: S.TTS_REASON_ENCODE,
}


def tts_label(provider: str) -> BotText:
    return _TTS_LABELS[provider]()


@dataclass(frozen=True, slots=True)
class TtsNotice:
    """One line for the owner. `key` names its reason, so the same reason is told once per hour."""

    key: str
    text: BotText


def tts_notices(result: TtsResult) -> tuple[TtsNotice, ...]:
    """Never silent (SPEC §3.2.11 step 6): a note that came from a fallback says which provider failed and how; a note
    that did not come says why (`S.VOICE_SKIPPED`). The owner's own "voice off" and an empty script say nothing."""
    if result.audio is not None:
        if not result.failed:
            return ()
        first = result.failed[0]
        text = S.TTS_FELL_BACK(
            provider=tts_label(first.provider),
            problem=problem_text(first.code),
            fallback=tts_label(result.audio.provider),
        )
        return (TtsNotice(key=f"fallback-{first.provider}-{first.code.value}", text=text),)
    if result.reason is None or result.reason in SILENT_SKIPS:
        return ()
    if result.reason is SkipReason.PROVIDER:
        reason = join(
            (
                S.STT_REASON_PROVIDER(provider=tts_label(attempt.provider), problem=problem_text(attempt.code))
                for attempt in result.failed
            ),
            "; ",
        )
        codes = "-".join(sorted({attempt.code.value for attempt in result.failed}))
        return (TtsNotice(key=f"skipped-{codes}", text=S.VOICE_SKIPPED(reason=reason)),)
    reason_text = _SKIP_TEXTS[result.reason]()
    return (TtsNotice(key=f"skipped-{result.reason.value}", text=S.VOICE_SKIPPED(reason=reason_text)),)


def notice_ref(key: str, now: datetime) -> str:
    """The outbox ref of a notice: the same reason within one clock hour is one row (the outbox dedupes it)."""
    return f"tts-{key}-{now.strftime(NOTICE_BUCKET)}"


def tts_usage_of(audio: SpeechAudio, *, duration_ms: int) -> Usage:
    """The `usage_records` row of one answered TTS call. Edge is free; OpenRouter and Google report no cost on this
    path (OpenRouter prices TTS per character, `units.characters`), so they record 0: TTS has no price setting yet,
    though a provider capped by its other spend is still skipped (T51)."""
    cost = audio.cost_usd_micros or 0
    return Usage(
        source=TTS_USAGE_SOURCE,
        provider=Provider(audio.provider),
        model=audio.model,
        is_paid_api=cost > 0 and audio.provider != EDGE,
        cost_usd_micros=cost,
        duration_ms=duration_ms,
        units={"audio_ms": audio.duration_ms or 0, **audio.units},
    )


def voice_duration_s(audio: SpeechAudio) -> int | None:
    """`sendVoice` takes whole seconds; a part second counts as one."""
    if not audio.duration_ms:
        return None
    return max(1, -(-audio.duration_ms // 1000))
