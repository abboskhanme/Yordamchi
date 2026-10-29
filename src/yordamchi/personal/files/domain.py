"""File rules, pure (UC-37; SPEC §3.2.17): which parts are files to read, how each kind becomes text, and how the
assistant's `make_document` arguments become a `DocumentSpec`.

**Which parts.** `file_kind` sorts a part's attachment by its declared type, then its suffix. Photos, images sent as
files and videos are `personal/media`'s (image blocks, `analyze_video`); voice notes are STT's. What is left:
- `AUDIO`: an audio message, or a file of type `audio/*` or with an audio suffix: transcribed by the speech reader;
- `PDF`: handed to Claude as a `document` block, at most `files.pdf_max_pages` by its own page count (best effort);
- `WORD`, `SHEET`, `SLIDES` (`.docx`, `.xlsx`, `.pptx`): their text, read here from the OOXML zip with the stdlib;
- `TEXT`: decoded (UTF-8, UTF-16 by BOM, else cp1251);
- `OTHER`: refused with a note of the types the bot reads.

**Content is data.** A file's text and an audio transcript reach the assistant only inside a material block whose
boundary carries a per-request nonce (`media.domain.boundary`), with every spoofable tag defused
(`media.domain.defuse`).

**Documents.** `document_of` validates the tool's arguments and parses its Markdown into the port's blocks; the
filename is cleaned to letters, digits, spaces, `-` and `_`. Nothing in the arguments is ever interpreted as markup
by the writer: a run is text with a style.
"""

import io
import re
import zipfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Final
from xml.etree import ElementTree as ET  # the stdlib's own spelling

from yordamchi.core import strings as S
from yordamchi.core.errors import (
    DocumentInvalid,
    FileTooLong,
    FileUnreadable,
    MediaTooLarge,
)
from yordamchi.core.json_value import JsonValue
from yordamchi.core.text import BotText, join
from yordamchi.personal.media import domain as media
from yordamchi.ports.documents import (
    Block,
    CodeBlock,
    DocFormat,
    DocumentSpec,
    Heading,
    ListItem,
    Paragraph,
    Rule,
    Run,
    Runs,
    Sheet,
    Table,
)
from yordamchi.ports.media import MediaAnalysis
from yordamchi.shared.burst.domain import BurstPart
from yordamchi.shared.cost.domain import Provider, Usage
from yordamchi.shared.inbox.domain import Attachment, UpdateKind
from yordamchi.shared.settings import defaults
from yordamchi.shared.settings.domain import Snapshot
from yordamchi.shared.voice import domain as voice

MIB: Final = 1024 * 1024
USAGE_SOURCE: Final = "audio_file"
# An OOXML part bigger than this is not read: a real document's XML is far smaller; a zip bomb is not.
XML_PART_MAX_BYTES: Final = 64 * MIB
SLIDES_MAX: Final = 500
SHEET_ROWS_MAX: Final = 20000
NUL_PROBE_BYTES: Final = 4096
FILENAME_MAX_CHARS: Final = 80
DEFAULT_FILENAME: Final = "hujjat"
TITLE_MAX_CHARS: Final = 200
CONTENT_MAX_CHARS: Final = 300000
SHEETS_MAX: Final = 20
SHEET_NAME_MAX_CHARS: Final = 31  # Excel's own limit
SHEET_COLUMNS_MAX: Final = 200
CELL_MAX_CHARS: Final = 32767  # Excel's own limit
LIST_DEPTH_MAX: Final = 3


class FileKind(StrEnum):
    AUDIO = "audio"
    PDF = "pdf"
    WORD = "word"
    SHEET = "sheet"
    SLIDES = "slides"
    TEXT = "text"
    OTHER = "other"


TEXT_KINDS: Final = frozenset({FileKind.WORD, FileKind.SHEET, FileKind.SLIDES, FileKind.TEXT})
AUDIO_SUFFIXES: Final = frozenset(
    {".mp3", ".m4a", ".wav", ".ogg", ".oga", ".opus", ".aac", ".flac", ".amr", ".wma", ".aiff", ".aif", ".weba"}
)
TEXT_SUFFIXES: Final = frozenset(
    {
        ".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".jsonl", ".xml", ".html", ".htm", ".yaml", ".yml",
        ".log", ".srt", ".vtt", ".ini", ".toml", ".cfg", ".conf", ".sql", ".py", ".js", ".ts", ".tsx", ".jsx",
        ".java", ".kt", ".go", ".rs", ".c", ".h", ".cpp", ".cs", ".php", ".rb", ".swift", ".sh", ".css", ".scss",
        ".rtf", ".tex",
    }
)  # fmt: skip
_SUFFIX_KINDS: Final[Mapping[str, FileKind]] = {
    ".pdf": FileKind.PDF,
    ".docx": FileKind.WORD,
    ".xlsx": FileKind.SHEET,
    ".pptx": FileKind.SLIDES,
}
_MIME_KINDS: Final[Mapping[str, FileKind]] = {
    "application/pdf": FileKind.PDF,
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": FileKind.WORD,
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": FileKind.SHEET,
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": FileKind.SLIDES,
    "application/json": FileKind.TEXT,
    "application/xml": FileKind.TEXT,
    "application/x-yaml": FileKind.TEXT,
    "application/yaml": FileKind.TEXT,
    "application/x-subrip": FileKind.TEXT,
    "application/rtf": FileKind.TEXT,
}
# What the material block calls each kind (model-facing, English).
KIND_LABELS: Final[Mapping[FileKind, str]] = {
    FileKind.AUDIO: "audio recording, as a verbatim transcript",
    FileKind.PDF: "PDF document",
    FileKind.WORD: "Word document, as text",
    FileKind.SHEET: "Excel workbook, every sheet as tab-separated rows",
    FileKind.SLIDES: "PowerPoint presentation, every slide's text",
    FileKind.TEXT: "text file",
}
DEFAULT_NAMES: Final[Mapping[FileKind, str]] = {
    FileKind.AUDIO: "audio",
    FileKind.PDF: "hujjat.pdf",
    FileKind.WORD: "hujjat.docx",
    FileKind.SHEET: "jadval.xlsx",
    FileKind.SLIDES: "taqdimot.pptx",
    FileKind.TEXT: "matn.txt",
    FileKind.OTHER: "fayl",
}


# --- which parts --------------------------------------------------------------------------------------------------
def _suffix(name: str | None) -> str:
    return PurePosixPath(name or "").suffix.lower()


def file_kind(kind: UpdateKind, attachment: Attachment | None) -> FileKind | None:
    """The kind of file a part carries, or None when it carries none this feature reads (no attachment, a photo, an
    image or video sent as a file, a voice note, a video message)."""
    if attachment is None or kind not in {UpdateKind.AUDIO, UpdateKind.DOCUMENT}:
        return None
    if kind is UpdateKind.AUDIO:
        return FileKind.AUDIO
    if media.image_suffix(kind, attachment.mime) is not None or media.is_video_attachment(kind, attachment):
        return None
    return _document_kind((attachment.mime or "").split(";", 1)[0].strip().lower(), _suffix(attachment.file_name))


def _document_kind(mime: str, suffix: str) -> FileKind:
    if mime.startswith("audio/") or suffix in AUDIO_SUFFIXES:
        return FileKind.AUDIO
    if found := _MIME_KINDS.get(mime) or _SUFFIX_KINDS.get(suffix):
        return found
    if mime.startswith("text/") or suffix in TEXT_SUFFIXES:
        return FileKind.TEXT
    return FileKind.OTHER


def file_parts(parts: Iterable[BurstPart]) -> tuple[tuple[BurstPart, FileKind], ...]:
    """The burst's parts this feature reads, with their kind, in arrival order."""
    found: list[tuple[BurstPart, FileKind]] = []
    for part in parts:
        kind = file_kind(part.kind, part.content.attachment)
        if kind is not None:
            found.append((part, kind))
    return tuple(found)


def display_name(attachment: Attachment, kind: FileKind) -> str:
    """The file's name as the owner sees it (his own, else a default per kind), one line, at most 120 characters."""
    name = " ".join((attachment.file_name or "").split())[:120]
    return name or DEFAULT_NAMES[kind]


# --- limits ---------------------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class FileLimits:
    max_bytes: int
    text_max_chars: int
    pdf_max_pages: int
    max_pdfs: int
    audio_max_s: int
    audio_kbps: int
    audio_budget_s: float
    download_s: float

    @property
    def max_mb(self) -> int:
        return self.max_bytes // MIB


def file_limits(snap: Snapshot) -> FileLimits:
    return FileLimits(
        max_bytes=snap[defaults.FILES_MAX_MB] * MIB,
        text_max_chars=snap[defaults.FILES_TEXT_MAX_CHARS],
        pdf_max_pages=snap[defaults.FILES_PDF_MAX_PAGES],
        max_pdfs=snap[defaults.FILES_MAX_PDFS],
        audio_max_s=snap[defaults.FILES_AUDIO_MAX_MIN] * 60,
        audio_kbps=snap[defaults.FILES_AUDIO_KBPS],
        audio_budget_s=snap[defaults.TIMEOUTS_AUDIO_FILE_S],
        download_s=snap[defaults.TIMEOUTS_MEDIA_S],
    )


@dataclass(frozen=True, slots=True)
class DocumentLimits:
    """This answer's `files.max_documents`, `files.document_max_mb` and `timeouts.document_s`."""

    max_documents: int
    max_bytes: int
    timeout_s: float


def document_limits(snap: Snapshot) -> DocumentLimits:
    return DocumentLimits(
        max_documents=snap[defaults.FILES_MAX_DOCUMENTS],
        max_bytes=snap[defaults.FILES_DOCUMENT_MAX_MB] * MIB,
        timeout_s=snap[defaults.TIMEOUTS_DOCUMENT_S],
    )


def check_file(attachment: Attachment, kind: FileKind, limits: FileLimits) -> None:
    """Refuse before downloading: over the size cap (as UC-31 F), or an audio file longer than `files.audio_max_min`.
    Unknown sizes and durations pass; the download itself is capped."""
    if attachment.size_bytes is not None and attachment.size_bytes > limits.max_bytes:
        raise MediaTooLarge(limit_mb=limits.max_mb)
    if kind is FileKind.AUDIO and attachment.duration_s is not None and attachment.duration_s > limits.audio_max_s:
        raise FileTooLong(max_minutes=limits.audio_max_s // 60)


def saved_name(kind: FileKind, attachment: Attachment) -> str:
    """The cached file's name: a fixed stem and the sender's suffix if it is one this kind knows, never his name."""
    suffix = _suffix(attachment.file_name)
    if kind is FileKind.AUDIO:
        return f"audio{suffix if suffix in AUDIO_SUFFIXES else '.bin'}"
    if kind is FileKind.PDF:
        return "document.pdf"
    return f"file{suffix if re.fullmatch(r'\.[a-z0-9]{1,10}', suffix) else '.bin'}"


# --- text -----------------------------------------------------------------------------------------------------------
def decode_text(data: bytes) -> str | None:
    """A text file's text: UTF-8 (BOM or not), UTF-16 by its BOM, else cp1251 (Cyrillic files from Windows). None
    when the bytes look binary (a NUL early on, outside UTF-16)."""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace")
    if b"\x00" in data[:NUL_PROBE_BYTES]:
        return None
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1251", errors="replace")


_W: Final = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_S: Final = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_A: Final = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_R: Final = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_REL: Final = "{http://schemas.openxmlformats.org/package/2006/relationships}"


def _xml(archive: zipfile.ZipFile, name: str) -> ET.Element | None:
    try:
        info = archive.getinfo(name)
    except KeyError:
        return None
    if info.file_size > XML_PART_MAX_BYTES:
        raise FileUnreadable
    return ET.fromstring(archive.read(info))  # noqa: S314  # owner's own file; expat refuses entity blow-ups


def _open_zip(data: bytes) -> zipfile.ZipFile:
    try:
        return zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, ValueError) as exc:
        raise FileUnreadable from exc


def _word_paragraph(paragraph: ET.Element) -> str:
    parts: list[str] = []
    for node in paragraph.iter():
        if node.tag == f"{_W}t" and node.text:
            parts.append(node.text)
        elif node.tag == f"{_W}tab":
            parts.append("\t")
        elif node.tag in {f"{_W}br", f"{_W}cr"}:
            parts.append("\n")
    return "".join(parts).strip()


def docx_text(data: bytes) -> str:
    """A Word file's body in reading order: paragraphs, and each table row as `cell | cell`."""
    with _open_zip(data) as archive:
        try:
            root = _xml(archive, "word/document.xml")
        except ET.ParseError as exc:
            raise FileUnreadable from exc
    if root is None:
        raise FileUnreadable
    body = root.find(f"{_W}body")
    lines: list[str] = []
    for child in body if body is not None else ():
        if child.tag == f"{_W}p":
            if text := _word_paragraph(child):
                lines.append(text)
        elif child.tag == f"{_W}tbl":
            for row in child.iter(f"{_W}tr"):
                cells = [
                    " ".join(_word_paragraph(p) for p in cell.iter(f"{_W}p")).strip() for cell in row.iter(f"{_W}tc")
                ]
                if any(cells):
                    lines.append(" | ".join(cells))
    return "\n".join(lines)


def _relationships(archive: zipfile.ZipFile, name: str) -> dict[str, str]:
    root = _xml(archive, name)
    if root is None:
        return {}
    return {rel.get("Id", ""): rel.get("Target", "") for rel in root.iter(f"{_REL}Relationship")}


def _part_path(base: str, target: str) -> str:
    """A relationship target as a zip member name (`worksheets/sheet1.xml` from `xl/` → `xl/worksheets/sheet1.xml`)."""
    if target.startswith("/"):
        return target.lstrip("/")
    parts: list[str] = [segment for segment in base.split("/") if segment]
    for segment in target.split("/"):
        if segment == "..":
            if parts:
                parts.pop()
        elif segment and segment != ".":
            parts.append(segment)
    return "/".join(parts)


def _shared_strings(archive: zipfile.ZipFile) -> list[str]:
    root = _xml(archive, "xl/sharedStrings.xml")
    if root is None:
        return []
    return ["".join(node.text or "" for node in item.iter(f"{_S}t")) for item in root.iter(f"{_S}si")]


def _cell_value(cell: ET.Element, shared: Sequence[str]) -> str:
    kind = cell.get("t", "")
    if kind == "inlineStr":
        return "".join(node.text or "" for node in cell.iter(f"{_S}t"))
    value = cell.findtext(f"{_S}v") or ""
    if kind == "s":
        try:
            return shared[int(value)]
        except (ValueError, IndexError):
            return ""
    if kind == "b":
        return "TRUE" if value == "1" else "FALSE"
    return value


def xlsx_text(data: bytes) -> str:
    """Every sheet of a workbook, headed by its name, each row's cells tab-separated (formulas as their last value)."""
    with _open_zip(data) as archive:
        try:
            workbook = _xml(archive, "xl/workbook.xml")
            if workbook is None:
                raise FileUnreadable
            targets = _relationships(archive, "xl/_rels/workbook.xml.rels")
            shared = _shared_strings(archive)
            blocks: list[str] = []
            for sheet in workbook.iter(f"{_S}sheet"):
                name = sheet.get("name", "")
                target = targets.get(sheet.get(f"{_R}id", ""), "")
                root = _xml(archive, _part_path("xl", target)) if target else None
                if root is None:
                    continue
                rows: list[str] = []
                for row in root.iter(f"{_S}row"):
                    cells = [_cell_value(cell, shared) for cell in row.iter(f"{_S}c")]
                    if any(cells):
                        rows.append("\t".join(cells).rstrip("\t"))
                    if len(rows) >= SHEET_ROWS_MAX:
                        break
                blocks.append(f"## {name}\n" + "\n".join(rows))
        except ET.ParseError as exc:
            raise FileUnreadable from exc
    return "\n\n".join(blocks)


def _slide_number(name: str) -> int:
    found = re.search(r"(\d+)\.xml$", name)
    return int(found.group(1)) if found else 0


def pptx_text(data: bytes) -> str:
    """Every slide's text in slide order (`Slide N:`, then its paragraphs), notes left out."""
    with _open_zip(data) as archive:
        names = sorted(
            (name for name in archive.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)),
            key=_slide_number,
        )[:SLIDES_MAX]
        slides: list[str] = []
        try:
            for index, name in enumerate(names, start=1):
                root = _xml(archive, name)
                if root is None:
                    continue
                paragraphs = [
                    "".join(node.text or "" for node in paragraph.iter(f"{_A}t")).strip()
                    for paragraph in root.iter(f"{_A}p")
                ]
                text = "\n".join(line for line in paragraphs if line)
                slides.append(f"Slide {index}:\n{text}" if text else f"Slide {index}: (no text)")
        except ET.ParseError as exc:
            raise FileUnreadable from exc
    return "\n\n".join(slides)


def text_of(kind: FileKind, data: bytes) -> str | None:
    """The text of a `TEXT_KINDS` file; None when a text file looks binary. Raises `FileUnreadable` for a damaged
    Office file."""
    if kind is FileKind.WORD:
        return docx_text(data)
    if kind is FileKind.SHEET:
        return xlsx_text(data)
    if kind is FileKind.SLIDES:
        return pptx_text(data)
    if kind is FileKind.TEXT:
        return decode_text(data)
    msg = f"{kind} has no text reader"
    raise ValueError(msg)


def cut(text: str, max_chars: int) -> tuple[str, bool]:
    """`text` within `max_chars` (at a line end when one is near), and whether it was cut."""
    if len(text) <= max_chars:
        return text, False
    head = text[:max_chars]
    line_end = head.rfind("\n")
    return (head[:line_end] if line_end > max_chars * 0.9 else head), True


# --- PDF ------------------------------------------------------------------------------------------------------------
_PAGES_COUNT: Final = re.compile(rb"/Type\s*/Pages\b[^>]*?/Count\s+(\d+)|/Count\s+(\d+)[^>]*?/Type\s*/Pages\b", re.S)
_PAGE: Final = re.compile(rb"/Type\s*/Page\b(?!s)")


def pdf_pages(data: bytes) -> int | None:
    """The PDF's page count from its page tree (the largest `/Count` of a `/Pages` node), else its `/Page` objects;
    None when neither can be read (compressed object streams hide both): such a file passes."""
    counts = [int(found.group(1) or found.group(2)) for found in _PAGES_COUNT.finditer(data)]
    if counts:
        return max(counts)
    pages = len(_PAGE.findall(data))
    return pages or None


# --- material -------------------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class MaterialParts:
    """The slots of `prompts/personal/files_material.md`, each already defused."""

    tag: str
    kind: str
    name: str
    body: str = field(repr=False)
    truncated: str


def material_parts(*, kind: FileKind, name: str, body: str, truncated: bool, nonce: str) -> MaterialParts:
    return MaterialParts(
        tag=media.boundary(nonce),
        kind=KIND_LABELS[kind],
        name=media.defuse(name) or "-",
        body=media.defuse(body),
        truncated="yes" if truncated else "no",
    )


def transcript_file_name(name: str) -> str:
    """`transkript-<the recording's name without its suffix>.txt`, cleaned like a document's name."""
    stem = PurePosixPath(name).stem if PurePosixPath(name).suffix.lower() in AUDIO_SUFFIXES else name
    return f"transkript-{clean_filename(stem, default='yozuv')}.txt"


# --- audio ----------------------------------------------------------------------------------------------------------
def usage_of(analysis: MediaAnalysis) -> Usage:
    """The `usage_records` row of one transcribed audio file (as `media.domain.usage_of`, its own source)."""
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


def audio_failure_text(attempts: Sequence[media.Attempt], params: Mapping[str, str | int]) -> BotText:
    """The owner's message when no reader transcribed the recording: per provider what went wrong, or a content
    problem's own message (too large, no audio in it)."""
    if not attempts:
        msg = "a failure needs at least one attempt"
        raise ValueError(msg)
    last = attempts[-1]
    if last.code not in media.PROVIDER_CODES:
        return S.ERRORS[last.code](**params)
    reasons = (
        S.STT_REASON_PROVIDER(provider=voice.provider_label(attempt.provider), problem=voice.problem_text(attempt.code))
        for attempt in attempts
    )
    return S.FILE_AUDIO_FAILED(reason=join(reasons, "; "))


def languages_text(tags: Sequence[str]) -> str:
    """`stt.languages` for the transcription instruction: `uz-UZ, ru-RU, en-US`."""
    return ", ".join(tags) or "uz-UZ"


# --- documents: the tool's arguments ---------------------------------------------------------------------------------
_FILENAME_DROP: Final = re.compile(r"[^\w\s-]", re.UNICODE)
_SHEET_NAME_DROP: Final = re.compile(r"[\[\]:*?/\\']")


def clean_filename(raw: str, *, default: str = DEFAULT_FILENAME) -> str:
    """Letters, digits, spaces, `-` and `_` only; no extension, no path; at most `FILENAME_MAX_CHARS`."""
    stem = raw.strip()
    suffix = PurePosixPath(stem).suffix.lower()
    if suffix.lstrip(".") in {item.value for item in DocFormat}:
        stem = stem[: -len(suffix)]
    cleaned = " ".join(_FILENAME_DROP.sub("", stem.replace("/", " ").replace("\\", " ")).split())
    cleaned = cleaned.strip(" -_.")[:FILENAME_MAX_CHARS].strip()
    return cleaned or default


def _string(arguments: Mapping[str, JsonValue], name: str, *, limit: int) -> str:
    value = arguments.get(name)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise DocumentInvalid(problem=name)
    if len(value) > limit:
        raise DocumentInvalid(problem="too_large")
    return value


def _cell(value: JsonValue) -> str | float:
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, int | float):
        return float(value)
    if value is None:
        return ""
    if isinstance(value, str):
        return value[:CELL_MAX_CHARS]
    raise DocumentInvalid(problem="sheets")


def sheet_name(raw: str, index: int, taken: set[str]) -> str:
    """An Excel-valid, unique sheet name (≤ 31 characters, none of `[]:*?/\\'`)."""
    base = " ".join(_SHEET_NAME_DROP.sub("", raw).split())[:SHEET_NAME_MAX_CHARS] or f"Sheet{index}"
    name, counter = base, 2
    while name.lower() in taken:
        tail = f" ({counter})"
        name = base[: SHEET_NAME_MAX_CHARS - len(tail)] + tail
        counter += 1
    taken.add(name.lower())
    return name


def sheets_of(raw: JsonValue, *, max_rows: int = SHEET_ROWS_MAX) -> tuple[Sheet, ...]:
    """`[{"name": …, "rows": [[cell, …], …]}, …]` as sheets; anything else is `DocumentInvalid("sheets")`."""
    if not isinstance(raw, list) or not raw or len(raw) > SHEETS_MAX:
        raise DocumentInvalid(problem="sheets")
    sheets: list[Sheet] = []
    taken: set[str] = set()
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise DocumentInvalid(problem="sheets")
        rows = item.get("rows")
        if not isinstance(rows, list) or len(rows) > max_rows:
            raise DocumentInvalid(problem="sheets")
        cells: list[tuple[str | float, ...]] = []
        for row in rows:
            if not isinstance(row, list) or len(row) > SHEET_COLUMNS_MAX:
                raise DocumentInvalid(problem="sheets")
            cells.append(tuple(_cell(value) for value in row))
        name = item.get("name")
        sheets.append(Sheet(name=sheet_name(name if isinstance(name, str) else "", index, taken), rows=tuple(cells)))
    return tuple(sheets)


_NUMBER: Final = re.compile(r"-?(?:0|[1-9]\d{0,14})(?:\.\d+)?")


def _table_cell(runs: Runs) -> str | float:
    text = "".join(run.text for run in runs).strip()
    return float(text) if _NUMBER.fullmatch(text) else text


def tables_as_sheets(blocks: Sequence[Block], title: str) -> tuple[Sheet, ...]:
    """Each Markdown table as one sheet (named after the heading above it, else the title), numbers as numbers."""
    sheets: list[Sheet] = []
    taken: set[str] = set()
    heading = title
    for block in blocks:
        if isinstance(block, Heading):
            heading = "".join(run.text for run in block.runs)
        elif isinstance(block, Table):
            rows = tuple(tuple(_table_cell(cell) for cell in row) for row in block.rows)
            sheets.append(Sheet(name=sheet_name(heading, len(sheets) + 1, taken), rows=rows))
    return tuple(sheets)


def document_of(arguments: Mapping[str, JsonValue]) -> DocumentSpec:
    """The `make_document` arguments as a spec (SPEC §3.2.17 step 4). Raises `DocumentInvalid(problem)`: `format`,
    `content` (missing for a text format), `sheets` (malformed, or an XLSX with neither sheets nor tables), or
    `too_large`."""
    raw_format = arguments.get("format")
    try:
        fmt = DocFormat(raw_format.strip().lower() if isinstance(raw_format, str) else "")
    except ValueError as exc:
        raise DocumentInvalid(problem="format") from exc
    filename = clean_filename(_string(arguments, "filename", limit=500))
    title = " ".join(_string(arguments, "title", limit=TITLE_MAX_CHARS).split())
    content = _string(arguments, "content", limit=CONTENT_MAX_CHARS).replace("\r\n", "\n")
    if fmt is DocFormat.XLSX:
        if arguments.get("sheets") is not None:
            sheets = sheets_of(arguments.get("sheets"))
        else:
            sheets = tables_as_sheets(parse_markdown(content), title)
        if not sheets:
            raise DocumentInvalid(problem="sheets")
        return DocumentSpec(format=fmt, filename=filename, title=title, sheets=sheets)
    if not content.strip():
        raise DocumentInvalid(problem="content")
    if fmt is DocFormat.MD:
        source = f"# {title}\n\n{content}" if title and not content.lstrip().startswith("#") else content
        return DocumentSpec(format=fmt, filename=filename, title=title, source=source.strip() + "\n")
    if fmt is DocFormat.TXT:
        source = f"{title}\n\n{content}" if title else content
        return DocumentSpec(format=fmt, filename=filename, title=title, source=source.strip() + "\n")
    return DocumentSpec(format=fmt, filename=filename, title=title, blocks=parse_markdown(content))


# --- documents: Markdown → blocks ------------------------------------------------------------------------------------
_HEADING: Final = re.compile(r"(#{1,6})\s+(.*?)\s*#*\s*")
_RULE: Final = re.compile(r"\s*(?:-{3,}|\*{3,}|_{3,})\s*")
_LIST: Final = re.compile(r"(\s*)([-*+•]|\d{1,4}[.)])\s+(.*)")
_FENCE: Final = re.compile(r"\s*(```|~~~)")
_TABLE_SEPARATOR: Final = re.compile(r"\s*\|?\s*:?-+:?\s*(?:\|\s*:?-+:?\s*)*\|?\s*")
_INLINE: Final = re.compile(
    r"\*\*(?P<b1>.+?)\*\*|__(?P<b2>.+?)__|`(?P<code>[^`]+)`|(?<![\w*])\*(?P<i1>[^*\s](?:[^*]*?[^*\s])?)\*(?![\w*])"
    r"|(?<!\w)_(?P<i2>[^_\s](?:[^_]*?[^_\s])?)_(?!\w)"
)


def inline_runs(text: str) -> Runs:
    """`**bold**`, `__bold__`, `*italic*`, `_italic_` and `` `code` `` as styled runs; everything else is plain."""
    runs: list[Run] = []
    position = 0
    for found in _INLINE.finditer(text):
        if found.start() > position:
            runs.append(Run(text[position : found.start()]))
        if (bold := found.group("b1") or found.group("b2")) is not None:
            runs.extend(Run(run.text, bold=True, italic=run.italic, code=run.code) for run in inline_runs(bold))
        elif (code := found.group("code")) is not None:
            runs.append(Run(code, code=True))
        else:
            italic = found.group("i1") or found.group("i2") or ""
            runs.extend(Run(run.text, bold=run.bold, italic=True, code=run.code) for run in inline_runs(italic))
        position = found.end()
    if position < len(text):
        runs.append(Run(text[position:]))
    return tuple(run for run in runs if run.text)


def _table_row(line: str) -> tuple[Runs, ...]:
    inner = line.strip()
    inner = inner.removeprefix("|").removesuffix("|")
    return tuple(inline_runs(cell.strip()) for cell in re.split(r"(?<!\\)\|", inner))


def _is_table_start(lines: Sequence[str], index: int) -> bool:
    return (
        "|" in lines[index]
        and index + 1 < len(lines)
        and "-" in lines[index + 1]
        and _TABLE_SEPARATOR.fullmatch(lines[index + 1]) is not None
    )


def parse_markdown(text: str) -> tuple[Block, ...]:
    """The Markdown subset the tool takes, as blocks: headings (`#`-`######`, deeper than 4 as 4), rules, fenced code,
    pipe tables (header, separator, rows), `-`/`*`/`1.` lists (two spaces per level, at most 3 deep) and paragraphs
    (consecutive lines joined)."""
    lines = text.replace("\r\n", "\n").split("\n")
    blocks: list[Block] = []
    paragraph: list[str] = []

    def flush() -> None:
        if paragraph:
            blocks.append(Paragraph(inline_runs(" ".join(line.strip() for line in paragraph))))
            paragraph.clear()

    index = 0
    while index < len(lines):
        line = lines[index]
        if fence := _FENCE.match(line):
            flush()
            code: list[str] = []
            index += 1
            while index < len(lines) and not lines[index].strip().startswith(fence.group(1)):
                code.append(lines[index])
                index += 1
            blocks.append(CodeBlock("\n".join(code)))
            index += 1
            continue
        if not line.strip():
            flush()
        elif heading := _HEADING.fullmatch(line.strip()):
            flush()
            blocks.append(Heading(level=min(len(heading.group(1)), 4), runs=inline_runs(heading.group(2))))
        elif _RULE.fullmatch(line):
            flush()
            blocks.append(Rule())
        elif _is_table_start(lines, index):
            flush()
            rows = [_table_row(line)]
            index += 2
            while index < len(lines) and "|" in lines[index] and lines[index].strip():
                rows.append(_table_row(lines[index]))
                index += 1
            width = max(len(row) for row in rows)
            blocks.append(Table(tuple(row + ((),) * (width - len(row)) for row in rows)))
            continue
        elif item := _LIST.fullmatch(line):
            flush()
            marker = item.group(2)
            ordered = marker[0].isdigit()
            depth = min(len(item.group(1).replace("\t", "  ")) // 2, LIST_DEPTH_MAX - 1)
            blocks.append(
                ListItem(
                    runs=inline_runs(item.group(3)),
                    ordered=ordered,
                    number=int(marker[:-1]) if ordered else 1,
                    depth=depth,
                )
            )
        else:
            paragraph.append(line)
        index += 1
    flush()
    return tuple(blocks)
