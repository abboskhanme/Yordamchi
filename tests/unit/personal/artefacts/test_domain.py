"""artefacts.domain: links, folders, the video size fit, delivery and the Uzbek replies (UC-32, SPEC §3.2.14)."""

from datetime import date
from pathlib import PurePosixPath

import pytest

from tests.factories import make_video_limits
from yordamchi.core import strings as S
from yordamchi.core.errors import ErrorCode, VideoClipInvalid, WebUrlInvalid
from yordamchi.personal.artefacts import domain
from yordamchi.personal.artefacts.domain import MIB, ArtefactKind, Delivery, FitAction, FitPlan
from yordamchi.ports.web import Clip

LIMITS = make_video_limits()  # 49 MB send cap, 46 MB target, 720/480p, 64 kbps audio, 200 kbps video floor


# --- links and folders ----------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "url", ["https://example.com", " http://example.com/a?b=1 ", "https://user.github.io/x#y", "http://localhost:5173/"]
)
def test_http_links_are_accepted_trimmed(url: str) -> None:
    assert domain.check_url(url) == url.strip()


@pytest.mark.parametrize(
    "url",
    [
        "",
        "example.com",
        "file:///Users/me/.config/yordamchi/master.key",
        "javascript:alert(1)",
        "data:text/html,hi",
        "ftp://example.com/x",
        "https://",
        "https://exa mple.com",
        "https://example.com/\x00",
        "http://[::1",
        "https://example.com/" + "a" * 2100,
    ],
)
def test_other_links_are_refused(url: str) -> None:
    with pytest.raises(WebUrlInvalid):
        domain.check_url(url)


def test_site_is_the_host_without_www_or_port() -> None:
    assert domain.site_of("https://www.YouTube.com:443/watch?v=x") == "youtube.com"
    assert domain.site_of("not a url") == "sayt"


def test_each_request_gets_its_own_folder_under_its_day() -> None:
    folder = domain.request_folder(date(2026, 10, 24), ArtefactKind.VIDEO, "abc123")
    assert folder == PurePosixPath("20261024/video-abc123")


# --- the format selector and the fit ----------------------------------------------------------------------------------
def test_format_selector_follows_the_spec_with_fallbacks() -> None:
    assert domain.format_selector(LIMITS) == (
        "bv*[height<=720][filesize<45MiB]+ba/b[height<=720][filesize<48MiB]/bv*[height<=480]+ba/b[height<=480]/w"
    )


def test_format_selector_never_lets_the_low_height_exceed_the_high_one() -> None:
    selector = domain.format_selector(make_video_limits(max_height=360, low_height=480))
    assert "height<=480" not in selector
    assert selector.startswith("bv*[height<=360]")


def test_the_longest_video_that_fits_is_target_over_the_floor_bitrates() -> None:
    expected = 46 * MIB * 8 / ((200 + 64) * 1000)
    assert domain.max_fit_duration_s(LIMITS) == pytest.approx(expected)
    assert domain.fit_minutes(LIMITS) == 24


def test_a_file_under_the_send_cap_is_sent_as_is() -> None:
    assert domain.plan_fit(49 * MIB, 600.0, LIMITS) == domain.SEND


def test_an_oversized_video_is_reencoded_at_the_spec_bitrate() -> None:
    plan = domain.plan_fit(80 * MIB, 600.0, LIMITS)
    total = int(46 * MIB * 8 / 1000 / 600)  # floor(46 MB·8/duration)
    assert plan == FitPlan(FitAction.REENCODE, video_kbps=total - 64, audio_kbps=64, max_height=360, pass_no=1)


@pytest.mark.parametrize(
    ("video_kbps", "height"), [(3000, 720), (1500, 720), (1499, 480), (800, 480), (400, 360), (201, 240)]
)
def test_height_follows_the_bitrate(video_kbps: int, height: int) -> None:
    assert domain.height_for(video_kbps, 720) == height


def test_height_never_exceeds_the_setting() -> None:
    assert domain.height_for(5000, 480) == 480


def test_a_video_that_needs_less_than_the_floor_bitrate_is_too_large() -> None:
    too_long = domain.max_fit_duration_s(LIMITS) + 60
    assert domain.plan_fit(300 * MIB, too_long, LIMITS) == domain.TOO_LARGE


def test_an_oversized_video_without_a_duration_is_too_large() -> None:
    assert domain.plan_fit(60 * MIB, None, LIMITS) == domain.TOO_LARGE
    assert domain.plan_fit(60 * MIB, 0.0, LIMITS) == domain.TOO_LARGE


def test_an_overshooting_encode_gets_one_more_pass_at_a_scaled_bitrate() -> None:
    first = domain.plan_fit(80 * MIB, 600.0, LIMITS)
    second = domain.plan_after_encode(first, 52 * MIB, LIMITS)
    total = int((first.video_kbps + 64) * (46 / 52) * domain.OVERSHOOT_MARGIN)
    assert second == FitPlan(FitAction.REENCODE, video_kbps=total - 64, audio_kbps=64, max_height=360, pass_no=2)
    assert domain.plan_after_encode(second, 50 * MIB, LIMITS) == domain.TOO_LARGE  # ENCODE_PASSES = 2


def test_an_encode_that_fits_is_sent() -> None:
    first = domain.plan_fit(80 * MIB, 600.0, LIMITS)
    assert domain.plan_after_encode(first, 45 * MIB, LIMITS) == domain.SEND


def test_a_second_pass_below_the_floor_is_too_large() -> None:
    near_floor = FitPlan(FitAction.REENCODE, video_kbps=210, audio_kbps=64, max_height=240, pass_no=1)
    assert domain.plan_after_encode(near_floor, 90 * MIB, LIMITS) == domain.TOO_LARGE


def test_the_target_never_exceeds_the_send_cap() -> None:
    limits = make_video_limits(send_max_mb=20, target_mb=46)
    assert limits.target_bytes == 20 * MIB


def test_a_clip_inside_the_fit_is_accepted() -> None:
    clip = Clip(start_s=90, end_s=240)
    assert domain.check_clip(clip, LIMITS) is clip


@pytest.mark.parametrize("clip", [Clip(-1, 10), Clip(30, 30), Clip(40, 10), Clip(0, 60 * 60)])
def test_a_bad_or_too_long_clip_is_refused_with_the_limit(clip: Clip) -> None:
    with pytest.raises(VideoClipInvalid) as caught:
        domain.check_clip(clip, LIMITS)
    assert caught.value.params == {"max_minutes": 24}


# --- delivery -------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("mime", "size", "as_file", "expected"),
    [
        ("image/jpeg", 2 * MIB, False, Delivery.PHOTO),
        ("image/png", 2 * MIB, True, Delivery.DOCUMENT),
        ("image/png", 11 * MIB, False, Delivery.DOCUMENT),  # sendPhoto takes ≤ 10 MB
        ("image/gif", 1 * MIB, False, Delivery.DOCUMENT),
        ("application/pdf", 1 * MIB, False, Delivery.DOCUMENT),
    ],
)
def test_file_delivery_follows_the_type_and_the_request(
    mime: str, size: int, as_file: bool, expected: Delivery
) -> None:
    assert domain.file_delivery(mime, size, as_file=as_file) is expected


def test_video_delivery() -> None:
    assert domain.video_delivery("video/mp4", as_file=False) is Delivery.VIDEO
    assert domain.video_delivery("video/mp4", as_file=True) is Delivery.DOCUMENT
    assert domain.video_delivery("video/webm", as_file=False) is Delivery.DOCUMENT


def test_screenshot_is_a_photo_plus_a_full_page_file() -> None:
    assert domain.screenshot_deliveries(as_file=False) == (Delivery.PHOTO, Delivery.DOCUMENT)
    assert domain.screenshot_deliveries(as_file=True) == (Delivery.DOCUMENT, Delivery.DOCUMENT)


# --- reply text -----------------------------------------------------------------------------------------------------
def test_sizes_and_durations_read_naturally() -> None:
    assert domain.size_text(500) == "1 KB"
    assert domain.size_text(int(12.34 * MIB)) == "12.3 MB"
    assert domain.duration_text(245) == "4:05"
    assert domain.duration_text(3723.4) == "1:02:03"
    assert domain.duration_text(None) == "?"


def test_file_labels_are_one_short_line() -> None:
    assert domain.file_label("a\nb   c.pdf") == "a b c.pdf"
    assert len(domain.file_label("x" * 200)) == 80
    assert domain.file_label("  ") == "fayl"


def test_video_caption_says_when_it_was_reencoded() -> None:
    plain = domain.video_caption(
        site="vimeo.com", duration_s=65, size_bytes=8 * MIB, reencoded_height=None, limit_mb=49
    )
    assert plain == S.ARTEFACT_VIDEO(site="vimeo.com", duration="1:05", size="8.0 MB")
    shrunk = domain.video_caption(
        site="vimeo.com", duration_s=65, size_bytes=45 * MIB, reencoded_height=480, limit_mb=49
    )
    assert "480p" in shrunk
    assert "49 MB" in shrunk


def test_a_too_large_refusal_offers_a_clip_a_summary_and_the_link() -> None:
    base = S.ERR_VIDEO_TOO_LARGE(limit_mb=49)
    text = domain.video_refusal(ErrorCode.VIDEO_TOO_LARGE, base, LIMITS, url="https://x.uz/v", link=None)
    assert text.startswith(base)
    assert S.ARTEFACT_VIDEO_OFFER(minutes=24) in text
    assert "qisqacha mazmunini" in text


def test_a_login_refusal_offers_the_link_and_a_forward() -> None:
    base = S.ERR_WEB_LOGIN_REQUIRED(site="instagram.com")
    text = domain.video_refusal(ErrorCode.WEB_LOGIN_REQUIRED, base, LIMITS, url="https://instagram.com/p/1", link=None)
    assert S.ARTEFACT_VIDEO_OFFER_NO_ACCESS() in text


def test_the_embedded_videos_own_page_is_added_when_it_differs() -> None:
    base = S.ERR_VIDEO_TOO_LONG(minutes=95)
    page = "https://www.youtube.com/watch?v=abc"
    text = domain.video_refusal(ErrorCode.VIDEO_TOO_LONG, base, LIMITS, url="https://blog.uz/post", link=page)
    assert S.ARTEFACT_DIRECT_LINK(url=page) in text
    same = domain.video_refusal(ErrorCode.VIDEO_TOO_LONG, base, LIMITS, url=page, link=page)
    assert "sahifasi" not in same
    unsafe = domain.video_refusal(ErrorCode.VIDEO_TOO_LONG, base, LIMITS, url=page, link="javascript:alert(1)")
    assert "javascript" not in unsafe


def test_other_failures_are_just_the_reason() -> None:
    base = S.ERR_WEB_UNREACHABLE(site="x.uz")
    assert domain.video_refusal(ErrorCode.WEB_UNREACHABLE, base, LIMITS, url="https://x.uz", link=None) == base
