"""`FfmpegEncoder`: re-encode a video to fit the send cap, and read a file's duration (static ffmpeg/ffprobe 9.0.2
in `DATA/bin`, STACK §2).

Runs through `core.proc` only: an argv list, never a shell, its own process group, killed on timeout. The bitrates
and the height come from the caller (`personal/artefacts/domain.plan_fit`); this file only turns them into argv.

Output is H.264 + AAC in an MP4 with `+faststart`, which Telegram plays inline. The encoder is `libx264` when the
binary has it, else macOS's `h264_videotoolbox`. ffmpeg's stderr is never kept: it quotes paths and metadata.
"""

import contextlib
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import Final

from yordamchi.core import proc
from yordamchi.core.errors import CallTimedOut, NotInstalled, VideoEncodeFailed
from yordamchi.core.log import get_log
from yordamchi.core.proc import ProcResult
from yordamchi.ports.web import DownloadedVideo

log = get_log(__name__)

PROBE_TIMEOUT_S: Final = 15.0
ENCODERS_TIMEOUT_S: Final = 10.0
SOFTWARE_ENCODER: Final = "libx264"
HARDWARE_ENCODER: Final = "h264_videotoolbox"

type Runner = Callable[..., Awaitable[ProcResult]]


def encode_argv(
    ffmpeg: Path, source: Path, out: Path, *, encoder: str, video_kbps: int, audio_kbps: int, max_height: int
) -> list[str]:
    """One ABR pass: scaled down to `max_height` (never up, width kept even), capped peak rate, stereo AAC."""
    argv = [
        str(ffmpeg), "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
        "-i", str(source),
        "-map", "0:v:0", "-map", "0:a:0?",
        "-vf", f"scale=-2:'min({max_height},ih)'",
        "-c:v", encoder, "-b:v", f"{video_kbps}k", "-maxrate", f"{video_kbps}k", "-bufsize", f"{2 * video_kbps}k",
    ]  # fmt: skip
    if encoder == SOFTWARE_ENCODER:
        argv += ["-preset", "veryfast"]
    argv += [
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", f"{audio_kbps}k", "-ac", "2",
        "-movflags", "+faststart",
        str(out),
    ]  # fmt: skip
    return argv


def probe_argv(ffprobe: Path, path: Path) -> list[str]:
    return [
        str(ffprobe), "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(path),
    ]  # fmt: skip


def parse_duration(stdout: bytes) -> float | None:
    text = stdout.decode("utf-8", errors="replace").strip().splitlines()
    try:
        value = float(text[0]) if text else None
    except ValueError:
        return None
    return value if value is not None and value > 0 else None


def pick_encoder(encoders_listing: bytes) -> str:
    """`libx264` if `ffmpeg -encoders` lists it, else the macOS hardware encoder."""
    names = {
        line.split()[1] for line in encoders_listing.decode("utf-8", "replace").splitlines() if len(line.split()) > 1
    }
    return SOFTWARE_ENCODER if SOFTWARE_ENCODER in names else HARDWARE_ENCODER


class FfmpegEncoder:
    """`ffmpeg`/`ffprobe` are absolute paths (`DATA/bin`); `child_path` is the children's PATH."""

    def __init__(self, *, ffmpeg: Path, ffprobe: Path, child_path: str, run: Runner = proc.run) -> None:
        self._ffmpeg = ffmpeg
        self._ffprobe = ffprobe
        self._child_path = child_path
        self._run = run
        self._encoder: str | None = None

    def _env(self) -> Mapping[str, str]:
        return proc.clean_env(path=self._child_path)

    async def _call(self, argv: Sequence[str], *, timeout: float, operation: str) -> ProcResult:
        try:
            return await self._run(argv, env=self._env(), timeout=timeout)
        except TimeoutError:
            raise CallTimedOut(operation=operation) from None
        except FileNotFoundError:
            raise NotInstalled(binary=Path(argv[0]).name) from None

    async def encoder(self) -> str:
        if self._encoder is None:
            result = await self._call(
                [str(self._ffmpeg), "-hide_banner", "-encoders"], timeout=ENCODERS_TIMEOUT_S, operation="video_encode"
            )
            self._encoder = pick_encoder(result.stdout)
        return self._encoder

    async def duration(self, path: Path, *, timeout: float = PROBE_TIMEOUT_S) -> float | None:
        """The file's duration, or None if ffprobe is missing or cannot tell (the caller then cannot re-encode)."""
        if not self._ffprobe.exists():
            return None
        try:
            result = await self._call(probe_argv(self._ffprobe, path), timeout=timeout, operation="video_probe")
        except (CallTimedOut, NotInstalled):
            return None
        return parse_duration(result.stdout) if result.returncode == 0 else None

    async def reencode(
        self, source: Path, *, dest_dir: Path, video_kbps: int, audio_kbps: int, max_height: int, timeout: float
    ) -> DownloadedVideo:
        if not self._ffmpeg.exists():
            raise NotInstalled(binary="ffmpeg")
        out = dest_dir / f"video-{video_kbps}k.mp4"
        argv = encode_argv(
            self._ffmpeg,
            source,
            out,
            encoder=await self.encoder(),
            video_kbps=video_kbps,
            audio_kbps=audio_kbps,
            max_height=max_height,
        )
        result = await self._call(argv, timeout=timeout, operation="video_encode")
        size = out.stat().st_size if out.exists() else 0
        if result.returncode != 0 or size == 0:
            with contextlib.suppress(OSError):
                out.unlink(missing_ok=True)
            log.warning("video_encode_failed", exit_code=result.returncode, duration_ms=result.duration_ms)
            raise VideoEncodeFailed
        out.chmod(0o600)
        return DownloadedVideo(
            path=out,
            mime="video/mp4",
            size_bytes=size,
            duration_s=await self.duration(out),
            title="",
            height=max_height,
        )
