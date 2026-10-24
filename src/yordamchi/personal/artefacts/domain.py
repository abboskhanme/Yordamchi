"""Artefact rules (UC-32, SPEC §3.2.14): which links are opened, where files go, how a video is made to fit the send
cap, how each file is delivered, and the Uzbek reply. Pure: every input is an argument.

**Fitting a video** (the numbers are settings, `limits` here):
1. The download format: `format_selector` prefers ≤ `max_height` with a known size under the target, then a single
   file under the send cap, then the low resolution, then whatever is smallest (SPEC: `bv*[height<=720]
   [filesize<45M]+ba/b[filesize<48M]/bv*[height<=480]+ba`, with two fallbacks so a site without sizes still works).
2. Before downloading: a video longer than `max_fit_duration_s` cannot fit even at `min_video_kbps`, so it is
   refused from its metadata (no wasted download).
3. After downloading: over the send cap → re-encode at `floor(target·8/duration) - audio` kbps, scaled down to a
   height that bitrate can carry. An overshoot gets one more pass at a proportionally lower bitrate. Below
   `min_video_kbps` → refused with alternatives: a shorter clip (≤ `fit_minutes`), a summary, the link.
"""

import math
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Final
from urllib.parse import urlsplit

from yordamchi.core import strings as S
from yordamchi.core.errors import ErrorCode, VideoClipInvalid, WebUrlInvalid
from yordamchi.core.text import BotText, join
from yordamchi.ports.web import Clip
from yordamchi.shared.settings import defaults
from yordamchi.shared.settings.domain import Snapshot

MIB: Final = 1024 * 1024
URL_MAX_CHARS: Final = 2048
URL_SCHEMES: Final = frozenset({"http", "https"})
# Telegram Bot API: sendPhoto takes at most 10 MB; these types show inline as a photo.
PHOTO_MAX_BYTES: Final = 10 * 1000 * 1000
PHOTO_MIMES: Final = frozenset({"image/jpeg", "image/png", "image/webp"})
# Telegram plays an MP4 inline (sendVideo); anything else goes as a document.
INLINE_VIDEO_MIMES: Final = frozenset({"video/mp4"})
ENCODE_PASSES: Final = 2
OVERSHOOT_MARGIN: Final = 0.95
# The lowest video bitrate (kbps) each height still looks acceptable at, highest first; 240p takes anything.
HEIGHT_FLOORS: Final = ((1500, 720), (800, 480), (400, 360), (0, 240))
# The fit failures whose reply offers a clip, a summary or the link (UC-32 F).
TOO_LARGE_CODES: Final = frozenset(
    {ErrorCode.VIDEO_TOO_LONG, ErrorCode.VIDEO_TOO_LARGE, ErrorCode.WEB_CONTENT_TOO_LARGE}
)
NO_ACCESS_CODES: Final = frozenset({ErrorCode.WEB_LOGIN_REQUIRED, ErrorCode.WEB_PAYWALLED, ErrorCode.WEB_GEO_BLOCKED})


class ArtefactKind(StrEnum):
    SCREENSHOT = "screenshot"
    FILE = "file"
    VIDEO = "video"


class Delivery(StrEnum):
    """How one file goes to the owner (the outbox op)."""

    PHOTO = "photo"
    DOCUMENT = "document"
    VIDEO = "video"


# --- links and folders ----------------------------------------------------------------------------------------------
def check_url(url: str) -> str:
    """The link, trimmed, if it is an absolute http(s) URL with a host; else `WebUrlInvalid`. `file:`, `data:`,
    `javascript:` and friends never reach a browser or a downloader."""
    candidate = url.strip()
    if not candidate or len(candidate) > URL_MAX_CHARS or any(char.isspace() or ord(char) < 32 for char in candidate):
        raise WebUrlInvalid
    try:
        parts = urlsplit(candidate)
        host = parts.hostname
    except ValueError:
        raise WebUrlInvalid from None
    if parts.scheme.lower() not in URL_SCHEMES or not host:
        raise WebUrlInvalid
    return candidate


def _is_web_link(url: str) -> bool:
    try:
        check_url(url)
    except WebUrlInvalid:
        return False
    return True


def site_of(url: str) -> str:
    """The host the owner recognises ("youtube.com"): lower case, without `www.` and without the port."""
    try:
        host = urlsplit(url.strip()).hostname or ""
    except ValueError:
        host = ""
    return host.removeprefix("www.") or "sayt"


def request_folder(day: date, kind: ArtefactKind, token: str) -> PurePosixPath:
    """`<yyyymmdd>/<kind>-<token>` under `DATA/media`: the day folder is what the daily purge removes after
    `media.retention_days`; one folder per request keeps two requests' files apart."""
    return PurePosixPath(f"{day:%Y%m%d}") / f"{kind.value}-{token}"


# --- video fitting --------------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class VideoLimits:
    send_max_mb: int
    target_mb: int
    download_max_mb: int
    max_height: int
    low_height: int
    audio_kbps: int
    min_video_kbps: int

    @property
    def send_max_bytes(self) -> int:
        return self.send_max_mb * MIB

    @property
    def target_bytes(self) -> int:
        return min(self.target_mb, self.send_max_mb) * MIB

    @property
    def download_max_bytes(self) -> int:
        return self.download_max_mb * MIB


def video_limits(snap: Snapshot) -> VideoLimits:
    """This turn's `web.video_*` settings as one value."""
    return VideoLimits(
        send_max_mb=snap[defaults.WEB_VIDEO_SEND_MAX_MB],
        target_mb=snap[defaults.WEB_VIDEO_TARGET_MB],
        download_max_mb=snap[defaults.WEB_VIDEO_DOWNLOAD_MAX_MB],
        max_height=snap[defaults.WEB_VIDEO_MAX_HEIGHT_PX],
        low_height=snap[defaults.WEB_VIDEO_LOW_HEIGHT_PX],
        audio_kbps=snap[defaults.WEB_VIDEO_AUDIO_KBPS],
        min_video_kbps=snap[defaults.WEB_VIDEO_MIN_KBPS],
    )


class FitAction(StrEnum):
    SEND = "send"
    REENCODE = "reencode"
    TOO_LARGE = "too_large"


@dataclass(frozen=True, slots=True)
class FitPlan:
    action: FitAction
    video_kbps: int = 0
    audio_kbps: int = 0
    max_height: int = 0
    pass_no: int = 0


SEND: Final = FitPlan(FitAction.SEND)
TOO_LARGE: Final = FitPlan(FitAction.TOO_LARGE)


def format_selector(limits: VideoLimits) -> str:
    """The yt-dlp format string (SPEC §3.2.14), sizes 1 MB under the target (video part) and the send cap (single
    file). The last two alternatives only guarantee *some* format; the re-encode makes it fit."""
    high, low = limits.max_height, min(limits.low_height, limits.max_height)
    video_mb = max(1, min(limits.target_mb, limits.send_max_mb) - 1)
    single_mb = max(1, limits.send_max_mb - 1)
    return "/".join(
        (
            f"bv*[height<={high}][filesize<{video_mb}MiB]+ba",
            f"b[height<={high}][filesize<{single_mb}MiB]",
            f"bv*[height<={low}]+ba",
            f"b[height<={low}]",
            "w",
        )
    )


def max_fit_duration_s(limits: VideoLimits) -> float:
    """The longest video that still fits the target at the lowest allowed bitrate."""
    return limits.target_bytes * 8 / ((limits.min_video_kbps + limits.audio_kbps) * 1000)


def fit_minutes(limits: VideoLimits) -> int:
    """`max_fit_duration_s` in whole minutes: the clip length the refusal offers."""
    return max(1, math.floor(max_fit_duration_s(limits) / 60))


def check_clip(clip: Clip, limits: VideoLimits) -> Clip:
    if clip.start_s < 0 or clip.end_s <= clip.start_s or clip.length_s > max_fit_duration_s(limits):
        raise VideoClipInvalid(max_minutes=fit_minutes(limits))
    return clip


def height_for(video_kbps: int, max_height: int) -> int:
    """The tallest height `video_kbps` carries (`HEIGHT_FLOORS`), never above `max_height`."""
    height = next(height for floor, height in HEIGHT_FLOORS if video_kbps >= floor)
    return min(height, max_height)


def _reencode(total_kbps: int, limits: VideoLimits, pass_no: int) -> FitPlan:
    video_kbps = total_kbps - limits.audio_kbps
    if video_kbps < limits.min_video_kbps:
        return TOO_LARGE
    return FitPlan(
        FitAction.REENCODE,
        video_kbps=video_kbps,
        audio_kbps=limits.audio_kbps,
        max_height=height_for(video_kbps, limits.max_height),
        pass_no=pass_no,
    )


def plan_fit(size_bytes: int, duration_s: float | None, limits: VideoLimits) -> FitPlan:
    """After the download: send it, re-encode it (`floor(target·8/duration) - audio` kbps), or give up."""
    if size_bytes <= limits.send_max_bytes:
        return SEND
    if duration_s is None or duration_s <= 0:
        return TOO_LARGE  # no duration → no bitrate to aim at
    return _reencode(math.floor(limits.target_bytes * 8 / 1000 / duration_s), limits, pass_no=1)


def plan_after_encode(previous: FitPlan, size_bytes: int, limits: VideoLimits) -> FitPlan:
    """After a re-encode pass: done, one more pass at a bitrate scaled by how far it overshot, or give up."""
    if size_bytes <= limits.send_max_bytes:
        return SEND
    if previous.action is not FitAction.REENCODE or previous.pass_no >= ENCODE_PASSES or size_bytes <= 0:
        return TOO_LARGE
    total = previous.video_kbps + previous.audio_kbps
    scaled = math.floor(total * limits.target_bytes / size_bytes * OVERSHOOT_MARGIN)
    return _reencode(scaled, limits, pass_no=previous.pass_no + 1)


# --- delivery -------------------------------------------------------------------------------------------------------
def file_delivery(mime: str, size_bytes: int, *, as_file: bool) -> Delivery:
    """An image Telegram shows inline goes as a photo unless the owner asked for the file; everything else is a
    document."""
    if not as_file and mime in PHOTO_MIMES and size_bytes <= PHOTO_MAX_BYTES:
        return Delivery.PHOTO
    return Delivery.DOCUMENT


def video_delivery(mime: str, *, as_file: bool) -> Delivery:
    return Delivery.VIDEO if not as_file and mime in INLINE_VIDEO_MIMES else Delivery.DOCUMENT


def screenshot_deliveries(*, as_file: bool) -> tuple[Delivery, Delivery]:
    """The viewport as a photo, the full page always as a file (SPEC §3.2.14); "fayl qilib" makes both files."""
    return (Delivery.DOCUMENT if as_file else Delivery.PHOTO, Delivery.DOCUMENT)


# --- reply text -----------------------------------------------------------------------------------------------------
def size_text(size_bytes: int) -> str:
    if size_bytes < MIB:
        return f"{max(1, math.ceil(size_bytes / 1024))} KB"
    return f"{size_bytes / MIB:.1f} MB"


def duration_text(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return "?"
    total = round(seconds)
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def file_label(name: str, limit: int = 80) -> str:
    """A file name fit for a caption: one line, at most `limit` characters."""
    single = " ".join(name.split()) or "fayl"
    return single if len(single) <= limit else single[: limit - 1] + "…"


def video_caption(
    *, site: str, duration_s: float | None, size_bytes: int, reencoded_height: int | None, limit_mb: int
) -> BotText:
    if reencoded_height is None:
        return S.ARTEFACT_VIDEO(site=site, duration=duration_text(duration_s), size=size_text(size_bytes))
    return S.ARTEFACT_VIDEO_REENCODED(
        site=site,
        duration=duration_text(duration_s),
        size=size_text(size_bytes),
        height=reencoded_height,
        limit_mb=limit_mb,
    )


def video_refusal(code: ErrorCode, base: BotText, limits: VideoLimits, *, url: str, link: str | None) -> BotText:
    """A video failure's reply: the reason, then what can be done instead (UC-32 F), then the video's own page when
    the link was a page that embeds it."""
    parts = [base]
    if code in TOO_LARGE_CODES:
        parts.append(S.ARTEFACT_VIDEO_OFFER(minutes=fit_minutes(limits)))
    elif code in NO_ACCESS_CODES:
        parts.append(S.ARTEFACT_VIDEO_OFFER_NO_ACCESS())
    if link and link.strip() != url.strip() and _is_web_link(link):
        parts.append(S.ARTEFACT_DIRECT_LINK(url=link.strip()))
    return join(parts)
