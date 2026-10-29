"""The `DocumentWriter` contract (UC-37): the fake and `OfficeDocumentWriter` write `dest_dir / spec.file_name`, 0600,
report its real size and type, and refuse the same way. Offline: the real adapter's PDF printer is a stand-in here (the
Chromium printer runs in `tests/integration/test_office.py` only through its HTML)."""

import asyncio
import stat
from pathlib import Path

import pytest

from tests.fakes.documents import FakeDocumentWriter
from yordamchi.adapters.office.writer import OfficeDocumentWriter
from yordamchi.core.errors import DocumentFailed
from yordamchi.personal.files import domain
from yordamchi.ports.documents import DocumentWriter


async def _print(html: str, out: Path, *, timeout: float) -> None:
    await asyncio.to_thread(out.write_bytes, b"%PDF-1.4 " + html[:10].encode())


class DocumentWriterContract:
    @pytest.fixture
    def writer(self) -> DocumentWriter:
        raise NotImplementedError

    @pytest.mark.parametrize("fmt", ["docx", "pdf", "xlsx", "md", "txt"])
    async def test_each_format_is_written_privately_under_its_name(
        self, writer: DocumentWriter, fmt: str, tmp_path: Path
    ) -> None:
        spec = domain.document_of({"format": fmt, "filename": "Hisobot", "content": "# Hisobot\n\n| A |\n|---|\n| 1 |"})
        written = await writer.write(spec, dest_dir=tmp_path, timeout=30.0)
        assert written.path == tmp_path / f"Hisobot.{fmt}"
        assert await asyncio.to_thread(written.path.exists)
        size = (await asyncio.to_thread(written.path.stat)).st_size
        assert written.size_bytes == size > 0
        assert stat.S_IMODE((await asyncio.to_thread(written.path.stat)).st_mode) == 0o600
        assert written.mime


class TestFake(DocumentWriterContract):
    @pytest.fixture
    def writer(self) -> DocumentWriter:
        fake = FakeDocumentWriter()
        fake.size_bytes = 64
        return fake


class TestOffice(DocumentWriterContract):
    @pytest.fixture
    def writer(self) -> DocumentWriter:
        return OfficeDocumentWriter(print_pdf=_print)


async def test_both_refuse_with_document_failed(tmp_path: Path) -> None:
    spec = domain.document_of({"format": "pdf", "filename": "x", "content": "a"})
    fake = FakeDocumentWriter()
    fake.failure = DocumentFailed(format="pdf")
    for writer in (fake, OfficeDocumentWriter(print_pdf=None)):
        with pytest.raises(DocumentFailed):
            await writer.write(spec, dest_dir=tmp_path, timeout=5.0)
