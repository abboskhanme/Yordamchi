"""CLI probe and user-message content, for the assistant pool (T12) without importing the executor.

The personal bot may hold the assistant but never the executor (C7, `allow_indirect_imports = false`), so what the
two share cannot live in `executor.py`. These are the executor's `probe()` and `_content()` shapes; the executor
moves onto this module when it is next edited (pending wiring, T12).
"""

import asyncio
import base64
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Final

from yordamchi.core import proc
from yordamchi.core.errors import CallTimedOut, ConfigInvalid
from yordamchi.ports.agent import RuntimeStatus

IMAGE_TYPES: Final = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}
DOCUMENT_TYPES: Final = {".pdf": "application/pdf"}


def version_of(text: str) -> str | None:
    first = text.strip().split(maxsplit=1)
    return first[0] if first and first[0][:1].isdigit() else None


def auth_status(stdout: bytes) -> Mapping[str, Any]:
    try:
        parsed = json.loads(stdout.decode("utf-8", errors="replace"))
    except ValueError:
        return {}
    return parsed if isinstance(parsed, Mapping) else {}


async def probe(
    cli_path: Path, *, search_path: str, base_env: Mapping[str, str] | None, timeout: float
) -> RuntimeStatus:
    """`claude --version` and `claude auth status` (JSON): installed, version, `authMethod: "claude.ai"`."""
    env = proc.clean_env(path=search_path, base=base_env)
    cli = str(cli_path)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    try:
        version = await proc.run([cli, "--version"], env=env, timeout=timeout / 2)
        status = await proc.run([cli, "auth", "status"], env=env, timeout=max(0.1, deadline - loop.time()))
    except (FileNotFoundError, PermissionError):
        return RuntimeStatus(installed=False, version=None, logged_in=False, auth_method=None, missing=("claude",))
    except TimeoutError:
        raise CallTimedOut(operation="claude_probe") from None
    auth = auth_status(status.stdout)
    logged_in = auth.get("loggedIn") is True
    method = auth.get("authMethod")
    return RuntimeStatus(
        installed=version.returncode == 0,
        version=version_of(version.stdout.decode("utf-8", errors="replace")),
        logged_in=logged_in,
        auth_method=str(method) if isinstance(method, str) else None,
        missing=() if logged_in else ("login",),
    )


async def user_content(text: str, images: Sequence[Path], documents: Sequence[Path] = ()) -> str | list[dict[str, Any]]:
    """Plain text, or text plus base64 image blocks, then base64 PDF `document` blocks (UC-37)."""
    if not images and not documents:
        return text
    blocks: list[dict[str, Any]] = [{"type": "text", "text": text}]
    for path in images:
        media_type = IMAGE_TYPES.get(path.suffix.lower())
        if media_type is None:
            raise ConfigInvalid(key="images", problem="unsupported_image_type")
        blocks.append({"type": "image", "source": await _base64_source(path, media_type)})
    for path in documents:
        media_type = DOCUMENT_TYPES.get(path.suffix.lower())
        if media_type is None:
            raise ConfigInvalid(key="documents", problem="unsupported_document_type")
        blocks.append({"type": "document", "source": await _base64_source(path, media_type)})
    return blocks


async def _base64_source(path: Path, media_type: str) -> dict[str, str]:
    data = await asyncio.to_thread(path.read_bytes)
    return {"type": "base64", "media_type": media_type, "data": base64.b64encode(data).decode("ascii")}
