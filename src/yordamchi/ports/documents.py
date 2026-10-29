"""DocumentWriter: the documents the personal assistant makes for the owner (UC-37; SPEC §3.2.17).

`personal/files` parses the assistant's `make_document` arguments (Markdown, or rows for a spreadsheet) into a
`DocumentSpec`: a format-free list of blocks. The adapter only lays those blocks out in the file format; it decides
nothing about content. Everything in a spec is model output: text to lay out, never markup the writer interprets.
"""

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Protocol


class DocFormat(StrEnum):
    DOCX = "docx"
    PDF = "pdf"
    XLSX = "xlsx"
    MD = "md"
    TXT = "txt"


@dataclass(frozen=True, slots=True)
class Run:
    """A stretch of text with one style."""

    text: str
    bold: bool = False
    italic: bool = False
    code: bool = False


type Runs = tuple[Run, ...]


@dataclass(frozen=True, slots=True)
class Heading:
    level: int  # 1-4
    runs: Runs


@dataclass(frozen=True, slots=True)
class Paragraph:
    runs: Runs


@dataclass(frozen=True, slots=True)
class ListItem:
    runs: Runs
    ordered: bool = False
    number: int = 1  # an ordered item's own number
    depth: int = 0  # nesting, 0 = top level


@dataclass(frozen=True, slots=True)
class Table:
    """The first row is the header."""

    rows: tuple[tuple[Runs, ...], ...]


@dataclass(frozen=True, slots=True)
class CodeBlock:
    text: str


@dataclass(frozen=True, slots=True)
class Rule:
    """A horizontal line (Markdown `---`)."""


type Block = Heading | Paragraph | ListItem | Table | CodeBlock | Rule


@dataclass(frozen=True, slots=True)
class Sheet:
    name: str
    rows: tuple[tuple[str | float, ...], ...]  # the first row is the header; numbers stay numbers


@dataclass(frozen=True, slots=True)
class DocumentSpec:
    """One document. `blocks` lay out DOCX and PDF; `sheets` make an XLSX; `source` is the text written as it is for
    MD and TXT. `filename` has no extension and is already clean (letters, digits, spaces, `-`, `_`)."""

    format: DocFormat
    filename: str
    title: str = field(default="", repr=False)
    blocks: tuple[Block, ...] = field(default=(), repr=False)
    sheets: tuple[Sheet, ...] = field(default=(), repr=False)
    source: str = field(default="", repr=False)

    @property
    def file_name(self) -> str:
        return f"{self.filename}.{self.format.value}"


@dataclass(frozen=True, slots=True)
class WrittenDocument:
    path: Path
    mime: str
    size_bytes: int


class DocumentWriter(Protocol):
    async def write(self, spec: DocumentSpec, *, dest_dir: Path, timeout: float) -> WrittenDocument:
        """Write `spec` as `dest_dir / spec.file_name` (0600) within `timeout`. A library or printer failure raises
        `DocumentFailed`; a timeout raises `CallTimedOut`."""
        ...
