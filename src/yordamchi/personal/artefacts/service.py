"""Artefacts for the owner (UC-32, SPEC §3.2.14): a screenshot, a file or image from a page, a video from a link.

The personal assistant calls these as tools (`yordamchi_media`); each returns an `ArtefactReply` and never raises
for an expected failure. Its `text` is always the Uzbek line to show (a caption, or what went wrong plus what can be
done instead, UC-32 F), so the tool layer has nothing to translate. Its `attachments` are queued after the turn with
`enqueue_reply(s, …)` inside the caller's unit of work.

Files go to `DATA/media/<yyyymmdd>/<kind>-<token>/` (0700 folders, 0600 files); the daily `health.purge_media`
removes day folders after `media.retention_days`. A failed request's folder is removed at once, and an oversized
original is deleted once its re-encode fits.

The personal process holds no executor, git or SSH handle (C7), and nothing fetched here is ever an instruction:
titles and files are data for the owner.
"""

import asyncio
import contextlib
import secrets
import shutil
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final

from yordamchi.core import strings as S
from yordamchi.core import timefmt
from yordamchi.core.errors import ErrorCode, VideoTooLarge, VideoTooLong, YordamchiError
from yordamchi.core.log import get_log
from yordamchi.core.paths import BotName
from yordamchi.core.text import BotText
from yordamchi.persistence.uow import Session
from yordamchi.personal.artefacts import domain
from yordamchi.personal.artefacts.domain import ArtefactKind, Delivery, FitAction, VideoLimits
from yordamchi.personal.deps import PersonalDeps
from yordamchi.ports.web import Clip, DownloadedVideo
from yordamchi.shared.outbox import service as outbox
from yordamchi.shared.outbox.domain import Kind, Op
from yordamchi.shared.settings import defaults
from yordamchi.shared.settings import service as settings
from yordamchi.shared.settings.domain import Snapshot

log = get_log(__name__)

_OPS: Final = {Delivery.PHOTO: Op.SEND_PHOTO, Delivery.DOCUMENT: Op.SEND_DOCUMENT, Delivery.VIDEO: Op.SEND_VIDEO}
_PNG: Final = "image/png"


@dataclass(frozen=True, slots=True)
class Attachment:
    path: Path
    delivery: Delivery
    mime: str
    filename: str
    caption: BotText | None = None


@dataclass(frozen=True, slots=True)
class ArtefactReply:
    kind: ArtefactKind
    text: BotText  # the caption on success; the reason and the alternatives on failure
    attachments: tuple[Attachment, ...] = ()
    error_code: ErrorCode | None = None
    title: str | None = None  # the page's or video's title: web content, data only

    @property
    def ok(self) -> bool:
        return self.error_code is None


# --- helpers ---------------------------------------------------------------------------------------------------------
def _make_private_dir(folder: Path) -> None:
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    for level in (folder.parent, folder):
        level.chmod(0o700)


def _discard(folder: Path) -> None:
    with contextlib.suppress(OSError):
        shutil.rmtree(folder)


async def _request_folder(deps: PersonalDeps, snap: Snapshot, kind: ArtefactKind) -> Path:
    day = timefmt.local(deps.clock.now(), snap[defaults.UI_TIMEZONE]).date()
    folder = deps.paths.media / domain.request_folder(day, kind, secrets.token_hex(6))
    await asyncio.to_thread(_make_private_dir, folder)
    return folder


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _ready(
    kind: ArtefactKind,
    text: BotText,
    attachments: tuple[Attachment, ...],
    *,
    started: float,
    title: str | None,
    size_bytes: int,
    encode_passes: int = 0,
) -> ArtefactReply:
    log.info(
        "artefact_ready",
        kind=kind.value,
        files=len(attachments),
        size_bytes=size_bytes,
        encode_passes=encode_passes,
        duration_ms=_ms(started),
    )
    return ArtefactReply(kind=kind, text=text, attachments=attachments, title=title)


async def _refused(
    kind: ArtefactKind, error: YordamchiError, *, started: float, folder: Path | None, text: BotText | None = None
) -> ArtefactReply:
    if folder is not None:
        await asyncio.to_thread(_discard, folder)
    log.warning("artefact_refused", kind=kind.value, error_code=error.code.value, duration_ms=_ms(started))
    message = text if text is not None else S.ERRORS[error.code](**error.params)
    return ArtefactReply(kind=kind, text=message, error_code=error.code)


# --- use cases ------------------------------------------------------------------------------------------------------
async def take_screenshot(deps: PersonalDeps, url: str, *, as_file: bool = False) -> ArtefactReply:
    """UC-32 "X saytining skrinshoti": the viewport as a photo, the full page as a file ("fayl qilib": both files)."""
    started, kind, folder = time.monotonic(), ArtefactKind.SCREENSHOT, None
    snap = await settings.snapshot(deps)
    try:
        link = domain.check_url(url)
        folder = await _request_folder(deps, snap, kind)
        shot = await deps.web.screenshot(
            link,
            dest_dir=folder,
            width=snap[defaults.WEB_VIEWPORT_WIDTH_PX],
            height=snap[defaults.WEB_VIEWPORT_HEIGHT_PX],
            timeout=snap[defaults.TIMEOUTS_SCREENSHOT_S],
        )
    except YordamchiError as error:
        return await _refused(kind, error, started=started, folder=folder)
    site = domain.site_of(link)
    first, second = domain.screenshot_deliveries(as_file=as_file)
    caption = S.ARTEFACT_SCREENSHOT(site=site)
    attachments = (
        Attachment(shot.viewport_png, first, _PNG, f"{site}.png", caption),
        Attachment(shot.full_page_png, second, _PNG, f"{site}-full.png", S.ARTEFACT_SCREENSHOT_FULL(site=site)),
    )
    return _ready(kind, caption, attachments, started=started, title=shot.title or None, size_bytes=0)


async def fetch_file(deps: PersonalDeps, url: str, *, as_file: bool = False) -> ArtefactReply:
    """UC-32 "bu sahifadagi rasm/faylni ol": the file at the link, or the page's main image, ≤ `web.file_max_mb`."""
    started, kind, folder = time.monotonic(), ArtefactKind.FILE, None
    snap = await settings.snapshot(deps)
    try:
        link = domain.check_url(url)
        folder = await _request_folder(deps, snap, kind)
        fetched = await deps.web.fetch_file(
            link,
            dest_dir=folder,
            max_bytes=snap[defaults.WEB_FILE_MAX_MB] * domain.MIB,
            timeout=snap[defaults.TIMEOUTS_FETCH_FILE_S],
        )
    except YordamchiError as error:
        return await _refused(kind, error, started=started, folder=folder)
    site = domain.site_of(link)
    caption = S.ARTEFACT_FILE(
        site=site, name=domain.file_label(fetched.path.name), size=domain.size_text(fetched.size_bytes)
    )
    delivery = domain.file_delivery(fetched.mime, fetched.size_bytes, as_file=as_file)
    attachment = Attachment(fetched.path, delivery, fetched.mime, fetched.path.name, caption)
    return _ready(kind, caption, (attachment,), started=started, title=None, size_bytes=fetched.size_bytes)


async def download_video(
    deps: PersonalDeps, url: str, *, clip: Clip | None = None, as_file: bool = False
) -> ArtefactReply:
    """UC-32 "videoni yubor": the video at the link, made to fit the send cap; else why not, and the alternatives."""
    started, kind, folder = time.monotonic(), ArtefactKind.VIDEO, None
    snap = await settings.snapshot(deps)
    limits = domain.video_limits(snap)
    video: DownloadedVideo | None = None
    try:
        link = domain.check_url(url)
        checked = domain.check_clip(clip, limits) if clip is not None else None
        folder = await _request_folder(deps, snap, kind)
        video = await deps.web.download_video(
            link,
            dest_dir=folder,
            max_bytes=limits.download_max_bytes,
            format_selector=domain.format_selector(limits),
            max_duration_s=None if checked is not None else domain.max_fit_duration_s(limits),
            clip=checked,
            timeout=snap[defaults.TIMEOUTS_VIDEO_DOWNLOAD_S],
        )
        fitted, height, passes = await _fit(deps, video, folder, limits, timeout=snap[defaults.TIMEOUTS_VIDEO_ENCODE_S])
    except YordamchiError as error:
        page = error.link if isinstance(error, VideoTooLong) else video.webpage_url if video is not None else None
        base = S.ERRORS[error.code](**error.params)
        text = domain.video_refusal(error.code, base, limits, url=url, link=page)
        return await _refused(kind, error, started=started, folder=folder, text=text)
    caption = domain.video_caption(
        site=domain.site_of(link),
        duration_s=video.duration_s,
        size_bytes=fitted.size_bytes,
        reencoded_height=height,
        limit_mb=limits.send_max_mb,
    )
    filename = f"{domain.site_of(link)}-video{fitted.path.suffix or '.mp4'}"
    attachment = Attachment(
        fitted.path, domain.video_delivery(fitted.mime, as_file=as_file), fitted.mime, filename, caption
    )
    return _ready(
        kind,
        caption,
        (attachment,),
        started=started,
        title=video.title or None,
        size_bytes=fitted.size_bytes,
        encode_passes=passes,
    )


async def _fit(
    deps: PersonalDeps, video: DownloadedVideo, folder: Path, limits: VideoLimits, *, timeout: float
) -> tuple[DownloadedVideo, int | None, int]:
    """The video as downloaded, or re-encoded (at most `domain.ENCODE_PASSES` passes, all from the original) until
    it fits. Returns the file to send, the height it was re-encoded to (None: sent as downloaded) and the passes."""
    plan = domain.plan_fit(video.size_bytes, video.duration_s, limits)
    current, height, passes = video, None, 0
    while plan.action is FitAction.REENCODE:
        passes += 1
        current = await deps.web.reencode_video(
            video.path,
            dest_dir=folder,
            video_kbps=plan.video_kbps,
            audio_kbps=plan.audio_kbps,
            max_height=plan.max_height,
            timeout=timeout,
        )
        height = plan.max_height
        plan = domain.plan_after_encode(plan, current.size_bytes, limits)
    if plan.action is FitAction.TOO_LARGE:
        raise VideoTooLarge(limit_mb=limits.send_max_mb)
    if current.path != video.path:
        await asyncio.to_thread(video.path.unlink, missing_ok=True)
    return current, height, passes


# --- delivery (inside the caller's unit of work) ---------------------------------------------------------------------
async def enqueue_reply(
    s: Session,
    reply: ArtefactReply,
    *,
    chat_id: int,
    ref: str,
    at: datetime,
    max_parts: int,
    reply_to: int | None = None,
) -> tuple[int, ...]:
    """Queue the reply in the outbox: each attachment with its caption (keyed `<ref>-<n>`), or the text alone when
    there is nothing to attach. Returns the new outbox row ids (() if this ref was queued before)."""
    if not reply.attachments:
        return await outbox.enqueue_text(
            s,
            bot=BotName.PERSONAL,
            chat_id=chat_id,
            text=reply.text,
            kind=Kind.RESULT,
            ref=ref,
            at=at,
            max_parts=max_parts,
            reply_to=reply_to,
        )
    ids: list[int] = []
    for index, attachment in enumerate(reply.attachments):
        ids.extend(
            await outbox.enqueue_file(
                s,
                bot=BotName.PERSONAL,
                chat_id=chat_id,
                op=_OPS[attachment.delivery],
                path=attachment.path,
                kind=Kind.FILE,
                ref=f"{ref}-{index}",
                at=at,
                max_parts=max_parts,
                caption=attachment.caption,
                filename=attachment.filename,
                mime=attachment.mime,
                reply_to=reply_to if index == 0 else None,
            )
        )
    return tuple(ids)
