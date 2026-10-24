"""burst.domain on virtual instants: the SPEC §4.4 window (text, media, albums, voice HOLD, "kut", the hard cap),
placement and interrupt words (UC-03), transcripts, the turn input, the follow-up merge decision (UC-02), the stored
form and retention."""

from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from tests.factories import make_default_snapshot, make_part, make_window_settings
from tests.fakes.clock import DEFAULT_START
from yordamchi.core.paths import BotName
from yordamchi.shared.burst import domain
from yordamchi.shared.burst.domain import (
    BurstEvents,
    BurstPart,
    BurstStatus,
    CloseReason,
    FollowUpReason,
    HoldReason,
    InterruptKind,
    Into,
    PartRole,
    RouteHint,
    RunningTurn,
    TranscriptState,
    TurnInput,
)
from yordamchi.shared.inbox.domain import Attachment, Content, UpdateKind
from yordamchi.shared.settings import defaults

WS = make_window_settings()
MIN_CONFIDENCE = make_default_snapshot(BotName.DEV)[defaults.BURST_MERGE_MIN_CONFIDENCE]


def at(seconds: float) -> datetime:
    return DEFAULT_START + timedelta(seconds=seconds)


def closes(parts: Sequence[BurstPart]) -> float:
    """Seconds after the first instant when the burst closes if nothing else arrives."""
    return (domain.window(parts, WS).closes_at - DEFAULT_START).total_seconds()


def voice(update_id: int, *, at_s: float, duration_s: int = 5) -> BurstPart:
    return make_part(update_id, at_s=at_s, kind=UpdateKind.VOICE, duration_s=duration_s)


def transcribed(parts: Sequence[BurstPart], update_id: int, text: str, *, at_s: float) -> tuple[BurstPart, ...]:
    outcome = domain.apply_transcript(parts, update_id, text=text, ok=True, at=at(at_s))
    assert (outcome.alone, outcome.close_now) == (None, None)
    return outcome.parts


def hold(update_id: int, *, at_s: float) -> BurstPart:
    return replace(make_part(update_id, at_s=at_s, text="kut"), role=PartRole.HOLD)


def turn(parts: Sequence[BurstPart], *, burst_id: int = 1, reason: CloseReason = CloseReason.QUIET) -> TurnInput:
    closed_at = max(part.at for part in parts)
    return domain.turn_input(burst_id, -100, parts, opened_at=parts[0].at, closed_at=closed_at, reason=reason)


# --- settings -------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("bot", list(BotName), ids=str)
def test_the_window_settings_are_read_from_the_snapshot(bot: BotName) -> None:
    settings = domain.window_settings(make_default_snapshot(bot))
    assert settings == WS
    assert (settings.after_complete_text_s, settings.after_media_s) == (
        defaults.BURST_AFTER_COMPLETE_TEXT_S.default,
        defaults.BURST_AFTER_MEDIA_S.default,
    )


# --- text -----------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("text", "complete"),
    [
        ("BonviHR qanday ishlayapti?", True),
        ("Login sahifasini tuzat.", True),
        ("Hammasi tayyor bo'lsin!", True),
        ("BonviHR qanday?", False),  # 2 words
        ("login sahifasini tuzat", False),  # no final mark
        ("Login sahifasida xato bor...", False),  # an ellipsis says more is coming
        ("Login sahifasida xato bor…", False),
        ("— — tuzat?", False),  # dashes are not words
    ],
)
def test_a_complete_text_has_three_words_and_a_final_mark(text: str, complete: bool) -> None:
    assert domain.is_complete_text(text) is complete


def test_a_complete_text_closes_after_the_short_window() -> None:
    window = domain.window([make_part(text="BonviHR qanday ishlayapti?")], WS)
    assert window == domain.Window(at(WS.after_complete_text_s), CloseReason.QUIET)
    assert window.status is BurstStatus.OPEN


@pytest.mark.parametrize("text", ["login sahifasini tuzat", "BonviHR qanday?", "Login sahifasida xato bor..."])
def test_other_text_waits_after_text_s(text: str) -> None:
    assert closes([make_part(text=text)]) == WS.after_text_s


def test_a_text_ending_with_a_colon_waits_for_what_it_announces() -> None:
    assert closes([make_part(text="Mana xato:")]) == WS.after_media_s


def test_an_unknown_kind_without_text_waits_like_text() -> None:
    assert closes([make_part(kind=UpdateKind.OTHER)]) == WS.after_text_s


# --- media ----------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("kind", [UpdateKind.PHOTO, UpdateKind.VIDEO, UpdateKind.DOCUMENT, UpdateKind.AUDIO])
def test_a_bare_photo_or_file_waits_longer_than_text(kind: UpdateKind) -> None:
    assert closes([make_part(kind=kind)]) == WS.after_media_s
    assert WS.after_media_s > WS.after_text_s


def test_a_text_of_nothing_but_links_waits_like_media() -> None:
    assert closes([make_part(kind=UpdateKind.LINK, text="https://example.com/a")]) == WS.after_media_s


@pytest.mark.parametrize(
    ("caption", "quiet"),
    [
        ("Shu xatoni tuzating.", WS.after_complete_text_s),
        ("shu xato", WS.after_text_s),
        ("https://bonvi.uz/xato", WS.after_media_s),
    ],
)
def test_a_captioned_photo_follows_the_rule_of_its_caption(caption: str, quiet: float) -> None:
    assert closes([make_part(caption=caption)]) == quiet


def test_an_album_stays_open_until_the_gap_after_its_last_item() -> None:
    album = [
        make_part(1, at_s=0.0, caption="Bu uchta xatoni tuzat.", media_group_id="g1"),
        make_part(2, at_s=0.3, media_group_id="g1"),
        make_part(3, at_s=0.6, media_group_id="g1"),
    ]
    assert closes(album) == pytest.approx(0.6 + WS.album_gap_s)  # the complete caption alone would allow 1.2 s
    assert WS.album_gap_s > WS.after_complete_text_s


def test_an_album_without_a_caption_waits_like_a_bare_photo() -> None:
    album = [make_part(i, at_s=0.2 * i, media_group_id="g1") for i in range(4)]
    assert closes(album) == pytest.approx(0.6 + WS.after_media_s)


def test_a_photo_then_text_waits_only_after_the_text() -> None:
    assert closes([make_part(1, at_s=0), make_part(2, at_s=6, text="shu xatoni tuzat")]) == 6 + WS.after_text_s


# --- voice ----------------------------------------------------------------------------------------------------------
def test_a_voice_part_being_transcribed_holds_the_window() -> None:
    window = domain.window([voice(1, at_s=0)], WS)
    assert window == domain.Window(at(WS.hold_max_s), CloseReason.HOLD_EXPIRED, HoldReason.TRANSCRIPT)
    assert window.status is BurstStatus.HOLDING


def test_the_transcript_releases_the_hold_after_after_voice_s() -> None:
    parts = transcribed([voice(1, at_s=0)], 1, "login sahifasini tuzat", at_s=3)
    assert domain.window(parts, WS) == domain.Window(at(3 + WS.after_voice_s), CloseReason.QUIET)
    assert parts[0].transcript is TranscriptState.DONE
    assert parts[0].text == "login sahifasini tuzat"


def test_every_transcript_extends_the_window_by_after_voice_s() -> None:
    late = transcribed([voice(1, at_s=0), make_part(2, at_s=2)], 1, "mana", at_s=8)
    assert closes(late) == 2 + WS.after_media_s  # the photo's window is longer
    after_text = transcribed([make_part(1, at_s=0, text="tuzat"), voice(2, at_s=1)], 2, "tez", at_s=9)
    assert closes(after_text) == 9 + WS.after_voice_s


def test_a_pending_transcript_is_not_cut_by_the_hard_cap() -> None:
    parts = (make_part(1, at_s=0), voice(2, at_s=25))
    window = domain.window(parts, WS)
    assert (window.hold, window.closes_at) == (HoldReason.TRANSCRIPT, at(25 + WS.hold_max_s))
    released = transcribed(parts, 2, "ha", at_s=33)
    assert domain.window(released, WS) == domain.Window(at(WS.max_total_s), CloseReason.CAP)  # past: close at once


# --- "kut" and the hard cap -----------------------------------------------------------------------------------------
def test_kut_holds_the_window_up_to_hold_max() -> None:
    window = domain.window([make_part(1, at_s=0), hold(2, at_s=1)], WS)
    assert window == domain.Window(at(1 + WS.hold_max_s), CloseReason.HOLD_EXPIRED, HoldReason.OWNER)


def test_the_time_kut_held_is_exempt_from_the_hard_cap() -> None:
    parts = [make_part(1, at_s=0), hold(2, at_s=5), make_part(3, at_s=65), make_part(4, at_s=74)]
    assert closes(parts) == 74 + WS.after_media_s  # 84 s: the 60 s hold does not count towards the 30 s cap
    longer = [*parts, make_part(5, at_s=83)]
    assert domain.window(longer, WS) == domain.Window(at(WS.max_total_s + 60), CloseReason.CAP)


def test_the_hard_cap_counts_from_the_first_part() -> None:
    parts = [make_part(i, at_s=9.0 * i) for i in range(4)]  # each bare photo would wait another 10 s
    assert domain.window(parts, WS) == domain.Window(at(WS.max_total_s), CloseReason.CAP)


def test_a_burst_has_at_least_one_part() -> None:
    with pytest.raises(ValueError, match="at least one part"):
        domain.window([], WS)


def test_a_window_admits_a_part_at_its_boundary_and_is_due_from_it() -> None:
    window = domain.window([make_part(text="tuzat")], WS)
    assert window.admits(at(WS.after_text_s))
    assert not window.admits(at(WS.after_text_s + 0.001))
    assert not window.is_due(at(WS.after_text_s - 0.001))
    assert window.is_due(at(WS.after_text_s))


# --- interrupt words ------------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("to'xta", InterruptKind.STOP),
        ("To'xta!", InterruptKind.STOP),
        ("toʻxta", InterruptKind.STOP),
        ("to‘xta", InterruptKind.STOP),
        ("TOXTA", InterruptKind.STOP),
        ("Stop.", InterruptKind.STOP),
        ("bas", InterruptKind.STOP),
        ("bas qil", InterruptKind.STOP),
        ("стоп", InterruptKind.STOP),
        ("to'xta, bu noto'g'ri", InterruptKind.STOP),
        ("hammasini to'xtat", InterruptKind.STOP_ALL),
        ("Hammasini toʻxtat!", InterruptKind.STOP_ALL),
        ("stop all", InterruptKind.STOP_ALL),
        ("kut", InterruptKind.HOLD),
        ("Kutib tur", InterruptKind.HOLD),
        ("wait", InterruptKind.HOLD),
        ("kut, rasm yuboraman", InterruptKind.HOLD),
        ("yo'q, boshqacha qil: login sahifasini qayta yoz", InterruptKind.REDIRECT),
        ("Yoʻq boshqacha qil", InterruptKind.REDIRECT),
    ],
)
def test_interrupt_words_are_recognised_in_their_spellings(text: str, kind: InterruptKind) -> None:
    assert domain.interrupt_of(text) is kind


@pytest.mark.parametrize(
    "text",
    ["serverni to'xtat", "stop the server", "BonviHR'da login sahifasini tuzat", "kutubxonani yangila", "yo'q", ""],
)
def test_an_instruction_that_mentions_a_stop_word_is_not_an_interrupt(text: str) -> None:
    assert domain.interrupt_of(text) is None


def test_a_caption_is_never_an_interrupt() -> None:
    assert domain.interrupt_of_part(make_part(caption="to'xta")) is None
    assert domain.interrupt_of_part(make_part(text="to'xta")) is InterruptKind.STOP


# --- placement ------------------------------------------------------------------------------------------------------
def test_a_part_inside_the_window_joins_the_assembling_burst() -> None:
    current = [make_part(1, at_s=0)]
    text = make_part(2, at_s=6, text="shu xatoni tuzat")
    placement = domain.place(text, current, WS)
    assert placement == domain.Placement(Into.CURRENT, text)
    assert domain.joined(current, placement) == (current[0], text)


def test_a_part_after_the_window_opens_a_new_burst_and_the_old_closes_for_its_own_reason() -> None:
    current = [make_part(1, at_s=0, text="login sahifasini tuzat")]
    late = make_part(2, at_s=WS.after_text_s + 0.5, text="registratsiyani ham")
    assert domain.place(late, current, WS) == domain.Placement(Into.NEW, late, close_current=CloseReason.QUIET)
    on_time = make_part(2, at_s=WS.after_text_s, text="registratsiyani ham")
    assert domain.place(on_time, current, WS).into is Into.CURRENT
    assert domain.place(late, [], WS) == domain.Placement(Into.NEW, late)


def test_an_interrupt_word_closes_the_assembling_burst_at_once_and_stands_alone() -> None:
    stop = make_part(2, at_s=1, text="to'xta")
    placement = domain.place(stop, [make_part(1, at_s=0)], WS)
    assert placement == domain.Placement(
        Into.ALONE, stop, close_current=CloseReason.INTERRUPTED, close_alone=CloseReason.INTERRUPT
    )
    nothing_assembling = domain.place(stop, [], WS)
    assert (nothing_assembling.into, nothing_assembling.close_current) == (Into.ALONE, None)


def test_an_interrupt_leaves_a_burst_that_waits_for_a_transcript_assembling() -> None:
    placement = domain.place(make_part(2, at_s=1, text="to'xta"), [voice(1, at_s=0)], WS)
    assert (placement.into, placement.close_current, placement.close_alone) == (
        Into.ALONE,
        None,
        CloseReason.INTERRUPT,
    )


def test_kut_while_assembling_joins_as_a_hold() -> None:
    placement = domain.place(make_part(2, at_s=1, text="kut"), [make_part(1, at_s=0)], WS)
    assert (placement.into, placement.part.role) == (Into.CURRENT, PartRole.HOLD)


def test_kut_with_nothing_assembling_opens_a_hold_for_the_next_part_and_stops_nothing() -> None:
    kut = make_part(2, at_s=WS.after_text_s + 1, text="kut")
    alone = domain.place(kut, [], WS)
    assert (alone.into, alone.part.role, alone.close_current, alone.close_alone) == (
        Into.NEW,
        PartRole.HOLD,
        None,
        None,
    )
    window = domain.window([alone.part], WS)
    assert window == domain.Window(
        kut.at + timedelta(seconds=WS.hold_max_s), CloseReason.HOLD_EXPIRED, HoldReason.OWNER
    )
    stale = domain.place(kut, [make_part(1, at_s=0, text="tuzat")], WS)  # the old burst's window had ended
    assert (stale.into, stale.close_current, stale.part.role) == (Into.NEW, CloseReason.QUIET, PartRole.HOLD)


def test_the_part_after_a_lone_kut_joins_its_burst_and_the_window_follows_that_part() -> None:
    kut = hold(1, at_s=0)
    photo = make_part(2, at_s=60)
    placement = domain.place(photo, [kut], WS)
    assert placement.into is Into.CURRENT
    parts = domain.joined([kut], placement)
    assert closes(parts) == 60 + WS.after_media_s  # the hold is over: the photo's own quiet window
    assert [part.update_id for part in turn(parts).parts] == [2]  # the "kut" never reaches the turn


def test_a_lone_kut_has_no_content_and_a_stop_after_it_is_still_an_interrupt() -> None:
    assert not domain.has_content([hold(1, at_s=0)])
    assert domain.has_content([hold(1, at_s=0), make_part(2, at_s=1)])
    stop = domain.place(make_part(2, at_s=5, text="to'xta"), [hold(1, at_s=0)], WS)
    assert (stop.into, stop.close_alone, stop.close_current) == (
        Into.ALONE,
        CloseReason.INTERRUPT,
        CloseReason.INTERRUPTED,
    )
    with pytest.raises(ValueError, match="lapses"):
        turn([hold(1, at_s=0)])


def test_an_edit_replaces_its_part_in_place() -> None:
    original = make_part(1, at_s=0, text="login tuzat", message_id=7)
    photo = make_part(2, at_s=1)
    edit = make_part(3, at_s=1.5, kind=UpdateKind.EDITED, text="login sahifasini tuzat", message_id=7)
    placement = domain.place(edit, [original, photo], WS)
    assert placement.into is Into.EDIT
    parts = domain.joined([original, photo], placement)
    assert [(part.update_id, part.text) for part in parts] == [(1, "login sahifasini tuzat"), (2, None)]


def test_an_edited_caption_keeps_the_file() -> None:
    original = make_part(1, at_s=0, caption="eski", message_id=7)
    edit = make_part(2, at_s=1, kind=UpdateKind.EDITED, caption="yangi izoh", message_id=7)
    [part] = domain.joined([original], domain.place(edit, [original], WS))
    assert (part.kind, part.text, part.content.attachment) == (
        UpdateKind.PHOTO,
        "yangi izoh",
        original.content.attachment,
    )


def test_an_edit_of_a_message_outside_the_burst_is_a_new_part() -> None:
    edit = make_part(3, at_s=1, kind=UpdateKind.EDITED, text="boshqa", message_id=99)
    assert domain.place(edit, [make_part(1, at_s=0, message_id=7)], WS).into is Into.CURRENT


def test_the_self_test_stands_alone_and_leaves_the_owners_burst_alone() -> None:
    selftest = make_part(2, at_s=1, text="selftest", synthetic=True)
    placement = domain.place(selftest, [make_part(1, at_s=0)], WS)
    assert placement == domain.Placement(Into.ALONE, selftest, close_alone=CloseReason.SYNTHETIC)


# --- transcripts ----------------------------------------------------------------------------------------------------
def test_a_short_voice_stop_leaves_the_burst_which_closes_at_once() -> None:
    photo = make_part(1, at_s=0)
    outcome = domain.apply_transcript((photo, voice(2, at_s=1)), 2, text="To'xta.", ok=True, at=at(2))
    assert outcome.parts == (photo,)
    assert outcome.alone is not None
    assert (outcome.alone.update_id, outcome.alone.transcript, outcome.close_now) == (
        2,
        TranscriptState.DONE,
        CloseReason.INTERRUPTED,
    )


def test_a_voice_stop_that_is_the_only_content_is_the_interrupt_itself() -> None:
    outcome = domain.apply_transcript((voice(1, at_s=0),), 1, text="to'xta", ok=True, at=at(1))
    assert (len(outcome.parts), outcome.alone, outcome.close_now) == (1, None, CloseReason.INTERRUPT)
    assert turn(outcome.parts, reason=CloseReason.INTERRUPT).interrupt is InterruptKind.STOP


def test_a_voice_kut_that_is_the_only_content_holds_and_stops_nothing() -> None:
    outcome = domain.apply_transcript((voice(1, at_s=0),), 1, text="Kut.", ok=True, at=at(1))
    assert (outcome.alone, outcome.close_now, outcome.parts[0].role) == (None, None, PartRole.HOLD)
    assert domain.window(outcome.parts, WS).hold is HoldReason.OWNER


def test_a_voice_kut_among_content_holds_the_burst() -> None:
    outcome = domain.apply_transcript((make_part(1, at_s=0), voice(2, at_s=1)), 2, text="kut", ok=True, at=at(2))
    assert (outcome.alone, outcome.close_now, outcome.parts[1].role) == (None, None, PartRole.HOLD)
    assert domain.window(outcome.parts, WS).hold is HoldReason.OWNER


def test_a_voice_stop_with_another_transcript_pending_keeps_the_burst_assembling() -> None:
    outcome = domain.apply_transcript((voice(1, at_s=0), voice(2, at_s=1)), 2, text="stop", ok=True, at=at(2))
    assert ([part.update_id for part in outcome.parts], outcome.close_now) == ([1], None)
    assert outcome.alone is not None


def test_a_long_transcript_that_mentions_stop_is_content() -> None:
    text = "BonviHR'dagi serverni to'xtat va qayta ishga tushir"
    outcome = domain.apply_transcript((voice(1, at_s=0),), 1, text=text, ok=True, at=at(1))
    assert (outcome.alone, outcome.close_now, outcome.parts[0].text) == (None, None, text)


def test_a_failed_transcript_stays_in_the_burst_and_is_never_an_interrupt() -> None:
    outcome = domain.apply_transcript((voice(1, at_s=0),), 1, text="to'xta", ok=False, at=at(4))
    assert (outcome.alone, outcome.close_now, outcome.parts[0].transcript) == (None, None, TranscriptState.FAILED)
    assert closes(outcome.parts) == 4 + WS.after_voice_s


def test_a_transcript_for_a_part_that_does_not_wait_is_refused() -> None:
    assert not domain.waits_for([make_part(1)], 1)
    with pytest.raises(ValueError, match="not waiting"):
        domain.apply_transcript([make_part(1)], 1, text="x", ok=True, at=at(1))


def test_a_voice_note_began_its_duration_before_it_arrived() -> None:
    assert voice(1, at_s=62, duration_s=60).started_at == at(62 - 60) - domain.DURATION_PRECISION
    assert make_part(2, at_s=5, text="x").started_at == at(5)


# --- the turn input -------------------------------------------------------------------------------------------------
def test_the_turn_input_keeps_content_in_order_and_leaves_kut_out() -> None:
    parts = [make_part(1, at_s=0), hold(2, at_s=1), make_part(3, at_s=40, text="shu xatoni tuzat", message_id=9)]
    item = turn(parts)
    assert [part.update_id for part in item.parts] == [1, 3]
    assert (item.interrupt, item.synthetic, item.last_part_at, item.message_ids) == (
        None,
        False,
        at(40),
        frozenset({5001, 9}),
    )


def test_a_turn_input_needs_content() -> None:
    with pytest.raises(ValueError, match="no content"):
        turn([hold(1, at_s=0)])


def test_an_interrupt_burst_says_which_interrupt_it_is() -> None:
    assert turn([make_part(text="hammasini to'xtat")], reason=CloseReason.INTERRUPT).interrupt is InterruptKind.STOP_ALL
    assert turn([make_part(text="to'xta")], reason=CloseReason.QUIET).interrupt is None


def test_only_bare_media_is_media_only() -> None:
    assert turn([make_part(1)]).is_media_only
    assert turn([make_part(i, at_s=0.2 * i, media_group_id="g") for i in range(3)]).is_media_only
    assert not turn([make_part(1), make_part(2, at_s=3, text="tuzat")]).is_media_only
    assert not turn([make_part(1, caption="shu xato")]).is_media_only


def test_interrupts_are_routed_first() -> None:
    first = turn([make_part(1)], burst_id=1)
    stop = turn([make_part(2, text="to'xta")], burst_id=2, reason=CloseReason.INTERRUPT)
    later = turn([make_part(3)], burst_id=3)
    assert domain.in_route_order([later, first, stop]) == (stop, first, later)
    events = BurstEvents(closed=(first,)) + BurstEvents(closed=(stop,), transcribe=(voice(4, at_s=0),))
    assert ([item.burst_id for item in events.closed], len(events.transcribe), bool(events)) == ([2, 1], 1, True)
    assert not BurstEvents()


# --- follow-ups (UC-02) ---------------------------------------------------------------------------------------------
def running_after(first: TurnInput, *, project_id: str | None = "BonviHR", answered: bool = False) -> RunningTurn:
    return RunningTurn(
        last_part_at=first.last_part_at,
        project_id=project_id,
        message_ids=first.message_ids,
        media_group_ids=first.media_group_ids,
        answered=answered,
    )


FIRST = turn([make_part(1, at_s=0, text="BonviHR'da login sahifasini tuzat", message_id=700)])


def decide(follow_up: TurnInput, route: RouteHint, running: RunningTurn | None = None) -> domain.FollowUpDecision:
    return domain.decide_dev_follow_up(
        follow_up, running or running_after(FIRST), route=route, settings=WS, min_confidence=MIN_CONFIDENCE
    )


def test_an_unrelated_follow_up_is_never_merged() -> None:
    quick_reply = turn([make_part(2, at_s=1.5, text="HRMentor'da testlarni yurgiz", reply_to_message_id=700)])
    decision = decide(quick_reply, RouteHint("HRMentor", 0.99))
    assert (decision.merge, decision.reason) == (False, FollowUpReason.OTHER_PROJECT)


def test_a_follow_up_for_the_running_project_merges_only_when_confident() -> None:
    later = turn([make_part(2, at_s=40, text="registratsiya sahifasini ham")], burst_id=2)
    assert decide(later, RouteHint("BonviHR", MIN_CONFIDENCE)).reason is FollowUpReason.CONFIDENT
    assert decide(later, RouteHint("BonviHR", MIN_CONFIDENCE - 0.01)).reason is FollowUpReason.UNSURE
    assert not decide(later, RouteHint(None, 0.0)).merge


def test_a_follow_up_begun_right_after_the_turns_last_part_continues_it() -> None:
    first = turn([make_part(1, at_s=0, text="BonviHR'da login sahifasini tuzat.")])  # complete: closed at 1.2 s
    quick = turn([make_part(2, at_s=1.8, text="va registratsiyani ham")], burst_id=2)
    decision = decide(quick, RouteHint(None, 0.4), running_after(first))
    assert (decision.merge, decision.reason) == (True, FollowUpReason.CONTINUATION)


def test_a_long_voice_note_begun_inside_the_window_continues_the_turn() -> None:
    recorded_right_after = turn([voice(2, at_s=62, duration_s=60)], burst_id=2)
    assert decide(recorded_right_after, RouteHint(None, 0.3)).reason is FollowUpReason.CONTINUATION
    recorded_later = turn([voice(3, at_s=70, duration_s=60)], burst_id=3)
    assert decide(recorded_later, RouteHint(None, 0.3)).reason is FollowUpReason.UNSURE


def test_a_reply_into_the_turn_or_a_late_album_item_continues_it() -> None:
    reply = turn([make_part(2, at_s=300, text="buni ham", reply_to_message_id=700)], burst_id=2)
    assert decide(reply, RouteHint("BonviHR", 0.2)).reason is FollowUpReason.CONTINUATION
    album = turn([make_part(i, at_s=0.1 * i, media_group_id="g7") for i in range(1, 3)])
    straggler = turn([make_part(5, at_s=20, media_group_id="g7")], burst_id=2)
    assert decide(straggler, RouteHint(None, 0.0), running_after(album)).reason is FollowUpReason.CONTINUATION


def test_an_interrupt_is_never_merged() -> None:
    stop = turn([make_part(2, at_s=1, text="to'xta", reply_to_message_id=700)], reason=CloseReason.INTERRUPT)
    assert decide(stop, RouteHint("BonviHR", 1.0)).reason is FollowUpReason.INTERRUPT
    personal = domain.decide_personal_follow_up(stop, running_after(FIRST, project_id=None))
    assert (personal.merge, personal.reason) == (False, FollowUpReason.INTERRUPT)


def test_the_personal_bot_answers_a_follow_up_in_the_same_reply_only_before_it_is_sent() -> None:
    unrelated = turn([make_part(2, at_s=20, text="bugun ob-havo qanday?")], burst_id=2)
    writing = domain.decide_personal_follow_up(unrelated, running_after(FIRST, project_id=None))
    sent = domain.decide_personal_follow_up(unrelated, running_after(FIRST, project_id=None, answered=True))
    assert (writing.merge, writing.reason, sent.merge, sent.reason) == (
        True,
        FollowUpReason.UNANSWERED,
        False,
        FollowUpReason.ANSWERED,
    )


# --- input_json and retention ---------------------------------------------------------------------------------------
def test_parts_round_trip_and_every_content_string_is_scrubbed() -> None:
    parts = (
        make_part(1, at_s=0, caption="izoh", media_group_id="g1", reply_to_message_id=3),
        replace(
            transcribed((voice(2, at_s=1, duration_s=14),), 2, "salom", at_s=2)[0],
            role=PartRole.HOLD,
            synthetic=True,
        ),
        make_part(3, at_s=2, kind=UpdateKind.EDITED, text="matn"),
    )
    assert domain.parts_from_json(domain.parts_json(parts, lambda text: text)) == parts
    stored = domain.parts_json(parts, str.upper)
    assert isinstance(stored, list)
    first, second, _ = stored
    assert isinstance(first, dict)
    assert isinstance(second, dict)
    assert isinstance(first["content"], dict)
    assert (first["content"]["caption"], second["transcript_text"], first["kind"]) == ("IZOH", "SALOM", "photo")


def test_content_never_shows_in_a_repr() -> None:
    part = replace(make_part(text="maxfiy matn"), transcript_text="maxfiy ovoz")
    assert "maxfiy" not in repr(part)
    assert "maxfiy" not in repr(turn([part]))


def test_malformed_stored_parts_are_dropped() -> None:
    good = domain.parts_json([make_part(1)], str)
    assert isinstance(good, list)
    entry = good[0]
    assert isinstance(entry, dict)
    broken = [{**entry, "kind": "hologram"}, {**entry, "at": "yesterday"}, {**entry, "at": "2026-10-23T09:00:00"}]
    assert domain.parts_from_json([*broken, "x", entry]) == (make_part(1),)
    assert domain.parts_from_json(None) == ()
    assert domain.parts_from_json({"parts": []}) == ()


def test_a_content_without_attachment_or_text_is_still_a_part() -> None:
    bare = BurstPart(update_id=9, kind=UpdateKind.OTHER, at=at(0), content=Content())
    assert domain.parts_from_json(domain.parts_json([bare], str)) == (bare,)
    assert Attachment(file_id="f").duration_s is None


def test_finished_bursts_are_kept_for_30_days() -> None:
    now = at(0)
    assert domain.row_cutoff(now) == now - timedelta(days=30)
    assert domain.TERMINAL.isdisjoint(domain.ASSEMBLING | {BurstStatus.CLOSED})
