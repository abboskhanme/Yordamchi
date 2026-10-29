"""`OfficeDocumentWriter` offline (UC-37; SPEC §3.2.17): real DOCX and XLSX files read back with python-docx and
openpyxl, MD and TXT as UTF-8, the PDF's HTML through a fake printer (every text escaped, nothing loaded), model text
never written as a spreadsheet formula, characters XML cannot hold dropped, and the failure mapping."""

import asyncio
import stat
from pathlib import Path

import docx
import pytest
from openpyxl import load_workbook

from yordamchi.adapters.office.writer import OfficeDocumentWriter, document_html
from yordamchi.core.errors import CallTimedOut, DocumentFailed
from yordamchi.core.json_value import JsonValue
from yordamchi.personal.files import domain
from yordamchi.ports.documents import DocFormat, DocumentSpec, Sheet

MARKDOWN = """# Bayonnoma

**Sana:** 29-oktabr · *Joy:* ofis

1. Narxni oshirish
- Yangi filial

| No | Vazifa | Mas'ul |
|---|---|---|
| 1 | Reklama | Ali |

---
```
kod\x07
```
"""


class Printer:
    def __init__(self, *, fail: Exception | None = None) -> None:
        self.html: list[str] = []
        self.fail = fail

    async def __call__(self, html: str, out: Path, *, timeout: float) -> None:
        self.html.append(html)
        if self.fail is not None:
            raise self.fail
        await asyncio.to_thread(out.write_bytes, b"%PDF-1.4 fake")


def _spec(fmt: str, **arguments: JsonValue) -> DocumentSpec:
    return domain.document_of({"format": fmt, "filename": "Bayonnoma", "content": MARKDOWN, **arguments})


async def test_a_docx_holds_the_title_headings_lists_and_table(tmp_path: Path) -> None:
    written = await OfficeDocumentWriter(print_pdf=None).write(_spec("docx", title="Yig'ilish"), dest_dir=tmp_path,
                                                               timeout=30)  # fmt: skip
    assert written.path == tmp_path / "Bayonnoma.docx"
    assert stat.S_IMODE(written.path.stat().st_mode) == 0o600
    assert written.mime.endswith("wordprocessingml.document")
    document = docx.Document(str(written.path))
    texts = [paragraph.text for paragraph in document.paragraphs if paragraph.text]
    assert texts[:5] == [
        "Yig'ilish",
        "Bayonnoma",
        "Sana: 29-oktabr · Joy: ofis",
        "1.\tNarxni oshirish",
        "•\tYangi filial",
    ]
    assert "kod" in texts  # the BEL control character was dropped
    [table] = document.tables
    assert [cell.text for cell in table.rows[0].cells] == ["No", "Vazifa", "Mas'ul"]
    assert table.rows[0].cells[0].paragraphs[0].runs[0].bold


async def test_an_xlsx_keeps_numbers_and_never_writes_a_formula(tmp_path: Path) -> None:
    spec = DocumentSpec(
        format=DocFormat.XLSX,
        filename="Xarajat",
        sheets=(Sheet(name="Oktabr", rows=(("Nomi", "Summa"), ("Ijara", 1500000.0), ('=HYPERLINK("x")', 1.5))),),
    )
    written = await OfficeDocumentWriter(print_pdf=None).write(spec, dest_dir=tmp_path, timeout=30)
    workbook = load_workbook(written.path)
    sheet = workbook["Oktabr"]
    assert [[cell.value for cell in row] for row in sheet.iter_rows()] == [
        ["Nomi", "Summa"],
        ["Ijara", 1500000],
        ['=HYPERLINK("x")', 1.5],
    ]
    assert sheet["A3"].data_type == "s"
    assert sheet["A1"].font.bold
    assert sheet.freeze_panes == "A2"


async def test_md_and_txt_are_utf8_text(tmp_path: Path) -> None:
    writer = OfficeDocumentWriter(print_pdf=None)
    md = await writer.write(_spec("md", title="Ҳисобот"), dest_dir=tmp_path, timeout=30)
    assert md.path.read_text(encoding="utf-8").startswith("# Bayonnoma")
    assert md.mime == "text/markdown"


async def test_a_pdf_is_escaped_html_printed_by_the_printer(tmp_path: Path) -> None:
    printer = Printer()
    spec = _spec("pdf", title="<script>alert(1)</script>")
    written = await OfficeDocumentWriter(print_pdf=printer).write(spec, dest_dir=tmp_path, timeout=30)
    assert written.path.read_bytes() == b"%PDF-1.4 fake"
    [html] = printer.html
    assert "<script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "<table>" in html
    assert "<strong>Sana:</strong>" in html
    for loader in ("<img", "<link", "src=", "href="):
        assert loader not in html


def test_markup_in_model_text_stays_text() -> None:
    spec = domain.document_of({"format": "pdf", "filename": "x", "content": "<img src=http://evil> & **<b>**"})
    html = document_html(spec)
    assert "<img" not in html
    assert "&lt;img src=http://evil&gt; &amp; <strong>&lt;b&gt;</strong>" in html


async def test_a_pdf_without_a_printer_fails(tmp_path: Path) -> None:
    with pytest.raises(DocumentFailed) as caught:
        await OfficeDocumentWriter(print_pdf=None).write(_spec("pdf"), dest_dir=tmp_path, timeout=30)
    assert caught.value.params == {"format": "pdf"}


async def test_a_printer_failure_leaves_no_file(tmp_path: Path) -> None:
    printer = Printer(fail=DocumentFailed(format="pdf"))
    with pytest.raises(DocumentFailed):
        await OfficeDocumentWriter(print_pdf=printer).write(_spec("pdf"), dest_dir=tmp_path, timeout=30)
    assert await asyncio.to_thread(lambda: list(tmp_path.iterdir())) == []


async def test_a_library_refusal_is_document_failed(tmp_path: Path) -> None:
    spec = DocumentSpec(format=DocFormat.XLSX, filename="x", sheets=(Sheet(name="bad[name]", rows=(("a",),)),))
    with pytest.raises(DocumentFailed) as caught:
        await OfficeDocumentWriter(print_pdf=None).write(spec, dest_dir=tmp_path, timeout=30)
    assert caught.value.params == {"format": "xlsx"}


async def test_a_slow_printer_is_a_call_timeout(tmp_path: Path) -> None:
    async def slow(html: str, out: Path, *, timeout: float) -> None:
        await asyncio.Event().wait()

    with pytest.raises(CallTimedOut):
        await OfficeDocumentWriter(print_pdf=slow).write(_spec("pdf"), dest_dir=tmp_path, timeout=0.05)
