"""Media analysis for the personal assistant (UC-31; SPEC §3.2.14): a video, a YouTube link or a web page becomes a
`MediaBrief` the `qa` assistant answers from, or a `MediaFailure` whose text the owner gets.

    request = domain.request_for_part(part, chat_id=chat_id)      # or domain.request_for_url(url, …) for the tool
    outcome = await media.analyze_media(deps, request)
    if outcome.ok: put outcome.material into the qa prompt        # data; qa writes summary, key points, opinion
    else:          reply with outcome.text

1. "Ko'ryapman" is queued at once through the outbox (`Kind.ACK`, keyed by the request's ref; NFR-SPD ≤ 2 s).
2. Videos: the provider order comes first (`speech.video_provider`, the speech keys in the vault, the adapters in
   this bot, and the cost allowance), so a request without a reader fails before anything is downloaded. Then the
   video is fetched (Telegram ≤ `media.video_max_mb`, or the link's file under the same cap) into
   `DATA/media/<yyyymmdd>/media-<token>/` (0700/0600; purged after `media.retention_days`), or the YouTube watch URL is
   used as it is. Each reader gets what is left of `timeouts.media_s`; a key, quota, rate or outage failure moves on to
   the next one. An answered reading is recorded via `shared/cost` (source `video`).
3. Pages: `WebTools.read_page` returns the readable text (≤ `media.page_max_chars`); login walls and paywalls come
   back as their own errors, so the owner is told the bot cannot see the content (UC-31 F).
4. Everything from the content reaches the assistant only inside `material` (`domain.material_parts`, rendered by
   `prompts/personal/media_material.md`): a nonce-tagged block with spoofed tags defused, the text scrubbed of known
   secrets. Nothing from the content is ever an instruction, a setting change or a tool call.

Caps (NFR-COST-02, T51): before any download, each paid reader is checked with `cost.service.allowance`; one whose
monthly cap is used up is skipped, and when every reader with a key is capped the owner gets `CostCapReached` with
the cap (raising it is a settings change, one question).

Expected failures never raise: they are a `MediaFailure`. Logs carry kinds, codes, providers, sizes and durations,
never a URL, a title or content.
"""

import asyncio
import os
import secrets
from datetime import datetime, timedelta
from pathlib import Path
from typing import Final

from yordamchi import prompts
from yordamchi.core import redact, timefmt
from yordamchi.core import strings as S
from yordamchi.core.errors import (
    CostCapReached,
    ErrorCode,
    ExternalError,
    MediaNoAnswer,
    MediaNoVideoKey,
    MediaTooLarge,
    WebUrlInvalid,
    YordamchiError,
)
from yordamchi.core.log import get_log
from yordamchi.personal.artefacts import domain as artefacts
from yordamchi.personal.deps import PersonalDeps
from yordamchi.personal.media import domain
from yordamchi.personal.media.domain import (
    Attempt,
    MediaBrief,
    MediaFailure,
    MediaFindings,
    MediaKind,
    MediaLimits,
    MediaOutcome,
    MediaRequest,
)
from yordamchi.ports.media import MediaAnalysis, VideoSource
from yordamchi.shared.cost import domain as cost_domain
from yordamchi.shared.cost import service as cost
from yordamchi.shared.cost.domain import PAYABLE, Allowance, Provider
from yordamchi.shared.outbox import service as outbox
from yordamchi.shared.outbox.domain import Kind
from yordamchi.shared.settings import defaults
from yordamchi.shared.settings import service as settings
from yordamchi.shared.settings.domain import Snapshot
from yordamchi.shared.vault import service as vault
from yordamchi.shared.voice import domain as voice

log = get_log(__name__)

FOLDER_KIND: Final = "media"
PROMPT_SYSTEM: Final = "personal/media_video_system"
PROMPT_REQUEST: Final = "personal/media_video_request"
PROMPT_MATERIAL: Final = "personal/media_material"
NO_QUESTION: Final = "(none)"


# --- the ack ---------------------------------------------------------------------------------------------------------
async def announce_looking(deps: PersonalDeps, request: MediaRequest, *, snap: Snapshot | None = None) -> None:
    """Queue "Ko'ryapman" for this request (once per ref: the outbox dedupes a retry)."""
    snap = snap if snap is not None else await settings.snapshot(deps)
    async with deps.db.uow() as s:
        await outbox.enqueue_text(
            s,
            bot=deps.bot,
            chat_id=request.chat_id,
            text=S.LOOKING(),
            kind=Kind.ACK,
            ref=f"media-{request.ref}",
            at=deps.clock.now(),
            max_parts=snap[defaults.OUTBOX_MAX_PARTS],
            reply_to=request.reply_to,
        )


# --- the use case ----------------------------------------------------------------------------------------------------
class _Budget:
    """What is left of `timeouts.media_s`, on `deps.clock` (virtual in tests)."""

    def __init__(self, deps: PersonalDeps, seconds: float) -> None:
        self._deps = deps
        self._deadline = deps.clock.now() + timedelta(seconds=seconds)

    def left(self) -> float:
        return max(0.0, (self._deadline - self._deps.clock.now()).total_seconds())


async def analyze_media(deps: PersonalDeps, request: MediaRequest) -> MediaOutcome:
    """One media request to a brief for the assistant, or the owner's failure message (see the module docstring)."""
    started = deps.clock.now()
    snap = await settings.snapshot(deps)
    if request.announce:
        await announce_looking(deps, request, snap=snap)
    limits = domain.media_limits(snap)
    budget = _Budget(deps, limits.budget_s)
    try:
        if request.kind is MediaKind.PAGE:
            outcome: MediaOutcome = await _page(deps, request, limits, budget)
        else:
            outcome = await _video(deps, snap, request, limits, budget)
    except YordamchiError as error:
        outcome = MediaFailure(kind=request.kind, code=error.code, text=S.ERRORS[error.code](**error.params))
    log.info(
        "media_analyzed",
        kind=request.kind.value,
        ok=outcome.ok,
        error_code=outcome.code.value if isinstance(outcome, MediaFailure) else None,
        provider=outcome.provider if isinstance(outcome, MediaBrief) else None,
        duration_ms=_ms_since(deps, started),
    )
    return outcome


async def _page(deps: PersonalDeps, request: MediaRequest, limits: MediaLimits, budget: _Budget) -> MediaOutcome:
    url = artefacts.check_url(request.url or "")
    page = await deps.web.read_page(
        url, max_chars=limits.page_max_chars, max_bytes=limits.page_max_bytes, timeout=budget.left()
    )
    site = domain.site_of(page.url or url)
    material = _material(kind=MediaKind.PAGE, site=site, title=page.title, body=page.text, truncated=page.truncated)
    return MediaBrief(kind=MediaKind.PAGE, site=site, material=material, title=page.title, truncated=page.truncated)


async def _video(
    deps: PersonalDeps,
    snap: Snapshot,
    request: MediaRequest,
    limits: MediaLimits,
    budget: _Budget,
) -> MediaOutcome:
    if request.kind is MediaKind.VIDEO and request.attachment is not None:
        domain.check_video(request.attachment, limits)
    order = domain.pick_video(snap[defaults.SPEECH_VIDEO_PROVIDER], await _with_key(deps))
    order, capped = await _under_cap(deps, snap, order)
    if not order:
        if capped:
            first = capped[0]
            raise CostCapReached(
                provider=voice.provider_label(first.provider.value), cap=cost_domain.cap_text(first.cap_usd)
            )
        raise MediaNoVideoKey
    found = await _source(deps, snap, request, limits, budget)
    if isinstance(found, MediaFailure):
        return found
    source, site = found
    instruction = prompts.render(PROMPT_SYSTEM, language=domain.language_name(snap[defaults.LANG_REPLY]))
    prompt = prompts.render(PROMPT_REQUEST, question=" ".join(request.question.split()) or NO_QUESTION)
    attempts: list[Attempt] = []
    last: ExternalError | None = None
    for provider in order:
        timeout = budget.left()
        if attempts and timeout < domain.MIN_ATTEMPT_S:
            break  # the budget is spent; the owner hears about the failures so far
        try:
            analysis = await deps.media_analyzers[provider].analyze_video(
                source,
                instruction=instruction,
                prompt=prompt,
                model=domain.video_model(provider, snap),
                timeout=timeout,
            )
        except ExternalError as error:
            attempts.append(Attempt(provider=provider, code=error.code))
            last = error
            log.warning("video_provider_failed", provider=provider, error_code=error.code.value)
            if domain.falls_back(error.code):
                continue
            break
        await _record(deps, analysis)
        return _video_brief(request.kind, site, analysis)
    if last is None:  # every provider was skipped: nothing was asked
        raise MediaNoVideoKey
    return MediaFailure(kind=request.kind, code=last.code, text=domain.failure_text(attempts, last.params))


def _video_brief(kind: MediaKind, site: str, analysis: MediaAnalysis) -> MediaBrief:
    findings = domain.parse_findings(analysis.text)
    if findings is None:
        raise MediaNoAnswer
    body = domain.findings_json(_scrubbed(findings))
    material = _material(kind=kind, site=site, title="", body=body, truncated=False)
    return MediaBrief(
        kind=kind, site=site, material=material, findings=findings, provider=analysis.provider, model=analysis.model
    )


def _scrubbed(findings: MediaFindings) -> MediaFindings:
    return MediaFindings(
        summary=redact.scrubbed(findings.summary),
        key_points=tuple(redact.scrubbed(point) for point in findings.key_points),
        opinion_hook=redact.scrubbed(findings.opinion_hook),
    )


def _material(*, kind: MediaKind, site: str, title: str, body: str, truncated: bool) -> str:
    parts = domain.material_parts(
        kind=kind,
        site=site,
        title=redact.scrubbed(title),
        body=redact.scrubbed(body),
        truncated=truncated,
        nonce=secrets.token_hex(8),
    )
    return prompts.render(
        PROMPT_MATERIAL,
        tag=parts.tag,
        kind=parts.kind,
        site=parts.site,
        title=parts.title,
        truncated=parts.truncated,
        body=parts.body,
    )


async def _source(
    deps: PersonalDeps, snap: Snapshot, request: MediaRequest, limits: MediaLimits, budget: _Budget
) -> tuple[VideoSource, str] | MediaFailure:
    """The video to read and the site it came from, or why there is none."""
    if request.kind is MediaKind.YOUTUBE:
        url = request.url or ""
        if domain.youtube_watch_url(url) is None:
            raise WebUrlInvalid
        return VideoSource(url=url), domain.site_of(url)
    folder = await _request_folder(deps, snap)
    if request.kind is MediaKind.VIDEO_LINK:
        url = artefacts.check_url(request.url or "")
        site = domain.site_of(url)
        try:
            fetched = await deps.web.fetch_file(
                url, dest_dir=folder, max_bytes=limits.video_max_bytes, timeout=budget.left()
            )
        except ExternalError as error:
            if error.code is ErrorCode.WEB_CONTENT_TOO_LARGE:
                raise MediaTooLarge(limit_mb=limits.video_max_mb) from error
            raise
        if not fetched.mime.lower().startswith("video/"):  # the link was a page or an image after all
            return MediaFailure(
                kind=request.kind, code=ErrorCode.VIDEO_NOT_FOUND, text=S.ERRORS[ErrorCode.VIDEO_NOT_FOUND](site=site)
            )
        return VideoSource(path=fetched.path, mime=fetched.mime), site
    attachment = request.attachment
    if attachment is None:
        msg = "a video request carries its attachment"
        raise ValueError(msg)
    try:
        data = await deps.messenger.download(
            attachment.file_id, max_bytes=limits.video_max_bytes, timeout=budget.left()
        )
    except ExternalError as error:
        log.warning("media_download_failed", error_code=error.code.value)
        return MediaFailure(kind=request.kind, code=error.code, text=S.MEDIA_DOWNLOAD_FAILED())
    path = folder / domain.video_file_name(attachment)
    await asyncio.to_thread(_write_private, path, data)
    return VideoSource(path=path, mime=attachment.mime), "Telegram"


async def _with_key(deps: PersonalDeps) -> tuple[str, ...]:
    """Providers with a speech key in the vault and a reader in this bot."""
    keys = voice.current_keys(await vault.list_secrets(deps))
    return tuple(provider for provider in keys if provider in deps.media_analyzers)


async def _under_cap(
    deps: PersonalDeps, snap: Snapshot, order: tuple[str, ...]
) -> tuple[tuple[str, ...], tuple[Allowance, ...]]:
    """`order` without the paid readers whose monthly cap is used up (T51), and those readers' allowances."""
    kept: list[str] = []
    capped: list[Allowance] = []
    for provider in order:
        if provider in PAYABLE:
            allowance = await cost.allowance(deps, Provider(provider), snap=snap)
            if not allowance.allowed:
                capped.append(allowance)
                log.warning("video_provider_capped", provider=provider)
                continue
        kept.append(provider)
    return tuple(kept), tuple(capped)


async def _record(deps: PersonalDeps, analysis: MediaAnalysis) -> None:
    async with deps.db.uow() as s:
        await cost.record_usage(s, bot=deps.bot, at=deps.clock.now(), usage=domain.usage_of(analysis))


async def _request_folder(deps: PersonalDeps, snap: Snapshot) -> Path:
    day = timefmt.local(deps.clock.now(), snap[defaults.UI_TIMEZONE]).date()
    folder = deps.paths.media / f"{day:%Y%m%d}" / f"{FOLDER_KIND}-{secrets.token_hex(6)}"
    await asyncio.to_thread(_make_private_dir, folder)
    return folder


def _make_private_dir(folder: Path) -> None:
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    for level in (folder.parent, folder):
        level.chmod(0o700)


def _write_private(path: Path, data: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as out:
        out.write(data)


def _ms_since(deps: PersonalDeps, started: datetime) -> int:
    return max(0, round((deps.clock.now() - started).total_seconds() * 1000))
