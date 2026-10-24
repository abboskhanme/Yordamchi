"""outbox.domain: Telegram length, escaping and code blocks, the 4,096 / 1,024 split and the `.md` fallback, payload
keys (no keyboard ever), per-chat order, rate limits, coalescing, typing, failure decisions and retention, on virtual
time (SPEC §2.7, §3.2.1, §4.6; NFR-REL-06, NFR-CAP-02/03, UC-11, UC-12)."""

from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from tests.fakes.clock import FakeClock
from yordamchi.core.errors import AuthFailed, CallTimedOut, ProviderUnavailable, RateLimited
from yordamchi.core.json_value import JsonValue
from yordamchi.shared.outbox import domain
from yordamchi.shared.outbox.domain import (
    Code,
    Drop,
    Failure,
    GiveUp,
    Kind,
    NotYet,
    Op,
    Park,
    Payload,
    PayloadInvalid,
    Queued,
    Ready,
    Resolved,
    Retry,
    Status,
)

CLOCK = FakeClock()
T0 = CLOCK.now()
GROUP = -1001234567890
PRIVATE = 424242


def _at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def _row(
    row_id: int,
    *,
    op: Op = Op.SEND_TEXT,
    kind: Kind = Kind.RESULT,
    chat_id: int = GROUP,
    priority: int = 1,
    status: Status = Status.PENDING,
    due: float = 0,
    attempts: int = 1,
) -> Queued:
    return Queued(
        id=row_id,
        chat_id=chat_id,
        op=op,
        kind=kind,
        priority=priority,
        status=status,
        next_attempt_at=_at(due),
        attempts=attempts,
        dedupe_key=domain.dedupe_key(kind, f"r{row_id}", 0),
    )


# --- length and escaping -------------------------------------------------------------------------------------------
def test_length_is_counted_in_utf16_units_like_telegram() -> None:
    assert domain.tg_len("salom") == 5
    assert domain.tg_len("o'zbek ʻ") == 8
    assert domain.tg_len("\U0001f440") == 2  # an emoji outside the BMP counts twice
    assert domain.cut("ab\U0001f440cd", 4) == "ab…"


def test_html_escaping_keeps_quotes_as_typed() -> None:
    assert domain.escape_html("a < b && c > d") == "a &lt; b &amp;&amp; c &gt; d"
    assert domain.escape_html('o\'zbek "tili"') == 'o\'zbek "tili"'


def test_plain_text_is_sent_as_typed_without_html() -> None:
    text = "BonviHR · Natija: <b>tayyor</b> & push qilindi"
    assert domain.render(text) == domain.Rendered(text=text, html=False)


def test_a_code_block_becomes_an_escaped_pre_block() -> None:
    rendered = domain.render("BonviHR · xato:\n```python\nif a < b and c & d:\n    pass\n```\nTuzatdim.")
    assert rendered.html
    assert rendered.text == (
        "BonviHR · xato:\n"
        '<pre><code class="language-python">if a &lt; b and c &amp; d:\n    pass</code></pre>\n'
        "Tuzatdim."
    )


def test_an_unclosed_fence_runs_to_the_end_and_a_strange_language_is_dropped() -> None:
    rendered = domain.render('```bad"lang\nx > 1')
    assert rendered.text == "<pre>x &gt; 1</pre>"


# --- the split -----------------------------------------------------------------------------------------------------
def test_a_text_that_fits_is_one_part_unchanged() -> None:
    text = "BonviHR · Natija: login tuzatildi.\n\n\nFayllar: 2 ta"
    assert domain.split_text(text) == (text,)
    assert domain.split_text("   \n ") == ()


def test_a_long_text_is_split_at_paragraphs_within_the_limit() -> None:
    paragraphs = [f"{index}-band. " + "so'z " * 150 for index in range(12)]
    parts = domain.split_text("\n\n".join(paragraphs))
    assert len(parts) > 1
    assert all(domain.tg_len(part) <= domain.TEXT_LIMIT for part in parts)
    assert [paragraph.strip() for part in parts for paragraph in part.split("\n\n")] == [
        paragraph.strip() for paragraph in paragraphs
    ]


def test_emoji_never_push_a_part_over_the_limit() -> None:
    text = "\U0001f600" * 5000  # 10,000 UTF-16 units, no spaces or lines to cut at
    parts = domain.split_text(text)
    assert all(domain.tg_len(part) <= domain.TEXT_LIMIT for part in parts)
    assert "".join(parts) == text


def test_a_code_block_that_fits_stays_whole_in_one_part() -> None:
    code = "```sh\n" + "\n".join(f"echo {index}" for index in range(30)) + "\n```"
    prose = "matn " * 900
    parts = domain.split_text(f"{prose}\n\n{code}\n\n{prose}", limit=4096)
    assert sum(code in part for part in parts) == 1


def test_a_long_code_block_reopens_its_fence_in_every_part() -> None:
    lines = [f"line_{index:04d} = {index}" for index in range(600)]
    parts = domain.split_text("```python\n" + "\n".join(lines) + "\n```", limit=1000)
    assert len(parts) > 1
    for part in parts:
        assert domain.tg_len(part) <= 1000
        assert part.startswith("```python\n")
        assert part.endswith("\n```")
        assert domain.render(part).text.startswith('<pre><code class="language-python">')
    assert [line for part in parts for line in part.split("\n")[1:-1]] == lines


def test_a_giant_word_is_cut_by_characters() -> None:
    parts = domain.split_text("x" * 9000)
    assert [len(part) for part in parts] == [4096, 4096, 808]


def test_the_split_limit_has_a_floor() -> None:
    with pytest.raises(ValueError, match="at least"):
        domain.split_text("abc", limit=10)


def test_fit_one_cuts_an_edit_to_one_message() -> None:
    assert domain.fit_one("qisqa") == "qisqa"
    long = domain.fit_one("so'z " * 2000)
    assert domain.tg_len(long) <= domain.TEXT_LIMIT
    assert long.endswith("\n…")


# --- the .md fallback and captions ---------------------------------------------------------------------------------
def test_up_to_max_parts_go_as_messages() -> None:
    text = "\n\n".join("band " * 700 for _ in range(3))
    plan = domain.plan_text(text, max_parts=3, file_stem="result-17")
    assert len(plan.parts) == 3
    assert plan.file is None


def test_more_than_max_parts_becomes_one_md_file_with_a_one_line_summary() -> None:
    text = "BonviHR · Natija: hisobot tayyor\n\n" + "\n\n".join("band " * 700 for _ in range(5))
    plan = domain.plan_text(text, max_parts=3, file_stem="result:17")
    assert plan.parts == ()
    assert plan.file is not None
    assert plan.file.summary == "BonviHR · Natija: hisobot tayyor"
    assert plan.file.filename == "result-17.md"
    assert plan.file.content == text


def test_the_summary_skips_fences_and_is_cut() -> None:
    assert domain.summary_line("\n```py\nprint(1)\n```") == "print(1)"
    assert domain.tg_len(domain.summary_line("a" * 500)) == domain.SUMMARY_LIMIT
    assert domain.summary_line("") == "…"
    assert domain.file_name("::") == "matn.md"


def test_an_empty_text_is_refused() -> None:
    with pytest.raises(ValueError, match="empty"):
        domain.plan_text("  ", max_parts=3, file_stem="x")


def test_a_long_caption_keeps_1024_and_the_rest_follows_as_text() -> None:
    caption = "\n\n".join("izoh " * 100 for _ in range(4))
    first, rest = domain.plan_caption(caption, max_parts=3, file_stem="file-1")
    assert domain.tg_len(first) <= domain.CAPTION_LIMIT
    assert rest is not None
    assert rest.parts
    assert domain.plan_caption("qisqa izoh", max_parts=3, file_stem="file-1") == ("qisqa izoh", None)


# --- keys ----------------------------------------------------------------------------------------------------------
def test_dedupe_keys_carry_their_kind() -> None:
    key = domain.dedupe_key(Kind.RESULT, "turn-17", 2)
    assert key == "result:turn-17:2"
    assert domain.kind_of_key(key) is Kind.RESULT
    assert domain.kind_of_key("nonsense:1:0") is None
    assert domain.op_key(Kind.ACK, "update-5", Op.DELETE) == "ack:update-5:delete"
    with pytest.raises(ValueError, match="at most"):
        domain.dedupe_key(Kind.RESULT, "x" * 200, 0)
    with pytest.raises(ValueError, match="ref"):
        domain.dedupe_key(Kind.RESULT, "", 0)


# --- payloads: each op has its own keys, and nothing else (UC-12) --------------------------------------------------
def _scrub(text: str) -> str:
    return text.replace("SECRET", "••••")


@pytest.mark.parametrize(
    ("op", "payload"),
    [
        (Op.SEND_TEXT, Payload(text="salom", question_id=4)),
        (Op.SEND_TEXT, Payload(after="ack:u-1:delete", text_if_after_failed="o'chmadi")),
        (Op.EDIT_TEXT, Payload(text="yangi", target="progress:1:0", every_s=5.0)),
        (Op.EDIT_TEXT, Payload(text="yangi", message_id=77)),
        (Op.DELETE, Payload(message_id=5)),
        (Op.REACT, Payload(message_id=5, emoji="\U0001f440")),
        (Op.CHAT_ACTION, Payload(action="typing", until=T0)),
        (Op.PIN, Payload(target="heartbeat:online:0")),
        (Op.SEND_DOCUMENT, Payload(content="# matn", filename="matn.md", mime="text/markdown", caption="izoh")),
        (Op.SEND_VOICE, Payload(path="/tmp/v.ogg", duration_s=12)),
    ],
    ids=lambda value: value.value if isinstance(value, Op) else "",
)
def test_each_op_payload_round_trips(op: Op, payload: Payload) -> None:
    assert domain.payload_from_json(op, domain.payload_json(op, payload, _scrub)) == payload


def test_stored_text_fields_are_scrubbed() -> None:
    stored = domain.payload_json(Op.SEND_TEXT, Payload(text="kalit SECRET"), _scrub)
    assert stored == {"text": "kalit ••••"}


@pytest.mark.parametrize(
    ("op", "payload", "reason"),
    [
        (Op.SEND_TEXT, Payload(), "needs text"),
        (Op.SEND_TEXT, Payload(text="a", text_if_after_failed="b"), "needs `after`"),
        (Op.SEND_TEXT, Payload(text="a", message_id=3), "takes no"),
        (Op.EDIT_TEXT, Payload(text="a"), "one target"),
        (Op.EDIT_TEXT, Payload(text="a", message_id=1, target="x:y:0"), "one target"),
        (Op.REACT, Payload(message_id=1), "emoji"),
        (Op.CHAT_ACTION, Payload(action="dance", until=T0), "action"),
        (Op.SEND_PHOTO, Payload(caption="a"), "path or content"),
        (Op.SEND_DOCUMENT, Payload(content="a"), "filename"),
    ],
)
def test_a_payload_that_does_not_fit_its_op_is_refused(op: Op, payload: Payload, reason: str) -> None:
    with pytest.raises(PayloadInvalid, match=reason):
        domain.payload_json(op, payload, _scrub)


def test_a_stored_keyboard_refuses_the_whole_row() -> None:
    keyboard: JsonValue = {"inline_keyboard": [[{"text": "Ha", "callback_data": "yes"}]]}
    with pytest.raises(PayloadInvalid, match="reply_markup"):
        domain.payload_from_json(Op.SEND_TEXT, {"text": "Bajaraymi?", "reply_markup": keyboard})
    with pytest.raises(PayloadInvalid, match="unexpected"):
        domain.payload_from_json(Op.EDIT_TEXT, {"text": "x", "message_id": 1, "keyboard": []})


@pytest.mark.parametrize(
    "raw",
    [None, ["text"], {"text": 5}, {"text": "a", "question_id": True}, {"text": "a", "question_id": "4"}],
)
def test_a_malformed_stored_payload_is_refused(raw: JsonValue) -> None:
    with pytest.raises(PayloadInvalid):
        domain.payload_from_json(Op.SEND_TEXT, raw)


def test_a_typing_end_must_be_an_aware_instant() -> None:
    with pytest.raises(PayloadInvalid, match="ISO"):
        domain.payload_from_json(Op.CHAT_ACTION, {"action": "typing", "until": "soon"})
    with pytest.raises(PayloadInvalid, match="aware"):
        domain.payload_from_json(Op.CHAT_ACTION, {"action": "typing", "until": "2026-10-23T09:00:00"})


# --- order ---------------------------------------------------------------------------------------------------------
def test_each_chat_goes_by_priority_then_age() -> None:
    rows = [_row(1, priority=2), _row(2, priority=0, kind=Kind.ACK), _row(3, chat_id=PRIVATE), _row(4, priority=1)]
    picked = domain.sendable(rows, now=T0, parked={})
    assert [row.id for row in picked if row.chat_id == GROUP] == [2, 4, 1]
    assert [row.id for row in picked if row.chat_id == PRIVATE] == [3]


def test_a_message_not_yet_due_holds_back_the_messages_after_it_but_not_out_of_band_rows() -> None:
    rows = [
        _row(1, due=5),  # retrying later
        _row(2),
        _row(3, op=Op.EDIT_TEXT, kind=Kind.PROGRESS),
        _row(4, op=Op.CHAT_ACTION, kind=Kind.ACK, priority=0),
    ]
    assert [row.id for row in domain.sendable(rows, now=T0, parked={})] == [4, 3]
    assert [row.id for row in domain.sendable(rows, now=_at(5), parked={})] == [4, 1, 2, 3]


def test_a_message_in_flight_holds_back_the_next_one() -> None:
    rows = [_row(1, status=Status.SENDING), _row(2)]
    assert domain.sendable(rows, now=T0, parked={}) == ()


def test_a_delete_keeps_its_place_before_the_reply_that_follows_it() -> None:
    rows = [_row(1, op=Op.DELETE, kind=Kind.ACK, priority=0, due=1), _row(2, kind=Kind.ACK, priority=0)]
    assert domain.sendable(rows, now=T0, parked={}) == ()


def test_a_parked_chat_gets_nothing_until_its_time() -> None:
    rows = [_row(1), _row(2, op=Op.REACT, kind=Kind.ACK), _row(3, chat_id=PRIVATE)]
    parked = {GROUP: _at(7)}
    assert [row.id for row in domain.sendable(rows, now=T0, parked=parked)] == [3]
    assert [row.id for row in domain.sendable(rows, now=_at(7), parked=parked)] == [1, 2, 3]


# --- rate limits (NFR-CAP-02) --------------------------------------------------------------------------------------
def test_one_message_per_second_per_chat() -> None:
    assert domain.next_send_at(now=T0, chat_id=PRIVATE, chat_sent=[], all_sent=[]) == T0
    later = domain.next_send_at(now=_at(0.3), chat_id=PRIVATE, chat_sent=[T0], all_sent=[T0])
    assert later == _at(1)


def test_twenty_messages_per_minute_per_group() -> None:
    sent = [_at(index * 2) for index in range(20)]  # every 2 s: the per-chat limit is never the reason
    now = _at(40)
    assert domain.next_send_at(now=now, chat_id=GROUP, chat_sent=sent, all_sent=sent) == _at(60)
    assert domain.next_send_at(now=now, chat_id=PRIVATE, chat_sent=sent, all_sent=sent) == now  # not a group


def test_thirty_calls_per_second_overall() -> None:
    everywhere = [_at(0.01 * index) for index in range(30)]
    assert domain.next_send_at(now=_at(0.5), chat_id=PRIVATE, chat_sent=[], all_sent=everywhere) == _at(1)
    assert domain.free_at(everywhere, domain.OVERALL, _at(1.5)) == _at(1.5)


def test_only_old_sends_are_forgotten() -> None:
    times = [T0, _at(30), _at(59)]
    assert domain.keep_recent(times, _at(60)) == [_at(30), _at(59)]
    assert domain.recent_cutoff(_at(60)) == T0


# --- coalescing and typing -----------------------------------------------------------------------------------------
def test_an_edit_waits_for_its_spacing_since_the_last_update() -> None:
    assert domain.edit_due_at(T0, None, 5.0) == T0
    assert domain.edit_due_at(_at(2), T0, 5.0) == _at(5)
    assert domain.edit_due_at(_at(9), T0, 5.0) == _at(9)
    assert domain.edit_due_at(_at(1), T0, None) == _at(1)


def test_typing_is_deduped_for_4_s_and_repeats_up_to_60_s() -> None:
    assert domain.first_typing_at(T0, None) == T0
    assert domain.first_typing_at(_at(1), T0) == _at(4)
    assert domain.first_typing_at(_at(4), T0) == _at(4)
    until = domain.typing_until(T0)
    assert until == _at(60)
    assert domain.next_typing_at(T0, until) == _at(4)
    assert domain.next_typing_at(_at(56), until) is None


def test_a_progress_line_is_no_answer_and_keeps_typing() -> None:
    """T34b: with `ack.typing_for_work` on, a turn's steps keep "typing…" up; its progress line must not end it."""
    assert not domain.ends_typing(Op.SEND_TEXT, Kind.PROGRESS)
    assert domain.ends_typing(Op.SEND_TEXT, Kind.ECHO)  # "→ BonviHR" is an answer


def test_a_reply_ends_typing_but_an_ack_does_not() -> None:
    assert domain.ends_typing(Op.SEND_TEXT, Kind.RESULT)
    assert domain.ends_typing(Op.SEND_VOICE, Kind.VOICE)
    assert not domain.ends_typing(Op.SEND_TEXT, Kind.ACK)
    assert not domain.ends_typing(Op.EDIT_TEXT, Kind.PROGRESS)


# --- preparing a call ----------------------------------------------------------------------------------------------
def test_a_conditional_reply_waits_for_its_delete_then_picks_its_text() -> None:
    payload = Payload(text="saqlandi", after="ack:u-1:delete", text_if_after_failed="saqlandi; almashtiring")
    for waiting in (Status.PENDING, Status.SENDING):
        assert domain.prepare(Op.SEND_TEXT, payload, Resolved(after_status=waiting), now=T0) == NotYet(_at(1))
    assert domain.prepare(Op.SEND_TEXT, payload, Resolved(after_status=Status.SENT), now=T0) == Ready("saqlandi")
    failed = domain.prepare(Op.SEND_TEXT, payload, Resolved(after_status=Status.FAILED), now=T0)
    assert failed == Ready("saqlandi; almashtiring")
    empty = replace(payload, text=None)
    assert domain.prepare(Op.SEND_TEXT, empty, Resolved(after_status=Status.SENT), now=T0) == Drop(
        Status.CANCELLED, Code.NOTHING_TO_SEND
    )


def test_an_edit_needs_its_message_sent_and_its_spacing_kept() -> None:
    payload = Payload(text="12 qadam", target="progress:7:0", every_s=5.0)
    queued = Resolved(target_status=Status.PENDING)
    assert domain.prepare(Op.EDIT_TEXT, payload, queued, now=T0) == NotYet(_at(1))
    gone = Resolved(target_status=Status.FAILED)
    assert domain.prepare(Op.EDIT_TEXT, payload, gone, now=T0) == Drop(Status.FAILED, Code.TARGET_MISSING)
    assert domain.prepare(Op.EDIT_TEXT, payload, Resolved(), now=T0) == Drop(Status.FAILED, Code.TARGET_MISSING)
    recent = Resolved(target_status=Status.SENT, target_message_id=900, last_sent_at=_at(-2))
    assert domain.prepare(Op.EDIT_TEXT, payload, recent, now=T0) == NotYet(_at(3))
    ready = Resolved(target_status=Status.SENT, target_message_id=900, last_sent_at=_at(-6))
    assert domain.prepare(Op.EDIT_TEXT, payload, ready, now=T0) == Ready("12 qadam", message_id=900)


def test_typing_past_its_end_is_dropped() -> None:
    payload = Payload(action="typing", until=_at(60))
    assert domain.prepare(Op.CHAT_ACTION, payload, Resolved(), now=_at(59)) == Ready()
    assert domain.prepare(Op.CHAT_ACTION, payload, Resolved(), now=_at(60)) == Drop(Status.CANCELLED, Code.TYPING_ENDED)


# --- after a failure -----------------------------------------------------------------------------------------------
def test_failures_follow_the_telegram_adapter_contract() -> None:
    timeout = CallTimedOut(operation="sendMessage")
    assert domain.failure_of(Op.SEND_TEXT, timeout) is Failure.UNKNOWN_DELIVERY
    assert domain.failure_of(Op.DELETE, timeout) is Failure.TRANSIENT
    assert domain.failure_of(Op.SEND_TEXT, RateLimited(provider="telegram", retry_after=3)) is Failure.RATE_LIMITED
    assert domain.failure_of(Op.SEND_TEXT, ProviderUnavailable(provider="telegram", retryable=False)) is (
        Failure.REFUSED
    )
    assert domain.failure_of(Op.REACT, AuthFailed(provider="telegram")) is Failure.REFUSED
    assert domain.timeout_failure(Op.SEND_DOCUMENT) is Failure.UNKNOWN_DELIVERY
    assert domain.timeout_failure(Op.EDIT_TEXT) is Failure.TRANSIENT


def test_a_rate_limit_parks_the_chat_and_gives_the_attempt_back() -> None:
    row = _row(1, attempts=4)
    parked = domain.after_failure(row, Failure.RATE_LIMITED, error_code="rate_limited", now=T0, retry_after=7)
    assert parked == Park(until=_at(7), attempts=3)
    without = domain.after_failure(row, Failure.RATE_LIMITED, error_code="rate_limited", now=T0)
    assert without == Park(until=_at(5), attempts=3)


def test_a_refusal_gives_up_at_once() -> None:
    assert domain.after_failure(_row(1), Failure.REFUSED, error_code="auth_failed", now=T0) == GiveUp("auth_failed")


def test_an_unknown_delivery_is_resent_under_the_same_key_unless_a_duplicate_costs_more() -> None:
    result = _row(1, kind=Kind.RESULT, attempts=1)
    assert domain.after_failure(result, Failure.UNKNOWN_DELIVERY, error_code="call_timed_out", now=T0) == Retry(
        at=_at(1), attempts=1
    )
    for kind in (Kind.ECHO, Kind.HEARTBEAT, Kind.VOICE):
        once = _row(2, kind=kind)
        decision = domain.after_failure(once, Failure.UNKNOWN_DELIVERY, error_code="call_timed_out", now=T0)
        assert decision == GiveUp(Code.DELIVERY_UNKNOWN), kind
    assert domain.resend_on_unknown("question:q-1:0")
    assert not domain.resend_on_unknown("echo:route-4:0")


def test_transient_failures_back_off_and_end_per_op() -> None:
    assert [domain.backoff(n).total_seconds() for n in (1, 2, 3, 6, 7, 30)] == [1, 2, 4, 32, 60, 60]
    react = _row(1, op=Op.REACT, kind=Kind.ACK, attempts=domain.ACK_MAX_ATTEMPTS)
    assert domain.after_failure(react, Failure.TRANSIENT, error_code="call_timed_out", now=T0) == GiveUp(
        "call_timed_out"
    )
    edit = _row(2, op=Op.EDIT_TEXT, kind=Kind.PROGRESS, attempts=2)
    assert domain.after_failure(edit, Failure.TRANSIENT, error_code="provider_unavailable", now=T0) == Retry(
        at=_at(2), attempts=2
    )
    message = _row(3, attempts=domain.MESSAGE_MAX_ATTEMPTS - 1)
    assert isinstance(domain.after_failure(message, Failure.UNKNOWN_DELIVERY, error_code="x", now=T0), Retry)
    assert domain.max_attempts(Op.SEND_TEXT) > domain.max_attempts(Op.EDIT_TEXT) > domain.max_attempts(Op.DELETE)


# --- timeouts and retention ----------------------------------------------------------------------------------------
def test_each_call_has_a_bound_that_fits_its_work() -> None:
    config = domain.DEFAULT_CONFIG
    assert domain.call_timeout(Op.DELETE, Kind.ACK, config) == config.ack_timeout_s
    assert domain.call_timeout(Op.SEND_TEXT, Kind.QUESTION, config) == config.text_timeout_s
    assert domain.call_timeout(Op.SEND_DOCUMENT, Kind.FILE, config) == config.file_timeout_s


def test_sent_rows_are_kept_30_days_and_links_90() -> None:
    assert domain.sent_cutoff(T0) == T0 - timedelta(days=30)
    assert domain.link_cutoff(T0) == T0 - timedelta(days=90)
    assert {Status.SENT, Status.FAILED, Status.SUPERSEDED, Status.CANCELLED} == domain.TERMINAL
