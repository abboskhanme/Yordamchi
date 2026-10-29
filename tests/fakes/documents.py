"""`FakeDocumentWriter`: writes a small placeholder file (0600) per spec and records each spec, so tests can check
what the assistant asked for. A scripted failure raises instead of writing."""

from pathlib import Path

from yordamchi.ports.documents import DocumentSpec, DocumentWriter, WrittenDocument

MIMES = {
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pdf": "application/pdf",
    "md": "text/markdown",
    "txt": "text/plain",
}


class FakeDocumentWriter:
    def __init__(self) -> None:
        self.specs: list[DocumentSpec] = []
        self.timeouts: list[float] = []
        self.failure: Exception | None = None
        self.size_bytes = 1024

    async def write(self, spec: DocumentSpec, *, dest_dir: Path, timeout: float) -> WrittenDocument:
        self.specs.append(spec)
        self.timeouts.append(timeout)
        if self.failure is not None:
            raise self.failure
        path = dest_dir / spec.file_name
        path.write_bytes(b"fake document")
        with path.open("r+b") as handle:
            handle.truncate(self.size_bytes)
        path.chmod(0o600)
        return WrittenDocument(path=path, mime=MIMES[spec.format.value], size_bytes=self.size_bytes)


_: DocumentWriter = FakeDocumentWriter()
