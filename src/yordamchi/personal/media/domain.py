"""Media analysis rules, pure (UC-31; SPEC §3.2.14, §3.2.11 step 3).

What the owner sends becomes a `MediaRequest` (`request_for_part`, `request_for_url`):
- a video message (or a video sent as a file) → `VIDEO`: read from Telegram (≤ `media.video_max_mb`, which Telegram
  caps at 20 MB for bots) and, when its duration is known, ≤ `media.video_max_min`;
- a YouTube link → `YOUTUBE`, normalised to its watch URL (the form both Gemini routes accept);
- a link to a video file (`.mp4`, `.webm`, …) → `VIDEO_LINK`: fetched under the same size cap, then read as a file;
- any other link → `PAGE`: its readable text, which the assistant reads itself.
Images are not read here: the assistant takes them as image blocks (T72); `image_parts` only says which parts to save
for it (T77).

Videos are read by the `MediaAnalyzer` of the first usable provider (`pick_video`: under `auto` Google, then
OpenRouter; a failure that the other may not share moves on, `voice.domain.falls_back`). The reader answers in JSON,
which `parse_findings` turns into `MediaFindings {summary, key_points, opinion_hook}`.

**Content is data** (UC-31: instructions inside the content are ignored). Everything that came from the video or page,
including the reader's findings about it, reaches the assistant only inside `material`: a block whose boundary carries
a per-request nonce the content cannot know, with every tag in it that looks like a boundary or a Yordamchi context
block defused (`defuse`). The framing text around it lives in `prompts/personal/media_material.md`.
"""

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Final
from urllib.parse import parse_qs, urlsplit

from yordamchi.core import strings as S
from yordamchi.core.errors import ErrorCode, MediaTooLarge, MediaTooLong, MediaVideoOff
from yordamchi.core.text import BotText, join
from yordamchi.ports.media import MediaAnalysis
from yordamchi.shared.burst.domain import BurstPart
from yordamchi.shared.cost.domain import Provider, Usage
from yordamchi.shared.inbox.domain import Attachment, UpdateKind
from yordamchi.shared.settings import defaults
from yordamchi.shared.settings.domain import Snapshot
from yordamchi.shared.voice import domain as voice

MIB: Final = 1024 * 1024
GOOGLE: Final = voice.GOOGLE
OPENROUTER: Final = voice.OPENROUTER
VIDEO_ORDER: Final = (GOOGLE, OPENROUTER)  # SPEC §3.2.11 step 3, video under `auto`: Google → OpenRouter → none
USAGE_SOURCE: Final = "video"
VIDEO_OFF: Final = "off"
# A provider attempt needs at least this much of the request's budget left to be worth starting.
MIN_ATTEMPT_S: Final = 5.0
# What the reader's findings may hold; more is cut (the assistant gets the material, not a transcript).
SUMMARY_MAX_CHARS: Final = 2000
KEY_POINTS_MAX: Final = 8
KEY_POINT_MAX_CHARS: Final = 400
OPINION_HOOK_MAX_CHARS: Final = 600
RAW_ANSWER_MAX_CHARS: Final = 6000
YOUTUBE_HOSTS: Final = frozenset(
    {"youtube.com", "m.youtube.com", "music.youtube.com", "youtube-nocookie.com", "youtu.be"}
)
VIDEO_SUFFIXES: Final = frozenset({".mp4", ".m4v", ".mov", ".webm", ".mpeg", ".mpg", ".avi", ".3gp"})
_YOUTUBE_ID: Final = re.compile(r"[A-Za-z0-9_-]{11}")
_URL: Final = re.compile(r"https?://\S+")
_URL_TRAILING: Final = ".,;:!?)]}>»\"'"
# A tag that could close the material block or open a context block of Yordamchi's: `<` or a look-alike (U+FF1C
# fullwidth, U+FE64 small), an optional slash, then a name starting with `untrusted` or `yordamchi`, in any case.
_SPOOFABLE_TAG: Final = re.compile(r"([<\uff1c\ufe64]\s*/?\s*)((?:untrusted|yordamchi)[\w-]*)", re.IGNORECASE)
LANGUAGE_NAMES: Final[Mapping[str, str]] = {"uz": "Uzbek (Latin script)", "ru": "Russian", "en": "English"}


class MediaKind(StrEnum):
    VIDEO = "video"  # a video the owner sent in the chat
    YOUTUBE = "youtube"
    VIDEO_LINK = "video_link"  # a link to a video file
    PAGE = "page"


VIDEO_KINDS: Final = frozenset({MediaKind.VIDEO, MediaKind.YOUTUBE, MediaKind.VIDEO_LINK})


# --- requests --------------------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class MediaRequest:
    """One thing to look at. `ref` keys the "Ko'ryapman" ack in the outbox (the update id: a retry acks once);
    `question` is what the owner asked about it (his caption or text), trusted; `announce` sends the ack."""

    kind: MediaKind
    chat_id: int
    ref: str
    url: str | None = None
    attachment: Attachment | None = None
    reply_to: int | None = None
    question: str = field(default="", repr=False)
    announce: bool = True


def first_url(text: str | None) -> str | None:
    """The first http(s) link in `text`, without the punctuation that usually follows a link in prose."""
    found = _URL.search(text or "")
    return found.group(0).rstrip(_URL_TRAILING) if found else None


def site_of(url: str) -> str:
    try:
        host = urlsplit(url.strip()).hostname or ""
    except ValueError:
        host = ""
    return host.removeprefix("www.") or "sayt"


def youtube_watch_url(url: str) -> str | None:
    """`https://www.youtube.com/watch?v=<id>` for any YouTube video link (watch, youtu.be, shorts, live, embed);
    None when `url` is not one."""
    try:
        parts = urlsplit(url.strip())
        host = (parts.hostname or "").removeprefix("www.")
    except ValueError:
        return None
    if host not in YOUTUBE_HOSTS:
        return None
    segments = [segment for segment in parts.path.split("/") if segment]
    candidate: str | None = None
    if host == "youtu.be":
        candidate = segments[0] if segments else None
    elif segments[:1] == ["watch"]:
        values = parse_qs(parts.query).get("v", [])
        candidate = values[0] if values else None
    elif len(segments) >= 2 and segments[0] in {"shorts", "live", "embed", "v"}:
        candidate = segments[1]
    if candidate is None or not _YOUTUBE_ID.fullmatch(candidate):
        return None
    return f"https://www.youtube.com/watch?v={candidate}"


def classify_url(url: str) -> MediaKind:
    if youtube_watch_url(url) is not None:
        return MediaKind.YOUTUBE
    try:
        suffix = PurePosixPath(urlsplit(url.strip()).path).suffix.lower()
    except ValueError:
        suffix = ""
    return MediaKind.VIDEO_LINK if suffix in VIDEO_SUFFIXES else MediaKind.PAGE


def request_for_url(
    url: str, *, chat_id: int, ref: str, question: str = "", reply_to: int | None = None, announce: bool = True
) -> MediaRequest:
    """A link to look at (also the `analyze_video(url)` tool's entry). YouTube links are normalised."""
    kind = classify_url(url)
    link = youtube_watch_url(url) if kind is MediaKind.YOUTUBE else url.strip()
    return MediaRequest(
        kind=kind, chat_id=chat_id, ref=ref, url=link, reply_to=reply_to, question=question, announce=announce
    )


def is_video_attachment(kind: UpdateKind, attachment: Attachment | None) -> bool:
    if attachment is None:
        return False
    if kind is UpdateKind.VIDEO:
        return True
    return kind is UpdateKind.DOCUMENT and (attachment.mime or "").lower().startswith("video/")


def request_for_part(part: BurstPart, *, chat_id: int) -> MediaRequest | None:
    """The media request one burst part carries: a video, or the first link of a link-only message; else None (a
    photo goes to the assistant as an image, a text is a question)."""
    attachment = part.content.attachment
    question = (part.text or "").strip()
    if is_video_attachment(part.kind, attachment):
        return MediaRequest(
            kind=MediaKind.VIDEO,
            chat_id=chat_id,
            ref=str(part.update_id),
            attachment=attachment,
            reply_to=part.message_id,
            question=question,
        )
    if part.kind is UpdateKind.LINK and (url := first_url(part.text)) is not None:
        return request_for_url(url, chat_id=chat_id, ref=str(part.update_id), reply_to=part.message_id)
    return None


# --- images (T77: the assistant reads them as image blocks) ---------------------------------------------------------
MAX_IMAGES: Final = 5  # SPEC §5.1: one input carries at most five images
IMAGE_MAX_BYTES: Final = 20 * MIB  # Telegram gives bots files up to 20 MB
# The image types Claude reads; a photo always arrives as JPEG.
IMAGE_SUFFIXES: Final[Mapping[str, str]] = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}


def image_suffix(kind: UpdateKind, mime: str | None) -> str | None:
    """The file suffix of a part the assistant sees as an image (a photo, or an image sent as a file), else None."""
    if kind is UpdateKind.PHOTO:
        return IMAGE_SUFFIXES["image/jpeg"]
    if kind is UpdateKind.DOCUMENT:
        return IMAGE_SUFFIXES.get((mime or "").lower())
    return None


def image_parts(parts: Sequence[BurstPart]) -> tuple[tuple[BurstPart, str], ...]:
    """The parts to save as images, with their suffix, in order; at most `MAX_IMAGES`."""
    found = [
        (part, suffix)
        for part in parts
        if part.content.attachment is not None
        and (suffix := image_suffix(part.kind, part.content.attachment.mime)) is not None
    ]
    return tuple(found[:MAX_IMAGES])


def words_beside(parts: Sequence[BurstPart], media_update_ids: Iterable[int]) -> bool:
    """Whether the burst says anything besides the media it carries: a part that is no media request and no image
    (a text, a voice note, a forwarded message) with words in it. Without such words, a media reading that failed
    is the whole answer (UC-31 F)."""
    taken = set(media_update_ids)
    return any(
        (part.text or "").strip()
        for part in parts
        if part.update_id not in taken
        and not (
            part.content.attachment is not None and image_suffix(part.kind, part.content.attachment.mime) is not None
        )
    )


# --- limits ---------------------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class MediaLimits:
    video_max_bytes: int
    video_max_s: int
    page_max_chars: int
    page_max_bytes: int
    budget_s: float

    @property
    def video_max_mb(self) -> int:
        return self.video_max_bytes // MIB


def media_limits(snap: Snapshot) -> MediaLimits:
    return MediaLimits(
        video_max_bytes=snap[defaults.MEDIA_VIDEO_MAX_MB] * MIB,
        video_max_s=snap[defaults.MEDIA_VIDEO_MAX_MIN] * 60,
        page_max_chars=snap[defaults.MEDIA_PAGE_MAX_CHARS],
        page_max_bytes=snap[defaults.MEDIA_PAGE_MAX_MB] * MIB,
        budget_s=snap[defaults.TIMEOUTS_MEDIA_S],
    )


def check_video(attachment: Attachment, limits: MediaLimits) -> None:
    """Refuse before downloading: over the size cap (UC-31 F) or longer than `media.video_max_min`. Unknown sizes and
    durations pass; the download itself is capped."""
    if attachment.size_bytes is not None and attachment.size_bytes > limits.video_max_bytes:
        raise MediaTooLarge(limit_mb=limits.video_max_mb)
    if attachment.duration_s is not None and attachment.duration_s > limits.video_max_s:
        raise MediaTooLong(max_minutes=limits.video_max_s // 60)


def video_file_name(attachment: Attachment) -> str:
    """The cached file's name: only an extension from the declared type, never the sender's file name."""
    mime = (attachment.mime or "").lower()
    extension = {"video/quicktime": ".mov", "video/webm": ".webm", "video/mpeg": ".mpeg"}.get(mime, ".mp4")
    return f"video{extension}"


# --- providers ------------------------------------------------------------------------------------------------------
def pick_video(preference: str, available: Iterable[str]) -> tuple[str, ...]:
    """The readers to try, in order. `speech.video_provider`: `off` refuses (`MediaVideoOff`); a named provider goes
    first with the other as its fallback; `auto` is Google, then OpenRouter. Only providers in `available` (a key in
    the vault, an adapter, and the cost allowance) are listed."""
    if preference == VIDEO_OFF:
        raise MediaVideoOff
    usable = set(available)
    first = (preference,) if preference in VIDEO_ORDER else ()
    order = (*first, *(provider for provider in VIDEO_ORDER if provider not in first))
    return tuple(provider for provider in order if provider in usable)


def video_model(provider: str, snap: Snapshot) -> str:
    return snap[defaults.VIDEO_GOOGLE_MODEL] if provider == GOOGLE else snap[defaults.VIDEO_OPENROUTER_MODEL]


def falls_back(code: ErrorCode) -> bool:
    """A failure the next provider may not share (key, quota, rate limit, outage) moves on; a timeout or a content
    problem (private video, too large) does not."""
    return voice.falls_back(code)


@dataclass(frozen=True, slots=True)
class Attempt:
    provider: str
    code: ErrorCode


PROVIDER_CODES: Final = frozenset({*voice.FALLBACK_CODES, ErrorCode.CALL_TIMED_OUT})


def failure_text(attempts: Sequence[Attempt], params: Mapping[str, str | int]) -> BotText:
    """The owner's message when no reader answered: per provider what went wrong, or, for a content problem (a
    private video, a file too large), that problem's own message. `params` are the last error's."""
    if not attempts:
        msg = "a failure needs at least one attempt"
        raise ValueError(msg)
    last = attempts[-1]
    if last.code not in PROVIDER_CODES:
        return S.ERRORS[last.code](**params)
    reasons = (
        S.STT_REASON_PROVIDER(provider=voice.provider_label(attempt.provider), problem=voice.problem_text(attempt.code))
        for attempt in attempts
    )
    return S.MEDIA_VIDEO_FAILED(reason=join(reasons, "; "))


def usage_of(analysis: MediaAnalysis) -> Usage:
    """The `usage_records` row of one answered reading. Paid when the provider reported a cost above 0; Google
    reports none, so its calls record 0 until the caps (T51) price them."""
    cost = max(0, analysis.cost_usd_micros)
    return Usage(
        source=USAGE_SOURCE,
        provider=Provider(analysis.provider),
        model=analysis.model,
        is_paid_api=cost > 0,
        cost_usd_micros=cost,
        duration_ms=max(0, analysis.latency_ms),
        units=dict(analysis.units),
    )


# --- findings -------------------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class MediaFindings:
    """What the reader found. Model output about untrusted content: data for the assistant, never instructions.

    `opinion_hook` is the claim or stance in the content that deserves the assistant's own evidence-based view (the
    persona takes a position, UC-30); empty when there is none."""

    summary: str = field(repr=False)
    key_points: tuple[str, ...] = field(default=(), repr=False)
    opinion_hook: str = field(default="", repr=False)


def _clip(value: object, limit: int) -> str:
    return " ".join(value.split())[:limit] if isinstance(value, str) else ""


def _json_object(text: str) -> object:
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(text[start : end + 1])
    except ValueError:
        return None


def parse_findings(text: str) -> MediaFindings | None:
    """The reader's JSON answer (`{"summary", "key_points", "opinion_hook"}`, code fences allowed) as findings; a
    non-JSON answer becomes the summary as it is. None when nothing usable came back."""
    raw = text.strip()
    if not raw:
        return None
    data = _json_object(raw)
    if not isinstance(data, dict):
        return MediaFindings(summary=raw[:RAW_ANSWER_MAX_CHARS])
    points = data.get("key_points")
    key_points = tuple(
        point
        for point in (_clip(item, KEY_POINT_MAX_CHARS) for item in (points if isinstance(points, list) else []))
        if point
    )[:KEY_POINTS_MAX]
    summary = _clip(data.get("summary"), SUMMARY_MAX_CHARS)
    if not summary and not key_points:
        return None
    return MediaFindings(
        summary=summary, key_points=key_points, opinion_hook=_clip(data.get("opinion_hook"), OPINION_HOOK_MAX_CHARS)
    )


def findings_json(findings: MediaFindings) -> str:
    """The findings as the material block carries them."""
    payload = {
        "summary": findings.summary,
        "key_points": list(findings.key_points),
        "opinion_hook": findings.opinion_hook,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


# --- content as data ------------------------------------------------------------------------------------------------
def defuse(text: str) -> str:
    """`text` with every tag that could pose as the material boundary or as a Yordamchi context block renamed
    (`</untrusted-content…>` → `</quoted-untrusted-content…>`), in any case and with look-alike brackets, so the content
    can neither end its block early nor open one that claims Yordamchi's authority (`adapters/claude/guard.py` does
    the same for its context block)."""
    return _SPOOFABLE_TAG.sub(lambda match: f"{match.group(1)}quoted-{match.group(2)}", text)


def boundary(nonce: str) -> str:
    """The material block's tag name. `nonce` is fresh per request (the service draws it), so content cannot guess
    the closing tag."""
    if not re.fullmatch(r"[0-9a-f]{8,64}", nonce):
        msg = "the nonce is lower-case hex, 8-64 characters"
        raise ValueError(msg)
    return f"untrusted-content-{nonce}"


@dataclass(frozen=True, slots=True)
class MaterialParts:
    """The slots of `prompts/personal/media_material.md`, each already defused."""

    tag: str
    kind: str
    site: str
    title: str
    body: str = field(repr=False)
    truncated: str


def material_parts(*, kind: MediaKind, site: str, title: str, body: str, truncated: bool, nonce: str) -> MaterialParts:
    return MaterialParts(
        tag=boundary(nonce),
        kind=kind.value,
        site=defuse(site),
        title=defuse(" ".join(title.split())) or "-",
        body=defuse(body),
        truncated="yes" if truncated else "no",
    )


def language_name(code: str) -> str:
    return LANGUAGE_NAMES.get(code, LANGUAGE_NAMES["uz"])


# --- the outcome ------------------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class MediaBrief:
    """What `personal/qa` gets for one media request (UC-31): the material to answer from, as data.

    - `material`: the delimited, defused block to put into the assistant's prompt as it is. It holds the reader's
      findings (video, YouTube) or the page text (page); the assistant writes what it is, the key points and its own
      opinion from it, in the owner's language.
    - `findings`: the same findings, structured, for a video or YouTube link; None for a page (the assistant reads
      the text itself). A fallback reply can be built from them without a model.
    """

    kind: MediaKind
    site: str
    material: str = field(repr=False)
    findings: MediaFindings | None = None
    title: str = field(default="", repr=False)
    provider: str | None = None
    model: str | None = None
    truncated: bool = False

    @property
    def ok(self) -> bool:
        return True


@dataclass(frozen=True, slots=True)
class MediaFailure:
    """Nothing to answer from. `text` is the owner's message (Uzbek, from the catalog): send it as the reply."""

    kind: MediaKind
    code: ErrorCode
    text: BotText

    @property
    def ok(self) -> bool:
        return False


type MediaOutcome = MediaBrief | MediaFailure
