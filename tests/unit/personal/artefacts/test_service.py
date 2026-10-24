"""artefacts.service with `FakeWebTools` and a real SQLite file: each use case's happy path and every UC-32 F
failure, the media folder layout, the video fit loop, and delivery through the outbox."""

import stat
from pathlib import Path

import pytest

from tests.factories import (
    OWNER_ID,
    Fakes,
    add_setting_row,
    drain_outbox,
    list_replies,
    make_video_limits,
    send_outbox,
)
from yordamchi.core import strings as S
from yordamchi.core import timefmt
from yordamchi.core.errors import (
    CallTimedOut,
    ErrorCode,
    NotInstalled,
    VideoEncodeFailed,
    VideoTooLong,
    WebBotCheck,
    WebContentTooLarge,
    WebLoginRequired,
    WebNoMedia,
    WebNotAPage,
    WebPaywalled,
    WebUnreachable,
    YordamchiError,
)
from yordamchi.core.paths import Paths
from yordamchi.personal.artefacts import domain, service
from yordamchi.personal.artefacts.domain import MIB, ArtefactKind, Delivery
from yordamchi.personal.deps import PersonalDeps
from yordamchi.ports.web import Clip
from yordamchi.shared.settings import defaults

URL = "https://www.example.com/page"
VIDEO_URL = "https://www.youtube.com/watch?v=abc"
LIMITS = make_video_limits()


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _owner_text(error: YordamchiError) -> str:
    return S.ERRORS[error.code](**error.params)


# --- screenshot -----------------------------------------------------------------------------------------------------
async def test_screenshot_sends_the_viewport_as_a_photo_and_the_page_as_a_file(
    personal_deps: PersonalDeps, fakes: Fakes, paths: Paths
) -> None:
    reply = await service.take_screenshot(personal_deps, URL)
    assert reply.ok
    assert reply.kind is ArtefactKind.SCREENSHOT
    assert [a.delivery for a in reply.attachments] == [Delivery.PHOTO, Delivery.DOCUMENT]
    assert reply.text == S.ARTEFACT_SCREENSHOT(site="example.com")
    assert reply.title == "Fake page"
    folder = reply.attachments[0].path.parent
    assert folder.parent.parent == paths.media
    day = timefmt.local(fakes.clock.now(), "Asia/Tashkent")  # the ui.timezone default
    assert folder.parent.name == f"{day:%Y%m%d}"
    assert folder.name.startswith("screenshot-")
    assert _mode(folder) == 0o700
    assert all(_mode(a.path) == 0o600 for a in reply.attachments)
    method, called_url = fakes.web.calls[0]
    assert (method, called_url) == ("screenshot", URL)
    assert fakes.web.kwargs[0] == {"width": 1280, "height": 800, "timeout": 25.0}


async def test_screenshot_settings_apply_from_the_next_request(personal_deps: PersonalDeps, fakes: Fakes) -> None:
    await add_setting_row(personal_deps.db, "web.viewport_width_px", 390, at=fakes.clock.now())
    await service.take_screenshot(personal_deps, URL)
    assert fakes.web.kwargs[0]["width"] == 390


async def test_screenshot_as_file_sends_two_documents(personal_deps: PersonalDeps) -> None:
    reply = await service.take_screenshot(personal_deps, URL, as_file=True)
    assert [a.delivery for a in reply.attachments] == [Delivery.DOCUMENT, Delivery.DOCUMENT]


async def test_two_requests_never_share_a_folder(personal_deps: PersonalDeps) -> None:
    first = await service.take_screenshot(personal_deps, URL)
    second = await service.take_screenshot(personal_deps, URL)
    assert first.attachments[0].path.parent != second.attachments[0].path.parent


async def test_a_refused_link_never_reaches_the_browser(personal_deps: PersonalDeps, fakes: Fakes) -> None:
    reply = await service.take_screenshot(personal_deps, "file:///etc/passwd")
    assert reply.error_code is ErrorCode.WEB_URL_INVALID
    assert reply.text == S.ERR_WEB_URL_INVALID()
    assert fakes.web.calls == []


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (WebUnreachable(site="example.com"), S.ERR_WEB_UNREACHABLE(site="example.com")),
        (WebNotAPage(site="example.com"), S.ERR_WEB_NOT_A_PAGE(site="example.com")),
        (CallTimedOut(operation="screenshot"), S.ERR_CALL_TIMED_OUT(operation="screenshot")),
        (NotInstalled(binary="chromium"), S.ERR_NOT_INSTALLED(binary="chromium")),
    ],
)
async def test_a_screenshot_failure_is_explained_in_uzbek_and_its_folder_removed(
    personal_deps: PersonalDeps, fakes: Fakes, paths: Paths, error: Exception, expected: str
) -> None:
    fakes.web.failures["screenshot"] = error
    reply = await service.take_screenshot(personal_deps, URL)
    assert not reply.ok
    assert reply.attachments == ()
    assert reply.text == expected
    assert list(paths.media.rglob("screenshot-*")) == []


# --- fetch_file -----------------------------------------------------------------------------------------------------
async def test_an_image_comes_back_as_a_photo(personal_deps: PersonalDeps, fakes: Fakes) -> None:
    fakes.web.file_mime, fakes.web.file_name, fakes.web.file_bytes = "image/jpeg", "cat.jpg", b"\xff\xd8 jpeg"
    reply = await service.fetch_file(personal_deps, URL)
    assert reply.ok
    (attachment,) = reply.attachments
    assert attachment.delivery is Delivery.PHOTO
    assert attachment.filename == "cat.jpg"
    assert reply.text == S.ARTEFACT_FILE(site="example.com", name="cat.jpg", size="1 KB")
    assert fakes.web.kwargs[0] == {"max_bytes": 50 * MIB, "timeout": 90.0}


async def test_a_file_asked_as_file_is_a_document(personal_deps: PersonalDeps, fakes: Fakes) -> None:
    fakes.web.file_mime = "image/png"
    reply = await service.fetch_file(personal_deps, URL, as_file=True)
    assert reply.attachments[0].delivery is Delivery.DOCUMENT


@pytest.mark.parametrize(
    "error",
    [WebContentTooLarge(limit_mb=50), WebNoMedia(site="example.com"), WebLoginRequired(site="example.com")],
)
async def test_a_fetch_failure_is_explained(personal_deps: PersonalDeps, fakes: Fakes, error: YordamchiError) -> None:
    fakes.web.failures["fetch_file"] = error
    reply = await service.fetch_file(personal_deps, URL)
    assert reply.error_code is error.code
    assert reply.text == _owner_text(error)


# --- download_video -------------------------------------------------------------------------------------------------
async def test_a_small_video_is_sent_as_downloaded(personal_deps: PersonalDeps, fakes: Fakes) -> None:
    fakes.web.video_size, fakes.web.video_duration_s = 12 * MIB, 125.0
    reply = await service.download_video(personal_deps, VIDEO_URL)
    assert reply.ok
    (attachment,) = reply.attachments
    assert attachment.delivery is Delivery.VIDEO
    assert attachment.filename == "youtube.com-video.mp4"
    assert reply.text == S.ARTEFACT_VIDEO(site="youtube.com", duration="2:05", size="12.0 MB")
    assert [method for method, _ in fakes.web.calls] == ["download_video"]
    asked = fakes.web.kwargs[0]
    assert asked["format_selector"] == domain.format_selector(LIMITS)
    assert asked["max_bytes"] == 500 * MIB
    assert asked["max_duration_s"] == pytest.approx(domain.max_fit_duration_s(LIMITS))
    assert asked["clip"] is None


async def test_an_oversized_video_is_reencoded_and_the_original_deleted(
    personal_deps: PersonalDeps, fakes: Fakes
) -> None:
    fakes.web.video_size, fakes.web.video_duration_s = 120 * MIB, 600.0
    fakes.web.encode_sizes = [44 * MIB]
    reply = await service.download_video(personal_deps, VIDEO_URL)
    assert reply.ok
    plan = domain.plan_fit(120 * MIB, 600.0, LIMITS)
    encode = fakes.web.kwargs[1]
    assert encode == {"video_kbps": plan.video_kbps, "audio_kbps": 64, "max_height": plan.max_height, "timeout": 420.0}
    (attachment,) = reply.attachments
    assert attachment.path.name == f"video-{plan.video_kbps}k.mp4"
    assert not (attachment.path.parent / "video.mp4").exists()
    assert f"{plan.max_height}p" in reply.text


async def test_an_overshooting_encode_gets_a_second_pass(personal_deps: PersonalDeps, fakes: Fakes) -> None:
    fakes.web.video_size, fakes.web.video_duration_s = 120 * MIB, 600.0
    fakes.web.encode_sizes = [53 * MIB, 45 * MIB]
    reply = await service.download_video(personal_deps, VIDEO_URL)
    assert reply.ok
    assert [method for method, _ in fakes.web.calls] == ["download_video", "reencode_video", "reencode_video"]
    first, second = fakes.web.kwargs[1]["video_kbps"], fakes.web.kwargs[2]["video_kbps"]
    assert isinstance(first, int)
    assert isinstance(second, int)
    assert second < first


async def test_a_video_that_cannot_fit_offers_alternatives_and_cleans_up(
    personal_deps: PersonalDeps, fakes: Fakes, paths: Paths
) -> None:
    fakes.web.video_size, fakes.web.video_duration_s = 120 * MIB, 600.0
    fakes.web.encode_sizes = [60 * MIB, 55 * MIB]
    reply = await service.download_video(personal_deps, VIDEO_URL)
    assert reply.error_code is ErrorCode.VIDEO_TOO_LARGE
    assert reply.text.startswith(S.ERR_VIDEO_TOO_LARGE(limit_mb=49))
    assert S.ARTEFACT_VIDEO_OFFER(minutes=24) in reply.text
    assert list(paths.media.rglob("video-*")) == []


async def test_an_unknown_duration_cannot_be_reencoded(personal_deps: PersonalDeps, fakes: Fakes) -> None:
    fakes.web.video_size, fakes.web.video_duration_s = 80 * MIB, None
    reply = await service.download_video(personal_deps, VIDEO_URL)
    assert reply.error_code is ErrorCode.VIDEO_TOO_LARGE
    assert [method for method, _ in fakes.web.calls] == ["download_video"]


async def test_a_too_long_video_is_refused_with_its_own_page(personal_deps: PersonalDeps, fakes: Fakes) -> None:
    page = "https://www.youtube.com/watch?v=xyz"
    fakes.web.failures["download_video"] = VideoTooLong(minutes=95, link=page)
    reply = await service.download_video(personal_deps, "https://blog.uz/post")
    assert reply.error_code is ErrorCode.VIDEO_TOO_LONG
    assert reply.text.startswith(S.ERR_VIDEO_TOO_LONG(minutes=95))
    assert S.ARTEFACT_VIDEO_OFFER(minutes=24) in reply.text
    assert S.ARTEFACT_DIRECT_LINK(url=page) in reply.text


async def test_a_clip_is_passed_on_and_skips_the_length_check(personal_deps: PersonalDeps, fakes: Fakes) -> None:
    fakes.web.video_size = 20 * MIB
    reply = await service.download_video(personal_deps, VIDEO_URL, clip=Clip(start_s=60, end_s=300))
    assert reply.ok
    assert fakes.web.kwargs[0]["clip"] == Clip(start_s=60, end_s=300)
    assert fakes.web.kwargs[0]["max_duration_s"] is None
    assert "4:00" in reply.text


async def test_a_clip_longer_than_what_fits_is_refused_before_downloading(
    personal_deps: PersonalDeps, fakes: Fakes
) -> None:
    reply = await service.download_video(personal_deps, VIDEO_URL, clip=Clip(start_s=0, end_s=3600))
    assert reply.error_code is ErrorCode.VIDEO_CLIP_INVALID
    assert reply.text == S.ERR_VIDEO_CLIP_INVALID(max_minutes=24)
    assert fakes.web.calls == []


@pytest.mark.parametrize(
    ("error", "offer"),
    [
        (WebLoginRequired(site="instagram.com"), S.ARTEFACT_VIDEO_OFFER_NO_ACCESS()),
        (WebPaywalled(site="youtube.com"), S.ARTEFACT_VIDEO_OFFER_NO_ACCESS()),
        (WebContentTooLarge(limit_mb=500), S.ARTEFACT_VIDEO_OFFER(minutes=24)),
        (WebBotCheck(site="youtube.com"), None),
        (VideoEncodeFailed(), None),
    ],
)
async def test_video_failures_explain_and_offer_what_fits(
    personal_deps: PersonalDeps, fakes: Fakes, error: YordamchiError, offer: str | None
) -> None:
    method = "reencode_video" if isinstance(error, VideoEncodeFailed) else "download_video"
    fakes.web.video_size, fakes.web.video_duration_s = 120 * MIB, 600.0
    fakes.web.failures[method] = error
    reply = await service.download_video(personal_deps, VIDEO_URL)
    assert not reply.ok
    assert reply.error_code is error.code
    assert reply.text.startswith(_owner_text(error))
    if offer is None:
        assert "Buning o'rniga" not in reply.text
    else:
        assert offer in reply.text


async def test_video_limits_come_from_settings(personal_deps: PersonalDeps, fakes: Fakes) -> None:
    await add_setting_row(personal_deps.db, defaults.WEB_VIDEO_MAX_HEIGHT_PX.key, 480, at=fakes.clock.now())
    await service.download_video(personal_deps, VIDEO_URL)
    assert str(fakes.web.kwargs[0]["format_selector"]).startswith("bv*[height<=480]")


# --- delivery -------------------------------------------------------------------------------------------------------
async def test_enqueue_reply_sends_each_attachment_with_its_caption(personal_deps: PersonalDeps, fakes: Fakes) -> None:
    reply = await service.take_screenshot(personal_deps, URL)
    async with personal_deps.db.uow() as s:
        ids = await service.enqueue_reply(
            s, reply, chat_id=OWNER_ID, ref="turn-7", at=fakes.clock.now(), max_parts=3, reply_to=42
        )
    assert len(ids) == 2
    await drain_outbox(personal_deps, fakes.clock, seconds=5)
    photo, document = fakes.messenger.sent("send_photo"), fakes.messenger.sent("send_document")
    assert len(photo) == 1
    assert len(document) == 1
    assert photo[0].payload["caption"] == S.ARTEFACT_SCREENSHOT(site="example.com")
    assert photo[0].payload["reply_to"] == 42
    assert document[0].payload["filename"] == "example.com-full.png"
    assert document[0].payload["reply_to"] is None


async def test_enqueue_reply_is_idempotent_per_ref(personal_deps: PersonalDeps, fakes: Fakes) -> None:
    reply = await service.take_screenshot(personal_deps, URL)
    for _ in range(2):
        async with personal_deps.db.uow() as s:
            await service.enqueue_reply(s, reply, chat_id=OWNER_ID, ref="turn-8", at=fakes.clock.now(), max_parts=3)
    assert len(await list_replies(personal_deps.db, personal_deps.bot)) == 2


async def test_a_refusal_is_queued_as_text(personal_deps: PersonalDeps, fakes: Fakes) -> None:
    fakes.web.failures["download_video"] = WebLoginRequired(site="instagram.com")
    reply = await service.download_video(personal_deps, "https://instagram.com/p/1")
    async with personal_deps.db.uow() as s:
        await service.enqueue_reply(s, reply, chat_id=OWNER_ID, ref="turn-9", at=fakes.clock.now(), max_parts=3)
    await send_outbox(personal_deps)
    (sent,) = fakes.messenger.sent("send_text")
    assert sent.payload["text"] == reply.text


async def test_a_video_goes_out_as_a_video(personal_deps: PersonalDeps, fakes: Fakes) -> None:
    reply = await service.download_video(personal_deps, VIDEO_URL)
    async with personal_deps.db.uow() as s:
        await service.enqueue_reply(s, reply, chat_id=OWNER_ID, ref="turn-10", at=fakes.clock.now(), max_parts=3)
    await send_outbox(personal_deps)
    (sent,) = fakes.messenger.sent("send_video")
    assert sent.payload["mime"] == "video/mp4"
