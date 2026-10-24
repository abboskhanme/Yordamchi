"""Copy-truncate of the launchd stdout/stderr logs (SPEC §9.2, NFR-CAP-05).

launchd owns `~/Library/Logs/Yordamchi/uz.yordamchi.<x>.launchd.log` and keeps it open, so it cannot be rotated by
renaming. Each file over 5 MB is copy-truncated: its last 1 MB, starting at a line boundary, goes to `<file>.1`
(replacing the previous one), and the file is truncated in place. launchd opens these files with `O_APPEND`, so the
next write lands at offset 0 and no sparse hole is left. Lines written between the copy and the truncate are lost,
which is the usual copy-truncate trade-off.

Budget (NFR-CAP-05 ≤ 200 MB): bots 2 × 20 MB × 4 = 160 MB, supervisor 10 MB × 2 = 20 MB, launchd logs
3 × (5 MB + 1 MB) = 18 MB. Total 198 MB.
"""

import os
from pathlib import Path
from typing import Final

from yordamchi_supervisor.log import JsonLog
from yordamchi_supervisor.paths import PROCESSES, SupervisorPaths

MAX_BYTES: Final = 5 * 1024 * 1024
KEEP_TAIL_BYTES: Final = 1024 * 1024


def trim_file(path: Path, *, max_bytes: int = MAX_BYTES, keep_tail: int = KEEP_TAIL_BYTES) -> int | None:
    """The size before trimming when the file was trimmed, else None."""
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return None
    if size <= max_bytes:
        return None
    with path.open("r+b") as handle:
        start = max(0, size - keep_tail)
        handle.seek(start)
        tail = handle.read(size - start)
        if start > 0:
            newline = tail.find(b"\n")
            tail = tail[newline + 1 :] if newline >= 0 else b""
        copy = path.with_name(path.name + ".1")
        tmp = path.with_name(path.name + ".1.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as out:
            out.write(tail)
        tmp.replace(copy)
        handle.truncate(0)
    return size


def trim_launchd_logs(paths: SupervisorPaths, log: JsonLog) -> None:
    for process in PROCESSES:
        path = paths.launchd_log(process)
        try:
            before = trim_file(path)
        except OSError as exc:
            log.warning("launchd_log_trim_failed", process=process, error=type(exc).__name__)
            continue
        if before is not None:
            log.info("launchd_log_trimmed", process=process, size_before=before)
