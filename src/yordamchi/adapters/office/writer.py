"""`OfficeDocumentWriter`: the `DocumentWriter` port (UC-37; SPEC §3.2.17) — DOCX via python-docx 1.2.0, XLSX via
openpyxl 3.1.5, PDF as HTML printed by Chromium (`adapters/web/browser.print_pdf`, injected at wiring), MD and TXT as
UTF-8 text.

The spec's blocks are laid out, never interpreted: every text is a run with a style, HTML-escaped for the PDF, and a
spreadsheet string that starts with `=` stays a string (no formula is ever written from model output). Characters XML
cannot hold (control characters) are dropped. The library work runs in a worker thread; the whole write is bounded by
the caller's `timeout`.

Failure mapping: any library error → `DocumentFailed(format)`; the clock → `CallTimedOut("document")`. Logs carry the
format, the size and the duration, never the file name or content.
"""

import asyncio
import contextlib
import html
import re
import time
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Final

import docx
from docx.document import Document as DocxDocument
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt
from docx.text.paragraph import Paragraph as DocxParagraph
from openpyxl import Workbook
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter

from yordamchi.core.errors import CallTimedOut, DocumentFailed, YordamchiError
from yordamchi.core.log import get_log
from yordamchi.ports.documents import (
    Block,
    CodeBlock,
    DocFormat,
    DocumentSpec,
    Heading,
    ListItem,
    Paragraph,
    Rule,
    Runs,
    Sheet,
    Table,
    WrittenDocument,
)

log = get_log(__name__)

OPERATION: Final = "document"
MIMES: Final = {
    DocFormat.DOCX: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    DocFormat.XLSX: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    DocFormat.PDF: "application/pdf",
    DocFormat.MD: "text/markdown",
    DocFormat.TXT: "text/plain",
}
CODE_FONT: Final = "Courier New"
CODE_SIZE_PT: Final = 9.5
LIST_INDENT_PT: Final = 18
COLUMN_WIDTH_MAX: Final = 60
# XML 1.0 cannot hold C0 controls other than tab, newline and carriage return (lxml and openpyxl refuse them).
_NOT_XML: Final = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")

type PdfPrinter = Callable[..., Awaitable[None]]  # print_pdf(html, out, *, timeout)


def xml_safe(text: str) -> str:
    return _NOT_XML.sub("", text)


# --- DOCX ------------------------------------------------------------------------------------------------------------
def _add_runs(paragraph: DocxParagraph, runs: Runs) -> None:
    for item in runs:
        run = paragraph.add_run(xml_safe(item.text))
        run.bold = item.bold or None
        run.italic = item.italic or None
        if item.code:
            run.font.name = CODE_FONT


def _rule(paragraph: DocxParagraph) -> None:
    """A bottom border on an empty paragraph: Word's horizontal line."""
    borders = OxmlElement("w:pBdr")
    bottom = OxmlElement("w:bottom")
    for name, value in (("w:val", "single"), ("w:sz", "6"), ("w:space", "1"), ("w:color", "999999")):
        bottom.set(qn(name), value)
    borders.append(bottom)
    paragraph._p.get_or_add_pPr().append(borders)  # python-docx has no API for paragraph borders


def _table(document: DocxDocument, table: Table) -> None:
    width = max((len(row) for row in table.rows), default=0)
    if not width:
        return
    grid = document.add_table(rows=len(table.rows), cols=width)
    grid.style = "Table Grid"
    for row_index, row in enumerate(table.rows):
        for column, runs in enumerate(row):
            paragraph = grid.cell(row_index, column).paragraphs[0]
            _add_runs(paragraph, runs)
            if row_index == 0:
                for run in paragraph.runs:
                    run.bold = True
    document.add_paragraph()


def _block(document: DocxDocument, block: Block) -> None:
    if isinstance(block, Heading):
        _add_runs(document.add_heading("", level=block.level), block.runs)
    elif isinstance(block, Paragraph):
        _add_runs(document.add_paragraph(), block.runs)
    elif isinstance(block, ListItem):
        paragraph = document.add_paragraph()
        paragraph.paragraph_format.left_indent = Pt(LIST_INDENT_PT * (block.depth + 1))
        paragraph.paragraph_format.first_line_indent = Pt(-LIST_INDENT_PT * 0.75)
        paragraph.add_run(f"{block.number}.\t" if block.ordered else "•\t")
        _add_runs(paragraph, block.runs)
    elif isinstance(block, Table):
        _table(document, block)
    elif isinstance(block, CodeBlock):
        paragraph = document.add_paragraph()
        for index, line in enumerate(xml_safe(block.text).split("\n")):
            run = paragraph.add_run(line)
            run.font.name = CODE_FONT
            run.font.size = Pt(CODE_SIZE_PT)
            if index:
                run.add_break()
    elif isinstance(block, Rule):
        _rule(document.add_paragraph())


def write_docx(spec: DocumentSpec, out: Path) -> None:
    document = docx.Document()
    if spec.title:
        document.add_heading(xml_safe(spec.title), level=0)
    for block in spec.blocks:
        _block(document, block)
    document.save(str(out))


# --- XLSX ------------------------------------------------------------------------------------------------------------
def _width(values: Sequence[str | float]) -> float:
    longest = max((len(str(value)) for value in values), default=0)
    return min(COLUMN_WIDTH_MAX, max(8, longest + 2))


def write_xlsx(sheets: Sequence[Sheet], out: Path) -> None:
    workbook = Workbook()
    if workbook.active is not None:
        workbook.remove(workbook.active)
    for sheet in sheets:
        worksheet = workbook.create_sheet(title=sheet.name)
        for row in sheet.rows:
            worksheet.append([xml_safe(value) if isinstance(value, str) else value for value in row])
        for cells in worksheet.iter_rows():
            for cell in cells:
                if isinstance(cell.value, str) and cell.value.startswith("="):
                    cell.data_type = "s"  # model output is text, never a formula
        if sheet.rows:
            for cell in worksheet[1]:
                cell.font = Font(bold=True)
            worksheet.freeze_panes = "A2"
        width = max((len(row) for row in sheet.rows), default=0)
        for column in range(width):
            values = [row[column] for row in sheet.rows if column < len(row)]
            worksheet.column_dimensions[get_column_letter(column + 1)].width = _width(values)
    workbook.save(str(out))


# --- PDF (HTML) ------------------------------------------------------------------------------------------------------
PDF_STYLE: Final = """
body { font-family: -apple-system, "Helvetica Neue", Arial, sans-serif; font-size: 11pt; line-height: 1.45;
       color: #111; }
h1.title { font-size: 20pt; margin: 0 0 12pt; }
h1 { font-size: 16pt; margin: 16pt 0 6pt; } h2 { font-size: 13.5pt; margin: 14pt 0 5pt; }
h3, h4 { font-size: 12pt; margin: 12pt 0 4pt; }
p { margin: 0 0 7pt; } p.li { margin: 0 0 3pt; display: flex; } p.li span.mark { min-width: 1.6em; }
table { border-collapse: collapse; width: 100%; margin: 6pt 0 10pt; font-size: 10pt; }
th, td { border: 1px solid #999; padding: 4pt 6pt; text-align: left; vertical-align: top; }
th { background: #eee; }
pre { font-family: Menlo, "Courier New", monospace; font-size: 9pt; background: #f4f4f4; padding: 6pt;
      white-space: pre-wrap; }
code { font-family: Menlo, "Courier New", monospace; font-size: 9.5pt; }
hr { border: 0; border-top: 1px solid #999; margin: 10pt 0; }
"""


def _runs_html(runs: Runs) -> str:
    parts: list[str] = []
    for run in runs:
        text = html.escape(xml_safe(run.text))
        if run.code:
            text = f"<code>{text}</code>"
        if run.italic:
            text = f"<em>{text}</em>"
        if run.bold:
            text = f"<strong>{text}</strong>"
        parts.append(text)
    return "".join(parts)


def _block_html(block: Block) -> str:
    if isinstance(block, Heading):
        return f"<h{block.level}>{_runs_html(block.runs)}</h{block.level}>"
    if isinstance(block, Paragraph):
        return f"<p>{_runs_html(block.runs)}</p>"
    if isinstance(block, ListItem):
        mark = f"{block.number}." if block.ordered else "•"
        indent = 1.2 * block.depth
        return f'<p class="li" style="margin-left:{indent}em"><span class="mark">{mark}</span><span>{_runs_html(block.runs)}</span></p>'  # noqa: E501
    if isinstance(block, Table):
        rows = []
        for index, row in enumerate(block.rows):
            cell = "th" if index == 0 else "td"
            rows.append("<tr>" + "".join(f"<{cell}>{_runs_html(runs)}</{cell}>" for runs in row) + "</tr>")
        return "<table>" + "".join(rows) + "</table>"
    if isinstance(block, CodeBlock):
        return f"<pre>{html.escape(xml_safe(block.text))}</pre>"
    return "<hr>"


def document_html(spec: DocumentSpec) -> str:
    """The spec as one self-contained HTML page: inline CSS, no script, no link, no image; every text escaped."""
    title = html.escape(xml_safe(spec.title))
    body = "".join(_block_html(block) for block in spec.blocks)
    heading = f'<h1 class="title">{title}</h1>' if title else ""
    return (
        f'<!doctype html><html><head><meta charset="utf-8"><title>{title}</title><style>{PDF_STYLE}</style></head>'
        f"<body>{heading}{body}</body></html>"
    )


# --- the writer ------------------------------------------------------------------------------------------------------
def _write_text(text: str, out: Path) -> None:
    out.write_text(text, encoding="utf-8")


class OfficeDocumentWriter:
    """`print_pdf` is `adapters/web/browser.print_pdf` (Playwright stays confined to `adapters/web`, C5); None leaves
    PDF unavailable (`DocumentFailed("pdf")`)."""

    def __init__(self, *, print_pdf: PdfPrinter | None) -> None:
        self._print_pdf = print_pdf

    async def write(self, spec: DocumentSpec, *, dest_dir: Path, timeout: float) -> WrittenDocument:
        started = time.monotonic()
        out = dest_dir / spec.file_name
        try:
            async with asyncio.timeout(timeout):
                await self._write(spec, out, timeout=timeout)
            out.chmod(0o600)
            size = out.stat().st_size
        except TimeoutError:
            self._discard(out)
            log.warning("document_write_failed", format=spec.format.value, code="call_timed_out")
            raise CallTimedOut(operation=OPERATION) from None
        except YordamchiError as error:
            self._discard(out)
            log.warning("document_write_failed", format=spec.format.value, code=error.code.value)
            raise
        except Exception as exc:  # any library refusal is one outcome for the caller
            self._discard(out)
            log.warning("document_write_failed", format=spec.format.value, code="document_failed")
            raise DocumentFailed(format=spec.format.value) from exc
        log.info(
            "document_written",
            format=spec.format.value,
            size_bytes=size,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        return WrittenDocument(path=out, mime=MIMES[spec.format], size_bytes=size)

    async def _write(self, spec: DocumentSpec, out: Path, *, timeout: float) -> None:
        if spec.format is DocFormat.DOCX:
            await asyncio.to_thread(write_docx, spec, out)
        elif spec.format is DocFormat.XLSX:
            await asyncio.to_thread(write_xlsx, spec.sheets, out)
        elif spec.format is DocFormat.PDF:
            if self._print_pdf is None:
                raise DocumentFailed(format=spec.format.value)
            await self._print_pdf(document_html(spec), out, timeout=timeout)
        else:
            await asyncio.to_thread(_write_text, spec.source, out)

    @staticmethod
    def _discard(out: Path) -> None:
        with contextlib.suppress(OSError):
            out.unlink(missing_ok=True)
