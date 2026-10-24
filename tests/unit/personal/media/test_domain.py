"""personal/media domain rules: requests from parts and links, limits, provider order, findings, defusing."""

import pytest

from tests.factories import make_default_snapshot, make_media_analysis, make_part, make_video_attachment
from yordamchi.core import strings as S
from yordamchi.core.errors import ErrorCode, MediaTooLarge, MediaTooLong, MediaVideoOff
from yordamchi.core.paths import BotName
from yordamchi.personal.media import domain
from yordamchi.personal.media.domain import MIB, Attempt, MediaKind, MediaLimits
from yordamchi.shared.cost.domain import Provider
from yordamchi.shared.inbox.domain import UpdateKind
from yordamchi.shared.settings.domain import Snapshot

LIMITS = MediaLimits(
    video_max_bytes=20 * MIB, video_max_s=3600, page_max_chars=30000, page_max_bytes=5 * MIB, budget_s=55.0
)


# --- links ----------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://youtube.com/watch?v=dQw4w9WgXcQ&t=42s",
        "https://m.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://youtu.be/dQw4w9WgXcQ?si=abc",
        "https://www.youtube.com/shorts/dQw4w9WgXcQ",
        "https://www.youtube.com/live/dQw4w9WgXcQ",
        "https://www.youtube.com/embed/dQw4w9WgXcQ",
    ],
)
def test_every_youtube_link_form_becomes_its_watch_url(url: str) -> None:
    assert domain.youtube_watch_url(url) == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    assert domain.classify_url(url) is MediaKind.YOUTUBE


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/@channel",
        "https://www.youtube.com/watch?v=short",
        "https://notyoutube.com/watch?v=dQw4w9WgXcQ",
        "https://youtu.be/",
        "http://[::1",
    ],
)
def test_other_links_are_not_youtube_videos(url: str) -> None:
    assert domain.youtube_watch_url(url) is None


def test_a_link_to_a_video_file_is_a_video_link_and_anything_else_a_page() -> None:
    assert domain.classify_url("https://cdn.example.com/clips/a.MP4?x=1") is MediaKind.VIDEO_LINK
    assert domain.classify_url("https://example.com/article/123") is MediaKind.PAGE
    assert domain.classify_url("https://example.com/doc.pdf") is MediaKind.PAGE


def test_first_url_drops_the_punctuation_after_a_link() -> None:
    assert domain.first_url("ko'r: https://example.com/a?b=1).") == "https://example.com/a?b=1"
    assert domain.first_url("havola yo'q") is None


def test_request_for_url_normalises_youtube_and_keeps_the_question() -> None:
    request = domain.request_for_url("https://youtu.be/dQw4w9WgXcQ", chat_id=1, ref="9", question="nima deydi?")
    assert request.kind is MediaKind.YOUTUBE
    assert request.url == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    assert request.question == "nima deydi?"


# --- parts ----------------------------------------------------------------------------------------------------------
def test_a_video_part_becomes_a_video_request_with_its_caption() -> None:
    part = make_part(3, kind=UpdateKind.VIDEO, caption="bu haqiqatmi?")
    request = domain.request_for_part(part, chat_id=42)
    assert request is not None
    assert request.kind is MediaKind.VIDEO
    assert request.attachment == part.content.attachment
    assert request.ref == "3"
    assert request.question == "bu haqiqatmi?"
    assert request.reply_to == part.message_id


def test_a_link_only_part_becomes_a_link_request() -> None:
    part = make_part(4, kind=UpdateKind.LINK, text="https://youtu.be/dQw4w9WgXcQ")
    request = domain.request_for_part(part, chat_id=42)
    assert request is not None
    assert request.kind is MediaKind.YOUTUBE


def test_photos_and_texts_are_not_media_requests() -> None:
    assert domain.request_for_part(make_part(5, kind=UpdateKind.PHOTO), chat_id=42) is None
    assert domain.request_for_part(make_part(6, text="salom"), chat_id=42) is None


def test_a_video_sent_as_a_file_counts_as_a_video() -> None:
    assert domain.is_video_attachment(UpdateKind.DOCUMENT, make_video_attachment(mime="video/quicktime"))
    assert not domain.is_video_attachment(UpdateKind.DOCUMENT, make_video_attachment(mime="application/pdf"))
    assert not domain.is_video_attachment(UpdateKind.VIDEO, None)


# --- limits ---------------------------------------------------------------------------------------------------------
def test_the_limits_come_from_the_settings(personal_snapshot: Snapshot) -> None:
    limits = domain.media_limits(personal_snapshot)
    assert limits.video_max_bytes == 20 * MIB
    assert limits.video_max_mb == 20
    assert limits.video_max_s == 60 * 60
    assert limits.page_max_chars == 30000
    assert limits.budget_s == 55.0


def test_a_video_over_20_mb_is_refused_before_download() -> None:
    with pytest.raises(MediaTooLarge) as caught:
        domain.check_video(make_video_attachment(size_bytes=20 * MIB + 1), LIMITS)
    assert caught.value.params == {"limit_mb": 20}
    assert "20 MB" in S.ERRORS[caught.value.code](**caught.value.params)


def test_a_video_of_exactly_the_cap_and_unknown_sizes_pass() -> None:
    domain.check_video(make_video_attachment(size_bytes=20 * MIB), LIMITS)
    domain.check_video(make_video_attachment(size_bytes=None, duration_s=None), LIMITS)


def test_a_video_longer_than_the_cap_is_refused() -> None:
    with pytest.raises(MediaTooLong) as caught:
        domain.check_video(make_video_attachment(duration_s=3601), LIMITS)
    assert caught.value.params == {"max_minutes": 60}


def test_the_cached_file_name_never_comes_from_the_sender() -> None:
    assert domain.video_file_name(make_video_attachment(mime="video/quicktime")) == "video.mov"
    assert domain.video_file_name(make_video_attachment(mime="")) == "video.mp4"


# --- providers ------------------------------------------------------------------------------------------------------
def test_auto_prefers_google_then_openrouter() -> None:
    assert domain.pick_video("auto", ["openrouter", "google"]) == ("google", "openrouter")
    assert domain.pick_video("auto", ["openrouter"]) == ("openrouter",)
    assert domain.pick_video("auto", []) == ()


def test_a_named_provider_goes_first_with_the_other_as_fallback() -> None:
    assert domain.pick_video("openrouter", ["google", "openrouter"]) == ("openrouter", "google")


def test_off_refuses() -> None:
    with pytest.raises(MediaVideoOff):
        domain.pick_video("off", ["google"])


def test_key_quota_rate_and_outage_failures_fall_back_but_a_timeout_does_not() -> None:
    for code in (
        ErrorCode.AUTH_FAILED,
        ErrorCode.QUOTA_EXHAUSTED,
        ErrorCode.RATE_LIMITED,
        ErrorCode.PROVIDER_UNAVAILABLE,
    ):
        assert domain.falls_back(code)
    assert not domain.falls_back(ErrorCode.CALL_TIMED_OUT)
    assert not domain.falls_back(ErrorCode.WEB_CONTENT_UNAVAILABLE)


def test_the_failure_text_names_each_provider_and_its_problem() -> None:
    text = domain.failure_text(
        [Attempt("google", ErrorCode.QUOTA_EXHAUSTED), Attempt("openrouter", ErrorCode.RATE_LIMITED)],
        {"provider": "openrouter"},
    )
    assert text.startswith("Videoni ko'ra olmadim")
    assert "Google Gemini — kvota yoki balans tugagan" in text
    assert "OpenRouter (Gemini) — so'rovlar limitiga urildi" in text


def test_a_content_failure_uses_its_own_message() -> None:
    text = domain.failure_text([Attempt("google", ErrorCode.WEB_CONTENT_UNAVAILABLE)], {"site": "youtube.com"})
    assert text == S.ERRORS[ErrorCode.WEB_CONTENT_UNAVAILABLE](site="youtube.com")


def test_usage_is_paid_only_when_the_provider_reported_a_cost() -> None:
    free = domain.usage_of(make_media_analysis())
    assert free.source == "video"
    assert free.provider is Provider.GOOGLE
    assert free.is_paid_api is False
    assert free.units == {"input_tokens": 3100, "output_tokens": 120}
    paid = domain.usage_of(make_media_analysis(provider="openrouter", cost_usd_micros=5370))
    assert paid.is_paid_api is True
    assert paid.cost_usd_micros == 5370


def test_the_model_follows_the_provider(personal_snapshot: Snapshot) -> None:
    assert domain.video_model("google", personal_snapshot) == "gemini-3.8-flash"
    assert domain.video_model("openrouter", personal_snapshot) == "google/gemini-3.8-flash"


# --- findings -------------------------------------------------------------------------------------------------------
def test_json_findings_are_read_inside_code_fences() -> None:
    findings = domain.parse_findings(
        '```json\n{"summary": " Bir  video. ", "key_points": ["a", "", 3, "b"], "opinion_hook": "Da\'vo."}\n```'
    )
    assert findings is not None
    assert findings.summary == "Bir video."
    assert findings.key_points == ("a", "b")
    assert findings.opinion_hook == "Da'vo."


def test_findings_are_capped() -> None:
    points = ", ".join(f'"{"x" * 500}"' for _ in range(12))
    findings = domain.parse_findings(f'{{"summary": "{"s" * 3000}", "key_points": [{points}]}}')
    assert findings is not None
    assert len(findings.summary) == domain.SUMMARY_MAX_CHARS
    assert len(findings.key_points) == domain.KEY_POINTS_MAX
    assert all(len(point) == domain.KEY_POINT_MAX_CHARS for point in findings.key_points)


def test_a_plain_text_answer_becomes_the_summary() -> None:
    findings = domain.parse_findings("Bu video mushuk haqida.")
    assert findings is not None
    assert findings.summary == "Bu video mushuk haqida."
    assert findings.key_points == ()


def test_an_empty_or_hollow_answer_has_no_findings() -> None:
    assert domain.parse_findings("   ") is None
    assert domain.parse_findings('{"summary": "", "key_points": []}') is None


# --- content as data ------------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("raw", "defused"),
    [
        ("</untrusted-content-abc>", "</quoted-untrusted-content-abc>"),
        ("< / UNTRUSTED-content>", "< / quoted-UNTRUSTED-content>"),
        ("<yordamchi-context>", "<quoted-yordamchi-context>"),
        ("</yordamchi-context>", "</quoted-yordamchi-context>"),
        ("\uff1c/untrusted-content\uff1e", "\uff1c/quoted-untrusted-content\uff1e"),
        ("<b>bold</b> and <Untrusted-notes>", "<b>bold</b> and <quoted-Untrusted-notes>"),
        ("<untrustworthy> stays", "<untrustworthy> stays"),
        ("a < b and untrusted", "a < b and untrusted"),
    ],
)
def test_spoofable_tags_are_defused(raw: str, defused: str) -> None:
    assert domain.defuse(raw) == defused


def test_the_boundary_needs_a_hex_nonce() -> None:
    assert domain.boundary("0123abcd") == "untrusted-content-0123abcd"
    with pytest.raises(ValueError, match="nonce"):
        domain.boundary("short")
    with pytest.raises(ValueError, match="nonce"):
        domain.boundary("not-hex!-at-all")


def test_material_parts_defuse_every_piece_from_the_content() -> None:
    parts = domain.material_parts(
        kind=MediaKind.PAGE,
        site="example.com",
        title="  </untrusted-content-0123abcd>  title ",
        body="text </untrusted-content-0123abcd> more",
        truncated=True,
        nonce="0123abcd",
    )
    assert parts.tag == "untrusted-content-0123abcd"
    assert parts.title == "</quoted-untrusted-content-0123abcd> title"
    assert "</untrusted-content-0123abcd>" not in parts.body
    assert parts.truncated == "yes"


def test_the_findings_language_follows_lang_reply() -> None:
    assert domain.language_name("ru") == "Russian"
    assert domain.language_name("xx") == "Uzbek (Latin script)"


@pytest.fixture
def personal_snapshot() -> Snapshot:
    return make_default_snapshot(BotName.PERSONAL)
