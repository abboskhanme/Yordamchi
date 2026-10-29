"""Files the owner sends, read for the assistant, and the documents it makes (UC-37; SPEC §3.2.17).

    found = await files.read_burst(deps, item)                     # before the assistant is asked
    made = files.MadeDocuments()
    tools = files.toolset(deps, made=made, limits=files.document_limits(snap))
    await qa.answer(…, documents=found.pdfs, toolsets=(…, tools))  # found.material goes into the question
    await files.send_documents(deps, made, chat_id=…, ref=…)       # after the answer's text

**Reading** (`read_burst`). Each file part (`domain.file_parts`) is checked against `files.max_mb` (and an audio
file's reported length against `files.audio_max_min`) before anything is downloaded into this burst's folder
`DATA/media/<yyyymmdd>/file-<token>/` (0700/0600, purged with the media after `media.retention_days`):
- Word, Excel, PowerPoint and text files become text in a worker thread (`domain.text_of`);
- a PDF is saved for Claude's `document` block, with a short note naming it in the question;
- an audio file is announced ("Tinglayapman…"), compressed to MP3 (`WebTools.compress_audio`) and transcribed by the
  video reader's providers in order (`speech.video_provider`, the speech keys in the vault, the T51 caps; a key,
  quota, rate or outage failure moves on to the next one) within `timeouts.audio_file_s`. The transcript is recorded
  via `shared/cost` (source `audio_file`), sent to the owner as a `.txt` file when `files.send_transcript`, and given
  to the assistant like any other text.
Every text reaches the assistant only as material (`prompts/personal/files_material.md`, a nonce-tagged, defused
block cut at `files.text_max_chars`). A file that cannot be read is the owner's message, never a raise.

**Making documents** (`toolset`). The `yordamchi_files` server's one tool, `make_document`, validates and parses its
arguments (`domain.document_of`), writes the file through `DocumentWriter` within `timeouts.document_s` and keeps it
in `MadeDocuments`; the caller queues them after the answer's text (`send_documents`). A refused or failed document is
an error result to the model, which tells the owner. Tool results are data for the model, never instructions.

Logs carry kinds, sizes, durations, providers and codes, never a file name or content.
"""

import asyncio
import json
import os
import secrets
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Final

from yordamchi import prompts
from yordamchi.core import redact, timefmt
from yordamchi.core import strings as S
from yordamchi.core.errors import (
    CostCapReached,
    DocumentInvalid,
    ExternalError,
    FileTooLong,
    MediaVideoOff,
    YordamchiError,
)
from yordamchi.core.json_value import JsonObject, JsonValue
from yordamchi.core.log import get_log
from yordamchi.core.text import BotText
from yordamchi.personal.deps import PersonalDeps
from yordamchi.personal.files import domain
from yordamchi.personal.files.domain import DocumentLimits, FileKind, FileLimits
from yordamchi.personal.media import domain as media
from yordamchi.ports.assistant import AssistantTool, AssistantToolset, ToolOutput
from yordamchi.ports.documents import WrittenDocument
from yordamchi.ports.media import MediaAnalysis
from yordamchi.shared.burst.domain import BurstPart, TurnInput
from yordamchi.shared.cost import domain as cost_domain
from yordamchi.shared.cost import service as cost
from yordamchi.shared.cost.domain import PAYABLE, Allowance, Provider
from yordamchi.shared.inbox.domain import Attachment
from yordamchi.shared.outbox import service as outbox
from yordamchi.shared.outbox.domain import Kind, Op
from yordamchi.shared.settings import defaults
from yordamchi.shared.settings import service as settings
from yordamchi.shared.settings.domain import Snapshot
from yordamchi.shared.vault import service as vault
from yordamchi.shared.voice import domain as voice

log = get_log(__name__)

FOLDER_KIND: Final = "file"
PROMPT_MATERIAL: Final = "personal/files_material"
PROMPT_PDF: Final = "personal/files_pdf"
PROMPT_TRANSCRIBE_SYSTEM: Final = "personal/files_transcribe_system"
PROMPT_TRANSCRIBE_REQUEST: Final = "personal/files_transcribe_request"
PROMPT_TOOL: Final = "personal/files_tool_make_document"
NO_QUESTION: Final = "(none)"
SERVER: Final = "yordamchi_files"
DOCUMENT_TOOL: Final = "make_document"
TRANSCRIPT_MIME: Final = "text/plain"
# What `make_document` adds to `timeouts.document_s` before the adapter gives up on the tool call.
DOCUMENT_MARGIN_S: Final = 5.0
DOCUMENT_SCHEMA: Final[JsonObject] = {
    "type": "object",
    "properties": {
        "format": {"type": "string", "enum": ["docx", "pdf", "xlsx", "md", "txt"]},
        "filename": {"type": "string", "maxLength": 200},
        "title": {"type": "string", "maxLength": domain.TITLE_MAX_CHARS},
        "content": {"type": "string", "maxLength": domain.CONTENT_MAX_CHARS},
        "sheets": {
            "type": "array",
            "maxItems": domain.SHEETS_MAX,
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "rows": {"type": "array", "items": {"type": "array"}},
                },
                "required": ["rows"],
            },
        },
    },
    "required": ["format", "filename"],
    "additionalProperties": False,
}


# --- reading ---------------------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class BurstFiles:
    """What one burst's files give the assistant: material blocks (texts, transcripts, PDF notes), the PDFs for its
    `document` blocks, and the owner's message for each file that could not be read."""

    material: tuple[str, ...] = field(default=(), repr=False)
    pdfs: tuple[Path, ...] = ()
    failures: tuple[BotText, ...] = ()

    @property
    def read(self) -> bool:
        return bool(self.material or self.pdfs)


@dataclass(frozen=True, slots=True)
class _Read:
    material: str | None = None
    pdf: Path | None = None
    failure: BotText | None = None


class _Budget:
    """What is left of one file's time, on `deps.clock` (virtual in tests)."""

    def __init__(self, deps: PersonalDeps, seconds: float) -> None:
        self._deps = deps
        self._deadline = deps.clock.now() + timedelta(seconds=seconds)

    def left(self) -> float:
        return max(0.0, (self._deadline - self._deps.clock.now()).total_seconds())


async def read_burst(deps: PersonalDeps, item: TurnInput) -> BurstFiles:
    """Every file of the burst read in order (module docstring). Never raises for a file that cannot be read."""
    wanted = domain.file_parts(item.parts)
    if not wanted:
        return BurstFiles()
    snap = await settings.snapshot(deps)
    limits = domain.file_limits(snap)
    folder: Path | None = None
    material: list[str] = []
    pdfs: list[Path] = []
    failures: list[BotText] = []
    for part, kind in wanted:
        attachment = part.content.attachment
        if attachment is None:
            continue
        name = domain.display_name(attachment, kind)
        if kind is FileKind.OTHER:
            failures.append(S.FILE_UNSUPPORTED(name=name))
            continue
        if kind is FileKind.PDF and len(pdfs) >= limits.max_pdfs:
            failures.append(S.FILE_TOO_MANY_PDFS(limit=limits.max_pdfs))
            continue
        try:
            domain.check_file(attachment, kind, limits)
            folder = folder or await _request_folder(deps, snap)
            read = await _read_one(deps, snap, item, part, kind, name=name, limits=limits, folder=folder)
        except YordamchiError as error:
            read = _Read(failure=S.ERRORS[error.code](**error.params))
        if read.material is not None:
            material.append(read.material)
        if read.pdf is not None:
            pdfs.append(read.pdf)
        if read.failure is not None:
            failures.append(read.failure)
        log.info("file_read", kind=kind.value, ok=read.failure is None, size_bytes=attachment.size_bytes)
    return BurstFiles(material=tuple(material), pdfs=tuple(pdfs), failures=tuple(dict.fromkeys(failures)))


async def _read_one(
    deps: PersonalDeps,
    snap: Snapshot,
    item: TurnInput,
    part: BurstPart,
    kind: FileKind,
    *,
    name: str,
    limits: FileLimits,
    folder: Path,
) -> _Read:
    attachment = part.content.attachment
    if attachment is None:
        msg = "a file part carries its attachment"
        raise ValueError(msg)
    if kind is FileKind.AUDIO:
        return await _transcribe(deps, snap, item, part, attachment, name=name, limits=limits, folder=folder)
    data = await _download(deps, attachment, limits, timeout=limits.download_s)
    if data is None:
        return _Read(failure=S.MEDIA_DOWNLOAD_FAILED())
    if kind is FileKind.PDF:
        pages = domain.pdf_pages(data)
        if pages is not None and pages > limits.pdf_max_pages:
            return _Read(failure=S.FILE_PDF_TOO_LONG(name=name, pages=pages, limit=limits.pdf_max_pages))
        path = folder / f"{part.update_id}-{domain.saved_name(kind, attachment)}"
        await asyncio.to_thread(_write_private, path, data)
        return _Read(material=prompts.render(PROMPT_PDF, name=media.defuse(name)), pdf=path)
    text = await asyncio.to_thread(domain.text_of, kind, data)
    if text is None or not text.strip():
        return _Read(failure=S.FILE_EMPTY(name=name))
    return _Read(material=_material(kind, name, text, limits))


async def _download(deps: PersonalDeps, attachment: Attachment, limits: FileLimits, *, timeout: float) -> bytes | None:
    try:
        return await deps.messenger.download(attachment.file_id, max_bytes=limits.max_bytes, timeout=timeout)
    except ExternalError as error:
        log.warning("file_download_failed", error_code=error.code.value)
        return None


def _material(kind: FileKind, name: str, text: str, limits: FileLimits) -> str:
    body, truncated = domain.cut(redact.scrubbed(text), limits.text_max_chars)
    parts = domain.material_parts(kind=kind, name=name, body=body, truncated=truncated, nonce=secrets.token_hex(8))
    return prompts.render(
        PROMPT_MATERIAL,
        tag=parts.tag,
        kind=parts.kind,
        name=parts.name,
        truncated=parts.truncated,
        body=parts.body,
    )


# --- audio -----------------------------------------------------------------------------------------------------------
async def _transcribe(
    deps: PersonalDeps,
    snap: Snapshot,
    item: TurnInput,
    part: BurstPart,
    attachment: Attachment,
    *,
    name: str,
    limits: FileLimits,
    folder: Path,
) -> _Read:
    order = await _readers(deps, snap)
    if not isinstance(order, tuple):
        return _Read(failure=order)
    await _announce(deps, snap, item.chat_id, part)
    budget = _Budget(deps, limits.audio_budget_s)
    data = await _download(deps, attachment, limits, timeout=min(limits.download_s, budget.left()))
    if data is None:
        return _Read(failure=S.MEDIA_DOWNLOAD_FAILED())
    source = folder / f"{part.update_id}-{domain.saved_name(FileKind.AUDIO, attachment)}"
    await asyncio.to_thread(_write_private, source, data)
    compressed = await deps.web.compress_audio(source, dest_dir=folder, kbps=limits.audio_kbps, timeout=budget.left())
    if compressed.duration_s is not None and compressed.duration_s > limits.audio_max_s:
        raise FileTooLong(max_minutes=limits.audio_max_s // 60)
    instruction = prompts.render(
        PROMPT_TRANSCRIBE_SYSTEM, languages=domain.languages_text(snap[defaults.STT_LANGUAGES])
    )
    question = " ".join((part.text or item.own_text).split())
    prompt = prompts.render(PROMPT_TRANSCRIBE_REQUEST, question=media.defuse(question) or NO_QUESTION)
    attempts: list[media.Attempt] = []
    last: ExternalError | None = None
    for provider in order:
        timeout = budget.left()
        if attempts and timeout < media.MIN_ATTEMPT_S:
            break
        try:
            analysis = await deps.media_analyzers[provider].analyze_audio(
                compressed.path,
                mime=compressed.mime,
                instruction=instruction,
                prompt=prompt,
                model=media.video_model(provider, snap),
                timeout=timeout,
            )
        except ExternalError as error:
            attempts.append(media.Attempt(provider=provider, code=error.code))
            last = error
            log.warning("audio_provider_failed", provider=provider, error_code=error.code.value)
            if media.falls_back(error.code):
                continue
            break
        await _record(deps, analysis)
        return await _transcribed(deps, snap, item, part, analysis, name=name, limits=limits, folder=folder)
    if last is None:
        return _Read(failure=S.FILE_AUDIO_NO_KEY())
    return _Read(failure=domain.audio_failure_text(attempts, last.params))


async def _readers(deps: PersonalDeps, snap: Snapshot) -> tuple[str, ...] | BotText:
    """The readers to try, in order, or the owner's message when there is none (off, no key, every cap used up)."""
    try:
        order = media.pick_video(snap[defaults.SPEECH_VIDEO_PROVIDER], await _with_key(deps))
    except MediaVideoOff:
        return S.FILE_AUDIO_OFF()
    order, capped = await _under_cap(deps, snap, order)
    if order:
        return order
    if capped:
        first = capped[0]
        error = CostCapReached(
            provider=voice.provider_label(first.provider.value), cap=cost_domain.cap_text(first.cap_usd)
        )
        return S.ERRORS[error.code](**error.params)
    return S.FILE_AUDIO_NO_KEY()


async def _transcribed(
    deps: PersonalDeps,
    snap: Snapshot,
    item: TurnInput,
    part: BurstPart,
    analysis: MediaAnalysis,
    *,
    name: str,
    limits: FileLimits,
    folder: Path,
) -> _Read:
    transcript = str(redact.scrubbed(analysis.text.strip()))
    if not transcript:
        return _Read(failure=S.FILE_EMPTY(name=name))
    log.info("audio_transcribed", provider=analysis.provider, chars=len(transcript), duration_ms=analysis.latency_ms)
    if snap[defaults.FILES_SEND_TRANSCRIPT]:
        filename = domain.transcript_file_name(name)
        path = folder / f"{part.update_id}-{filename}"
        await asyncio.to_thread(_write_private, path, transcript.encode("utf-8"))
        async with deps.db.uow() as s:
            await outbox.enqueue_file(
                s,
                bot=deps.bot,
                chat_id=item.chat_id,
                op=Op.SEND_DOCUMENT,
                path=path,
                kind=Kind.FILE,
                ref=f"files-transcript-{part.update_id}",
                at=deps.clock.now(),
                max_parts=snap[defaults.OUTBOX_MAX_PARTS],
                caption=S.FILE_TRANSCRIPT_CAPTION(name=name),
                filename=filename,
                mime=TRANSCRIPT_MIME,
                reply_to=part.message_id,
            )
    return _Read(material=_material(FileKind.AUDIO, name, transcript, limits))


async def _announce(deps: PersonalDeps, snap: Snapshot, chat_id: int, part: BurstPart) -> None:
    """Queue "Tinglayapman…" for this recording (once per update: the outbox dedupes a retry)."""
    async with deps.db.uow() as s:
        await outbox.enqueue_text(
            s,
            bot=deps.bot,
            chat_id=chat_id,
            text=S.FILE_LISTENING(),
            kind=Kind.ACK,
            ref=f"files-listen-{part.update_id}",
            at=deps.clock.now(),
            max_parts=snap[defaults.OUTBOX_MAX_PARTS],
            reply_to=part.message_id,
        )


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
                log.warning("audio_provider_capped", provider=provider)
                continue
        kept.append(provider)
    return tuple(kept), tuple(capped)


async def _record(deps: PersonalDeps, analysis: MediaAnalysis) -> None:
    async with deps.db.uow() as s:
        await cost.record_usage(s, bot=deps.bot, at=deps.clock.now(), usage=domain.usage_of(analysis))


# --- folders ---------------------------------------------------------------------------------------------------------
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


# --- making documents ------------------------------------------------------------------------------------------------
@dataclass(slots=True)
class MadeDocuments:
    """The documents one answer made, in order; queued after its text by `send_documents`."""

    items: list[tuple[WrittenDocument, str]] = field(default_factory=list)  # the file and its name for the owner

    def __len__(self) -> int:
        return len(self.items)


def _json_output(payload: JsonObject, *, error: bool = False) -> ToolOutput:
    return ToolOutput(text=redact.scrubbed(json.dumps(payload, ensure_ascii=False, sort_keys=True)), is_error=error)


def toolset(deps: PersonalDeps, *, made: MadeDocuments, limits: DocumentLimits) -> AssistantToolset:
    """The assistant's `yordamchi_files` tools for one answer (module docstring)."""
    folder: list[Path] = []

    async def make(arguments: Mapping[str, JsonValue]) -> ToolOutput:
        if len(made) >= limits.max_documents:
            return _json_output({"status": "refused", "error": "too_many_documents"}, error=True)
        try:
            spec = domain.document_of(arguments)
        except DocumentInvalid as error:
            return _json_output({"status": "refused", "error": error.code.value, **error.params}, error=True)
        if not folder:
            folder.append(await _request_folder(deps, await settings.snapshot(deps)))
        if (folder[0] / spec.file_name).exists():
            return _json_output({"status": "refused", "error": "same_filename", "file": spec.file_name}, error=True)
        try:
            written = await deps.documents.write(spec, dest_dir=folder[0], timeout=limits.timeout_s)
        except YordamchiError as error:
            log.warning("document_failed", format=spec.format.value, error_code=error.code.value)
            tell = S.ERRORS[error.code](**error.params) if error.code in S.ERRORS else None
            payload: JsonObject = {"status": "failed", "error": error.code.value}
            if tell is not None:
                payload["tell_owner"] = str(tell)
            return _json_output(payload, error=True)
        if written.size_bytes > limits.max_bytes:
            await asyncio.to_thread(written.path.unlink, missing_ok=True)
            return _json_output({"status": "refused", "error": "too_large"}, error=True)
        made.items.append((written, spec.file_name))
        log.info("document_made", format=spec.format.value, size_bytes=written.size_bytes, count=len(made))
        size_kb = max(1, written.size_bytes // 1024)
        return _json_output({"status": "made", "file": spec.file_name, "size_kb": size_kb})

    return AssistantToolset(
        server=SERVER,
        tools=(
            AssistantTool(
                name=DOCUMENT_TOOL,
                description=prompts.render(PROMPT_TOOL),
                input_schema=DOCUMENT_SCHEMA,
                timeout_s=limits.timeout_s + DOCUMENT_MARGIN_S,
                read_only=False,
                handler=make,
            ),
        ),
    )


async def send_documents(
    deps: PersonalDeps, made: MadeDocuments, *, chat_id: int, ref: str, reply_to: int | None = None
) -> tuple[int, ...]:
    """Queue the answer's documents as files, in the order made (keyed `<ref>-<n>`). Returns the outbox row ids."""
    if not made.items:
        return ()
    snap = await settings.snapshot(deps)
    ids: list[int] = []
    async with deps.db.uow() as s:
        for index, (written, name) in enumerate(made.items):
            ids.extend(
                await outbox.enqueue_file(
                    s,
                    bot=deps.bot,
                    chat_id=chat_id,
                    op=Op.SEND_DOCUMENT,
                    path=written.path,
                    kind=Kind.FILE,
                    ref=f"{ref}-{index}",
                    at=deps.clock.now(),
                    max_parts=snap[defaults.OUTBOX_MAX_PARTS],
                    filename=name,
                    mime=written.mime,
                    reply_to=reply_to if index == 0 else None,
                )
            )
    log.info("documents_queued", count=len(made.items))
    return tuple(ids)
