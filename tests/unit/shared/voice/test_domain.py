"""voice.domain: provider order and fallback, the vocabulary, the owner's terms, failure texts, speech-key verdicts
and the status line (SPEC §3.2 step 5, §3.2.11; UC-05)."""

from dataclasses import dataclass
from decimal import Decimal

import pytest

from tests.factories import (
    DEV_GROUP_ID,
    OWNER_ID,
    make_default_snapshot,
    make_project_record,
    make_transcript,
)
from tests.fakes.clock import DEFAULT_START
from yordamchi.core import strings as S
from yordamchi.core.errors import ErrorCode, SttTermInvalid
from yordamchi.core.paths import BotName
from yordamchi.dev.projects import domain as projects
from yordamchi.dev.projects.domain import ProjectPrefs
from yordamchi.ports.speech import SpeechKeyCheck
from yordamchi.shared.cost.domain import Provider
from yordamchi.shared.cost.domain import require_valid as require_valid_usage
from yordamchi.shared.settings import defaults
from yordamchi.shared.vault.domain import SecretRef
from yordamchi.shared.voice import domain
from yordamchi.shared.voice.domain import (
    GOOGLE,
    OPENROUTER,
    Attempt,
    FailReason,
    KeyOutcome,
    KeyVerdict,
    SpeechKey,
    SttResult,
    SttStatus,
)

GOOGLE_KEY = SpeechKey(provider=GOOGLE, name="google_api_key")
OPENROUTER_KEY = SpeechKey(provider=OPENROUTER, name="openrouter_api_key")
STT_MODEL = "gemini-3.5-transcribe"


@dataclass(frozen=True)
class _Entry:
    id: str
    aliases: tuple[str, ...] = ()


def _ref(secret_id: int, kind: str, name: str | None = None) -> SecretRef:
    return SecretRef(id=secret_id, name=name or kind, kind=kind, last4="abcd", bots=(BotName.DEV,))


def _check(
    *,
    available: tuple[str, ...] = (STT_MODEL,),
    is_free_tier: bool | None = None,
    limit_remaining_usd: Decimal | None = None,
) -> SpeechKeyCheck:
    return SpeechKeyCheck(
        provider=GOOGLE,
        available_models=available,
        missing_models=() if STT_MODEL in available else (STT_MODEL,),
        is_free_tier=is_free_tier,
        limit_remaining_usd=limit_remaining_usd,
    )


# --- provider choice ------------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("preference", "available", "order"),
    [
        ("auto", {GOOGLE, OPENROUTER}, (GOOGLE, OPENROUTER)),  # a direct Google key spells names better
        ("auto", {OPENROUTER}, (OPENROUTER,)),
        ("auto", {GOOGLE}, (GOOGLE,)),
        ("auto", set(), ()),
        ("openrouter", {GOOGLE, OPENROUTER}, (OPENROUTER, GOOGLE)),  # the other one stays as its fallback
        ("google", {OPENROUTER}, (OPENROUTER,)),  # the named one has no key: the other still works
        ("whisper", {GOOGLE, OPENROUTER}, (GOOGLE, OPENROUTER)),  # an unknown value reads as auto
    ],
)
def test_pick_stt_orders_the_providers_that_have_a_key(
    preference: str, available: set[str], order: tuple[str, ...]
) -> None:
    assert domain.pick_stt(preference, available) == order


@pytest.mark.parametrize(
    "code",
    [
        ErrorCode.QUOTA_EXHAUSTED,
        ErrorCode.AUTH_FAILED,
        ErrorCode.PROVIDER_UNAVAILABLE,
        ErrorCode.RATE_LIMITED,
        ErrorCode.NOT_INSTALLED,
    ],
)
def test_a_provider_side_failure_falls_back_to_the_next_provider(code: ErrorCode) -> None:
    assert domain.falls_back(code)


def test_a_timeout_does_not_fall_back_because_the_time_budget_is_spent() -> None:
    assert not domain.falls_back(ErrorCode.CALL_TIMED_OUT)


def test_each_provider_uses_its_own_model_setting() -> None:
    snap = make_default_snapshot()
    assert domain.stt_model(GOOGLE, snap) == snap[defaults.STT_GOOGLE_MODEL]
    assert domain.stt_model(OPENROUTER, snap) == snap[defaults.STT_OPENROUTER_MODEL]


# --- vocabulary -----------------------------------------------------------------------------------------------------
def test_vocabulary_puts_the_owners_newest_terms_first_then_names_and_aliases() -> None:
    named = [_Entry("BonviHR", ("hr", "bonvi")), _Entry("HRMentor")]
    vocabulary = domain.build_vocabulary(["Wunderkind", "OvozYoz"], named)
    assert vocabulary == ("OvozYoz", "Wunderkind", "BonviHR", "hr", "bonvi", "HRMentor")


def test_vocabulary_drops_blanks_and_case_duplicates_keeping_the_first_spelling() -> None:
    named = [_Entry("BonviHR", ("bonvihr", "  Bonvi   HR ")), _Entry("  ")]
    assert domain.build_vocabulary(["BONVIHR"], named) == ("BONVIHR", "Bonvi HR")


def test_vocabulary_stops_at_its_limit() -> None:
    named = [_Entry(f"P{index}") for index in range(1500)]
    vocabulary = domain.build_vocabulary(["mine"], named)
    assert len(vocabulary) == domain.VOCABULARY_LIMIT
    assert vocabulary[:2] == ("mine", "P0")


def test_a_dev_project_is_a_named_entry_as_it_is() -> None:
    """The dev bot injects `projects.service.list_projects`; its `Project` needs no adapter (C1 keeps shared clean)."""
    prefs = ProjectPrefs(aliases={"hr": "BonviHR"})
    project = projects.project_view(make_project_record("BonviHR", at=DEFAULT_START), prefs)
    assert domain.build_vocabulary([], [project]) == ("BonviHR", "hr")


# --- the owner's terms ----------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("raw", "term"),
    [
        ("BonviHR", "BonviHR"),
        ("  «Wunderkind   School».  ", "Wunderkind School"),
        ('"HRMentor"', "HRMentor"),
        ("o'zbekcha", "o'zbekcha"),  # an apostrophe inside a word stays
        ("Node.js", "Node.js"),
    ],
)
def test_a_term_is_one_clean_line(raw: str, term: str) -> None:
    assert domain.normalised_term(raw) == term


@pytest.mark.parametrize("raw", ["", "  «» ", "x" * (domain.TERM_MAX_CHARS + 1)])
def test_an_empty_or_overlong_term_is_refused(raw: str) -> None:
    with pytest.raises(SttTermInvalid) as caught:
        domain.normalised_term(raw)
    assert caught.value.params == {"limit": domain.TERM_MAX_CHARS}


def test_with_term_appends_replaces_another_spelling_and_ignores_an_exact_repeat() -> None:
    assert domain.with_term(["A", "B"], "C") == ["A", "B", "C"]
    assert domain.with_term(["bonvihr", "B"], "BonviHR") == ["B", "BonviHR"]
    assert domain.with_term(["A", "B"], "B") is None


def test_with_term_drops_the_oldest_past_the_limit() -> None:
    assert domain.with_term(["A", "B", "C"], "D", limit=3) == ["B", "C", "D"]


# --- a note's outcome -----------------------------------------------------------------------------------------------
def test_an_answer_is_done_and_an_empty_answer_is_no_speech() -> None:
    done = domain.transcribed(7, make_transcript("  salom  "))
    assert (done.ok, done.status, done.text, done.provider, done.error_code) == (
        True,
        SttStatus.DONE,
        "salom",
        GOOGLE,
        None,
    )
    silent = domain.transcribed(7, make_transcript("  "))
    assert (silent.ok, silent.status, silent.reason, silent.error_code) == (
        False,
        SttStatus.FAILED,
        FailReason.NO_SPEECH,
        "stt_no_speech",
    )


def test_a_provider_failure_names_every_provider_and_stores_the_last_code() -> None:
    result = SttResult(
        3,
        reason=FailReason.PROVIDER,
        failed=(Attempt(GOOGLE, ErrorCode.QUOTA_EXHAUSTED), Attempt(OPENROUTER, ErrorCode.CALL_TIMED_OUT)),
    )
    assert result.provider == OPENROUTER
    assert result.error_code == "call_timed_out"
    reason = f"{S.SPEECH_GOOGLE()} — {S.STT_PROBLEM_QUOTA()}; {S.SPEECH_OPENROUTER()} — {S.STT_PROBLEM_TIMEOUT()}"
    assert domain.reason_text(result) == reason
    assert domain.marker(result) == S.VOICE_NOT_UNDERSTOOD_MARKER(reason=reason)
    assert domain.failure_notice(result) == S.STT_FAILED(reason=reason)


def test_a_missing_or_rejected_key_tells_the_owner_which_key_to_send() -> None:
    no_key = SttResult(1, reason=FailReason.NO_KEY)
    assert domain.failure_notice(no_key) == S.STT_FAILED_NEEDS_KEY(reason=S.STT_REASON_NO_KEY())
    rejected = SttResult(1, reason=FailReason.PROVIDER, failed=(Attempt(GOOGLE, ErrorCode.AUTH_FAILED),))
    assert domain.failure_notice(rejected) == S.STT_FAILED_NEEDS_KEY(reason=domain.reason_text(rejected))
    assert domain.marker(SttResult(1, reason=FailReason.DOWNLOAD)) == S.VOICE_NOT_UNDERSTOOD_MARKER(
        reason=S.STT_REASON_DOWNLOAD()
    )


def test_a_transcribed_note_has_no_reason_text() -> None:
    with pytest.raises(ValueError, match="was transcribed"):
        domain.reason_text(SttResult(1, text="salom"))


def test_the_dev_bot_replies_to_the_note_in_its_group_and_the_personal_bot_writes_privately() -> None:
    dev = domain.notice_target(make_default_snapshot(BotName.DEV), 5001)
    assert (dev.chat_id, dev.reply_to) == (DEV_GROUP_ID, 5001)
    personal = domain.notice_target(make_default_snapshot(BotName.PERSONAL), 5001)
    assert (personal.chat_id, personal.reply_to) == (OWNER_ID, None)


def test_usage_records_the_reported_cost_in_micros_and_the_counters() -> None:
    paid = domain.usage_of(
        make_transcript(provider="openrouter", model="google/gemini-3.8-flash", cost_usd_micros=410, units={"x": 1})
    )
    require_valid_usage(paid)
    assert (paid.source, paid.provider, paid.is_paid_api, paid.cost_usd_micros) == (
        "stt",
        Provider.OPENROUTER,
        True,
        410,
    )
    assert paid.units == {"audio_ms": 60_000, "x": 1}
    free = domain.usage_of(make_transcript())
    require_valid_usage(free)
    assert (free.provider, free.is_paid_api, free.cost_usd_micros, free.duration_ms) == (
        Provider.GOOGLE,
        False,
        0,
        1800,
    )


# --- speech keys ----------------------------------------------------------------------------------------------------
def test_speech_keys_are_picked_out_of_the_stored_secrets_once_each() -> None:
    refs = [
        _ref(1, "github_token"),
        _ref(2, "google_api_key"),
        _ref(3, "openrouter_api_key"),
        _ref(2, "google_api_key"),
    ]
    assert domain.speech_keys(refs) == (GOOGLE_KEY, OPENROUTER_KEY)


def test_the_key_in_use_is_the_newest_of_its_kind() -> None:
    refs = [_ref(4, "google_api_key", "google_api_key_2"), _ref(1, "google_api_key"), _ref(2, "github_token")]
    assert domain.current_keys(refs) == {GOOGLE: "google_api_key_2"}
    assert domain.current_keys([]) == {}


def test_a_check_with_the_model_available_is_active() -> None:
    outcome = domain.key_checked(GOOGLE_KEY, _check(), model=STT_MODEL)
    assert (outcome.verdict, outcome.is_free_tier) == (KeyVerdict.ACTIVE, False)


def test_a_check_without_the_stt_model_or_with_a_spent_limit_is_not_active() -> None:
    assert domain.key_checked(GOOGLE_KEY, _check(available=()), model=STT_MODEL).verdict is KeyVerdict.NO_MODEL
    spent = _check(limit_remaining_usd=Decimal(0))
    assert domain.key_checked(OPENROUTER_KEY, spent, model=STT_MODEL).verdict is KeyVerdict.OUT_OF_QUOTA
    free = domain.key_checked(OPENROUTER_KEY, _check(is_free_tier=True), model=STT_MODEL)
    assert (free.verdict, free.is_free_tier) == (KeyVerdict.ACTIVE, True)


@pytest.mark.parametrize(
    ("code", "verdict"),
    [
        (ErrorCode.AUTH_FAILED, KeyVerdict.REJECTED),
        (ErrorCode.QUOTA_EXHAUSTED, KeyVerdict.OUT_OF_QUOTA),
        (ErrorCode.RATE_LIMITED, KeyVerdict.RATE_LIMITED),
        (ErrorCode.CALL_TIMED_OUT, KeyVerdict.UNCHECKED),
        (ErrorCode.PROVIDER_UNAVAILABLE, KeyVerdict.UNCHECKED),
    ],
)
def test_a_failed_check_has_a_verdict(code: ErrorCode, verdict: KeyVerdict) -> None:
    outcome = domain.key_failed(GOOGLE_KEY, code, model=STT_MODEL)
    assert (outcome.verdict, outcome.error_code) == (verdict, code)


def test_a_rejected_key_goes_and_a_working_key_retires_the_older_ones_of_its_kind() -> None:
    active = [
        _ref(1, "google_api_key"),
        _ref(2, "openrouter_api_key"),
        _ref(3, "google_api_key", "google_api_key_2"),
    ]
    newer = SpeechKey(provider=GOOGLE, name="google_api_key_2")
    assert domain.keys_to_retire(KeyOutcome(newer, KeyVerdict.ACTIVE, STT_MODEL), active) == ("google_api_key",)
    assert domain.keys_to_retire(KeyOutcome(newer, KeyVerdict.REJECTED, STT_MODEL), active) == ("google_api_key_2",)
    for verdict in (KeyVerdict.OUT_OF_QUOTA, KeyVerdict.RATE_LIMITED, KeyVerdict.UNCHECKED, KeyVerdict.NO_MODEL):
        assert domain.keys_to_retire(KeyOutcome(newer, verdict, STT_MODEL), active) == ()


# --- the status line ------------------------------------------------------------------------------------------------
def test_a_new_working_key_says_it_is_connected() -> None:
    line = domain.speech_status([KeyOutcome(OPENROUTER_KEY, KeyVerdict.ACTIVE, STT_MODEL)], (OPENROUTER,))
    assert line == "Ovoz: OpenRouter (Gemini) ulandi."


def test_both_providers_name_the_fallback_and_a_free_tier_is_said_plainly() -> None:
    outcome = KeyOutcome(OPENROUTER_KEY, KeyVerdict.ACTIVE, STT_MODEL, is_free_tier=True)
    line = domain.speech_status([outcome], (GOOGLE, OPENROUTER))
    providers = S.SPEECH_WITH_FALLBACK(provider=S.SPEECH_GOOGLE(), fallback=S.SPEECH_OPENROUTER())
    assert line == f"{S.SPEECH_CONNECTED(providers=providers)} {S.SPEECH_FREE_TIER(provider=S.SPEECH_OPENROUTER())}"
    assert "\n" not in line


def test_a_rejected_key_with_nothing_else_says_voice_does_not_work() -> None:
    line = domain.speech_status([KeyOutcome(GOOGLE_KEY, KeyVerdict.REJECTED, STT_MODEL)], ())
    assert line == f"{S.SPEECH_NONE()} {S.SPEECH_KEY_REJECTED(provider=S.SPEECH_GOOGLE())}"


@pytest.mark.parametrize(
    ("outcome", "detail"),
    [
        (
            KeyOutcome(GOOGLE_KEY, KeyVerdict.NO_MODEL, STT_MODEL),
            S.SPEECH_KEY_NO_MODEL(provider=S.SPEECH_GOOGLE(), model=STT_MODEL),
        ),
        (
            KeyOutcome(GOOGLE_KEY, KeyVerdict.OUT_OF_QUOTA, STT_MODEL),
            S.SPEECH_KEY_OUT_OF_QUOTA(provider=S.SPEECH_GOOGLE()),
        ),
        (
            KeyOutcome(GOOGLE_KEY, KeyVerdict.RATE_LIMITED, STT_MODEL),
            S.SPEECH_KEY_RATE_LIMITED(provider=S.SPEECH_GOOGLE()),
        ),
        (
            KeyOutcome(GOOGLE_KEY, KeyVerdict.UNCHECKED, STT_MODEL, error_code=ErrorCode.CALL_TIMED_OUT),
            S.SPEECH_KEY_UNCHECKED(provider=S.SPEECH_GOOGLE(), problem=S.STT_PROBLEM_TIMEOUT()),
        ),
    ],
)
def test_a_key_that_is_not_active_says_why_and_what_runs_now(outcome: KeyOutcome, detail: str) -> None:
    line = domain.speech_status([outcome], (GOOGLE,))
    assert line == f"{S.SPEECH_ACTIVE(providers=S.SPEECH_GOOGLE())} {detail}"


# --- retention ------------------------------------------------------------------------------------------------------
def test_retention_cutoffs() -> None:
    assert domain.text_cutoff(DEFAULT_START) == DEFAULT_START - domain.TEXT_KEEP
    assert domain.row_cutoff(DEFAULT_START) == DEFAULT_START - domain.ROW_KEEP
    assert SttStatus.PENDING not in domain.TERMINAL
