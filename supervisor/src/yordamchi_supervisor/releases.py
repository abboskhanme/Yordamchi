"""Release folders for the self-updater (SPEC §2.2, §3.2.12, §9.3), built the way `scripts/install.py` builds them.

A copy of the installer's rules, because A6 forbids importing anything outside this package:
- the name is `YYYY.MM.DD-<sha7>` (the commit date, then the short sha);
- the tree comes from `git archive <treeish>` of the working copy, never from its files;
- the venv comes from `uv sync --frozen --no-dev --no-editable --compile-bytecode --link-mode clone --managed-python
  --python 3.12.15` (clone, never hardlink: `chmod -R a-w` must not reach into uv's cache);
- `.yordamchi-release.json` marks a finished build, then the whole folder is made read-only.

A folder without the marker is a build that stopped half way (`kill -9`): it is deleted and built again. The
`current` symlinks are relative and swapped with `os.replace`, so a crash leaves the old or the new target, never none.

The test workspace (`check_workspace`) is a second, throw-away copy of the same commit with the dev dependencies, so
the release itself never carries pytest or ruff.
"""

import json
import os
import re
import shutil
import tarfile
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from yordamchi_supervisor.proc import CommandResult, CommandRunner

GIT: Final = "/usr/bin/git"
PYTHON_VERSION: Final = "3.12.15"
RELEASE_MARKER: Final = ".yordamchi-release.json"
UV_SYNC_RELEASE: Final = (
    "sync",
    "--frozen",
    "--no-dev",
    "--no-editable",
    "--compile-bytecode",
    "--link-mode",
    "clone",
    "--managed-python",
    "--python",
    PYTHON_VERSION,
)
UV_SYNC_CHECK: Final = ("sync", "--frozen", "--link-mode", "clone", "--managed-python", "--python", PYTHON_VERSION)
TIMEOUT_GIT_S: Final = 120.0
TIMEOUT_UV_S: Final = 1800.0
DIR_MODE: Final = 0o700
_SHA: Final = re.compile(r"[0-9a-f]{40}")
_DAY: Final = re.compile(r"\d{4}\.\d{2}\.\d{2}")


@dataclass(frozen=True, slots=True)
class Built:
    ok: bool
    path: Path
    problem: str = ""  # a short code plus the last line of the tool's output


def uv_binary(home: Path) -> str:
    return str(home / ".local" / "bin" / "uv")


def git(runner: CommandRunner, repo: Path, *args: str, env: Mapping[str, str]) -> CommandResult:
    return runner.run([GIT, "-C", str(repo), *args], timeout=TIMEOUT_GIT_S, env=env)


def resolve_commit(runner: CommandRunner, repo: Path, commit: str, *, env: Mapping[str, str]) -> str | None:
    """The full sha when `commit` names a commit of the working copy, else None."""
    result = git(runner, repo, "rev-parse", "--verify", "--quiet", f"{commit}^{{commit}}", env=env)
    sha = result.text()
    return sha if result.ok and _SHA.fullmatch(sha) else None


def release_version(runner: CommandRunner, repo: Path, sha: str, *, env: Mapping[str, str]) -> str | None:
    """`YYYY.MM.DD-<sha7>`, exactly as the installer names it."""
    result = git(runner, repo, "log", "-1", "--format=%cd", "--date=format:%Y.%m.%d", sha, env=env)
    day = result.text()
    return f"{day}-{sha[:7]}" if result.ok and _DAY.fullmatch(day) else None


def changed_paths(
    runner: CommandRunner, repo: Path, base: str, head: str, *, env: Mapping[str, str]
) -> list[str] | None:
    result = git(runner, repo, "diff", "--name-only", "--no-renames", base, head, env=env)
    return [line for line in result.text().splitlines() if line.strip()] if result.ok else None


def _walk(root: Path) -> Iterator[Path]:
    for dirpath, dirnames, filenames in os.walk(root):
        base = Path(dirpath)
        for name in (*dirnames, *filenames):
            yield base / name


def make_read_only(root: Path) -> None:
    """`chmod -R a-w`, without following symlinks (`.venv/bin/python` points at uv's interpreter)."""
    for path in [*_walk(root), root]:
        if not path.is_symlink():
            path.chmod(path.stat().st_mode & ~0o222)


def make_writable(root: Path) -> None:
    for path in [root, *_walk(root)]:
        if not path.is_symlink():
            path.chmod(path.stat().st_mode | 0o200)


def remove_tree(root: Path) -> None:
    """Delete a release or a workspace, read-only or not."""
    if root.is_symlink():
        root.unlink()
    elif root.exists():
        make_writable(root)
        shutil.rmtree(root)


def link_target(link: Path) -> str | None:
    """The name a `current` symlink points at (`2026.10.24-abcdef0`), or None when there is no symlink."""
    return link.readlink().name if link.is_symlink() else None


def swap_symlink(link: Path, target: Path) -> None:
    """Point `link` at `target` atomically, as a link relative to `link`'s directory (so DATA can move)."""
    tmp = link.with_name(f".{link.name}.tmp")
    tmp.unlink(missing_ok=True)
    tmp.symlink_to(os.path.relpath(target, link.parent))
    tmp.replace(link)


def read_marker(release: Path) -> dict[str, object] | None:
    try:
        raw = json.loads((release / RELEASE_MARKER).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return {str(key): value for key, value in raw.items()} if isinstance(raw, dict) else None


def marker_commit(release: Path) -> str | None:
    """The sha a finished release was built from (`treeish` is `<sha>` or `<sha>:supervisor`)."""
    marker = read_marker(release)
    treeish = marker.get("treeish") if marker else None
    sha = treeish.split(":", 1)[0] if isinstance(treeish, str) else ""
    return sha if _SHA.fullmatch(sha) else None


def _extract(runner: CommandRunner, repo: Path, treeish: str, target: Path, *, env: Mapping[str, str]) -> str:
    """`git archive` into `target`; '' when it worked, else the problem."""
    archive = target.with_name(f".{target.name}.tar")
    archive.unlink(missing_ok=True)
    try:
        result = git(runner, repo, "archive", "--format=tar", f"--output={archive}", treeish, env=env)
        if not result.ok or not archive.exists():
            return "git_archive_failed: " + " ".join(result.tail(1))
        target.mkdir(mode=DIR_MODE)
        with tarfile.open(archive, mode="r:") as tar:
            tar.extractall(target, filter="data")
    except (OSError, tarfile.TarError) as exc:
        return f"extract_failed: {type(exc).__name__}"
    finally:
        archive.unlink(missing_ok=True)
    return ""


def build_release(
    runner: CommandRunner, *, repo: Path, treeish: str, target: Path, home: Path, env: Mapping[str, str]
) -> Built:
    """A finished, read-only release in `target` (reused when its marker is already there)."""
    if (target / RELEASE_MARKER).exists():
        make_read_only(target)
        return Built(ok=True, path=target)
    remove_tree(target)  # a build that stopped half way
    target.parent.mkdir(parents=True, exist_ok=True, mode=DIR_MODE)
    problem = _extract(runner, repo, treeish, target, env=env)
    if problem:
        remove_tree(target)
        return Built(ok=False, path=target, problem=problem)
    sync = runner.run([uv_binary(home), *UV_SYNC_RELEASE], timeout=TIMEOUT_UV_S, cwd=target, env=env)
    if not sync.ok:
        remove_tree(target)
        return Built(ok=False, path=target, problem="uv_sync_failed: " + " ".join(sync.tail(1)))
    info = {"version": target.name, "treeish": treeish, "python": PYTHON_VERSION, "built_by": "yordamchi_supervisor"}
    (target / RELEASE_MARKER).write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8")
    make_read_only(target)
    return Built(ok=True, path=target)


def check_workspace(
    runner: CommandRunner, *, repo: Path, treeish: str, target: Path, home: Path, env: Mapping[str, str]
) -> Built:
    """A writable copy of the commit with the dev dependencies, where `make check` runs. Deleted afterwards."""
    remove_tree(target)
    target.parent.mkdir(parents=True, exist_ok=True, mode=DIR_MODE)
    problem = _extract(runner, repo, treeish, target, env=env)
    if problem:
        remove_tree(target)
        return Built(ok=False, path=target, problem=problem)
    sync = runner.run([uv_binary(home), *UV_SYNC_CHECK], timeout=TIMEOUT_UV_S, cwd=target, env=env)
    if not sync.ok:
        return Built(ok=False, path=target, problem="uv_sync_failed: " + " ".join(sync.tail(1)))
    return Built(ok=True, path=target)


def prune(directory: Path, *, keep: int, protect: set[str]) -> list[str]:
    """Delete all but the `keep` newest finished releases in `directory` (by mtime), never one named in `protect`."""
    if not directory.is_dir():
        return []
    entries = sorted(
        (p for p in directory.iterdir() if p.is_dir() and not p.is_symlink() and not p.name.startswith(".")),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    removed: list[str] = []
    for old in entries[keep:]:
        if old.name in protect:
            continue
        remove_tree(old)
        removed.append(old.name)
    return removed
