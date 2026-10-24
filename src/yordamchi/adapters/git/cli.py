"""`GitCli`: the `Git` port over the `git` binary (CONVENTIONS §4, §6; SPEC §3.2.4, §3.2.6, §5.7).

Every call runs local git commands through `core.proc`: an argv list (never a shell), its own process group, the
call's `timeout`, and the environment of `proc.clean_env()` with these changes:

- no inherited `GIT_*` variable: a hook's `GIT_DIR` or `GIT_INDEX_FILE` would point git at another repository;
- `GIT_OPTIONAL_LOCKS=0`: the digest's status poll never takes the index lock an agent's own git may need;
- `GIT_TERMINAL_PROMPT=0`, and `LC_ALL=C` so error messages are stable for the mapping below.

`checkout_paths` adds `--literal-pathspecs` (a path is a path, never a glob). It is not set for every command:
`check-ignore` refuses to run under it.

Output is read in machine formats (`--porcelain=v2 -z`, `--format` with separator bytes, NUL-separated paths), so file
names with spaces or non-ASCII letters round-trip. A ref or a name that starts with `-` is refused with `ValueError`
before git runs: refs come from Yordamchi's own rows, so one that looks like an option is a bug.

Errors: the binary missing → `NotInstalled(git)`; the timeout → `CallTimedOut(git_<op>)`; a non-zero exit →
`GitFailed(git_<op>, reason)`. Only `reason="locked"` (another git holds `index.lock`) is retryable, through
`core.retry`, inside the same timeout. git's stderr only picks the reason and is then dropped: it can quote file
content, and it never reaches a log line or an exception.
"""

import os
from collections.abc import Collection, Iterator, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, NamedTuple
from urllib.parse import urlsplit, urlunsplit

from yordamchi.core import proc, retry
from yordamchi.core.errors import CallTimedOut, GitFailed, NotInstalled
from yordamchi.core.log import get_log
from yordamchi.ports.git import Commit, FileChange, GitStatus

log = get_log(__name__)

BINARY: Final = "git"
REMOTE: Final = "origin"
# Cleanup after a failed revert must run even when the caller's budget is spent: a repo left mid-revert is worse.
ABORT_TIMEOUT_S: Final = 10.0

# Per-invocation config: raw UTF-8 paths, no fsmonitor daemon spawned by a status poll, no colour codes.
_CONFIG: Final = ("-c", "core.quotepath=off", "-c", "core.fsmonitor=false", "-c", "color.ui=false")
_ENV: Final[Mapping[str, str]] = {"GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"}
_FIELD: Final = "\x1f"
_LOG_FORMAT: Final = "--format=%H%x1f%ct%x1f%s"
_SYMLINK_MODE: Final = "120000"
# stderr substring (lowercased) → reason, first match wins.
_REASONS: Final = (
    ("index.lock", "locked"),
    (".lock': file exists", "locked"),  # another ref lock; a path such as yarn.lock in a message is not one
    ("not a git repository", "not_a_repo"),
    ("unknown revision", "bad_ref"),
    ("bad revision", "bad_ref"),
    ("not a valid object name", "bad_ref"),
    ("bad object", "bad_ref"),
    ("ambiguous argument", "bad_ref"),
    ("invalid reference", "bad_ref"),
    ("did not match any file", "bad_ref"),
    ("conflict", "conflict"),
    ("could not revert", "conflict"),
    ("would be overwritten", "conflict"),
)


def _reason(stderr: bytes) -> str:
    text = stderr.decode("utf-8", errors="replace").lower()
    return next((reason for needle, reason in _REASONS if needle in text), "failed")


def _checked(value: str, *, what: str) -> str:
    if not value or value.startswith("-") or any(char in value for char in "\0\n"):
        msg = f"refusing a git {what} that is empty, has a newline or looks like an option"
        raise ValueError(msg)
    return value


def _nul_split(data: bytes) -> list[str]:
    return [os.fsdecode(item) for item in data.split(b"\0") if item]


def _commits(data: bytes) -> list[Commit]:
    """Records of `_LOG_FORMAT` separated by NUL (`log -z`)."""
    commits: list[Commit] = []
    for record in data.split(b"\0"):
        fields = record.decode("utf-8", errors="replace").strip("\n").split(_FIELD, 2)
        if len(fields) == 3 and fields[0]:
            sha, epoch, subject = fields
            commits.append(Commit(sha=sha, subject=subject, committed_at=datetime.fromtimestamp(int(epoch), UTC)))
    return commits


def _status(data: bytes) -> GitStatus:
    """`status --porcelain=v2 --branch -z`: headers, then one entry per path (a rename carries its old path next)."""
    branch: str | None = None
    head: str | None = None
    upstream: str | None = None
    ahead = behind = changed = untracked = 0
    entries = iter(data.split(b"\0"))
    for raw in entries:
        entry = os.fsdecode(raw)
        if entry.startswith("# branch.oid "):
            oid = entry.removeprefix("# branch.oid ")
            head = None if oid == "(initial)" else oid
        elif entry.startswith("# branch.head "):
            name = entry.removeprefix("# branch.head ")
            branch = None if name == "(detached)" else name
        elif entry.startswith("# branch.upstream "):
            upstream = entry.removeprefix("# branch.upstream ")
        elif entry.startswith("# branch.ab "):
            plus, minus = entry.removeprefix("# branch.ab ").split()
            ahead, behind = int(plus), abs(int(minus))
        elif entry[:2] in {"1 ", "u "}:
            changed += 1
        elif entry.startswith("2 "):
            changed += 1
            next(entries, None)  # the original path of the rename or copy
        elif entry.startswith("? "):
            untracked += 1
    return GitStatus(
        branch=branch, head=head, upstream=upstream, ahead=ahead, behind=behind, changed=changed, untracked=untracked
    )


def _changes(data: bytes) -> list[FileChange]:
    """`diff --name-status -z`: a status, then one path (two for a rename or copy: old, new)."""
    changes: list[FileChange] = []
    fields = iter(_nul_split(data))
    for status in fields:
        letter = status[:1]
        if letter in {"R", "C"}:
            old_path, path = next(fields), next(fields)
            changes.append(FileChange(status=letter, path=path, old_path=old_path))
        else:
            changes.append(FileChange(status=letter, path=next(fields)))
    return changes


def _without_credentials(url: str) -> str:
    """`https://user:token@host/x` → `https://host/x`. scp-style `git@host:x` and `ssh://git@host/x` keep their user:
    it is not a secret."""
    parts = urlsplit(url)
    if not parts.scheme or "@" not in parts.netloc:
        return url
    if parts.password is None and parts.scheme not in {"http", "https"}:
        return url
    host = parts.netloc.rsplit("@", 1)[1]
    return urlunsplit(parts._replace(netloc=host))


class _TreeEntry(NamedTuple):
    name: str  # the file name inside the listed directory
    mode: str
    kind: str  # blob | tree | commit (a submodule)
    obj: str
    size: int | None  # None for a tree or a submodule


def _tree_entries(data: bytes) -> Iterator[_TreeEntry]:
    """`ls-tree -z --long`: `<mode> <type> <object> <size>\\t<path>` per entry."""
    for raw in data.split(b"\0"):
        meta, tab, path = raw.partition(b"\t")
        fields = meta.decode("ascii", errors="replace").split()
        if tab and len(fields) == 4:
            mode, kind, obj, size = fields
            name = os.fsdecode(path).rsplit("/", 1)[-1]
            yield _TreeEntry(name=name, mode=mode, kind=kind, obj=obj, size=int(size) if size.isdigit() else None)


def _blobs(data: bytes) -> dict[str, bytes]:
    """`cat-file --batch` output, `<object> <type> <size>\\n<content>\\n` per object, by object id. An object git
    could not find (`<object> missing`) is absent."""
    found: dict[str, bytes] = {}
    cursor = 0
    while (end := data.find(b"\n", cursor)) != -1:
        header = data[cursor:end].decode("ascii", errors="replace").split()
        cursor = end + 1
        if len(header) == 3 and header[2].isdigit():
            obj, size = header[0], int(header[2])
            found[obj] = data[cursor : cursor + size]
            cursor += size + 1
    return found


class GitCli:
    """`Git` port adapter. `search_path` is the child PATH built at startup (`proc.build_search_path`)."""

    def __init__(self, search_path: str, *, binary: str = BINARY) -> None:
        self._search_path = search_path
        self._binary = binary

    def _env(self) -> dict[str, str]:
        env = proc.clean_env(path=self._search_path, extra=_ENV)
        return {name: value for name, value in env.items() if not name.startswith("GIT_") or name in _ENV}

    async def _run(
        self,
        operation: str,
        repo: Path | None,
        *args: str,
        timeout: float,
        stdin: bytes | None = None,
        answers: Collection[int] = (0,),
    ) -> proc.ProcResult:
        """One git command. An exit code in `answers` is a result; any other raises `GitFailed`."""

        async def attempt() -> proc.ProcResult:
            try:
                result = await proc.run(
                    [self._binary, *_CONFIG, *args], env=self._env(), timeout=timeout, cwd=repo, stdin=stdin
                )
            except TimeoutError:
                raise CallTimedOut(operation=operation) from None
            except (FileNotFoundError, NotADirectoryError) as error:
                # The missing name is the working directory (a Path) or the binary: only the first is a repo problem.
                if repo is not None and error.filename is not None and str(error.filename) == str(repo):
                    raise GitFailed(operation=operation, reason="not_a_repo") from None
                raise NotInstalled(binary=BINARY) from None
            except OSError:
                raise GitFailed(operation=operation, reason="failed") from None
            if result.returncode not in answers:
                reason = _reason(result.stderr)
                log.warning("git_failed", operation=operation, reason=reason, returncode=result.returncode)
                raise GitFailed(operation=operation, reason=reason)
            return result

        return await retry.retrying(attempt, timeout=timeout, operation=operation)

    # --- reads -------------------------------------------------------------------------------------------------
    async def head(self, repo: Path, *, timeout: float) -> str | None:
        result = await self._run(
            "git_head", repo, "rev-parse", "--verify", "-q", "HEAD^{commit}", timeout=timeout, answers=(0, 1)
        )
        return result.stdout.decode("ascii").strip() or None

    async def current_branch(self, repo: Path, *, timeout: float) -> str | None:
        result = await self._run(
            "git_current_branch", repo, "symbolic-ref", "-q", "--short", "HEAD", timeout=timeout, answers=(0, 1)
        )
        return os.fsdecode(result.stdout).strip() or None

    async def find_commit(self, repo: Path, ref: str, *, timeout: float) -> Commit | None:
        # --ignore-missing: a ref that names nothing (an empty repo's HEAD) prints nothing instead of failing.
        rev = _checked(ref, what="ref")
        args = ("log", "-1", "-z", "--ignore-missing", "--no-show-signature", _LOG_FORMAT, rev, "--")
        result = await self._run("git_find_commit", repo, *args, timeout=timeout)
        found = _commits(result.stdout)
        return found[0] if found else None

    async def status(self, repo: Path, *, timeout: float) -> GitStatus:
        args = ("status", "--porcelain=v2", "--branch", "-z", "--untracked-files=normal")
        result = await self._run("git_status", repo, *args, timeout=timeout)
        return _status(result.stdout)

    async def log(self, repo: Path, *, since_days: int, limit: int, timeout: float) -> list[Commit]:
        if limit <= 0:
            return []
        since = f"--since={since_days}.days.ago"
        args = ("log", f"-n{limit}", since, "-z", "--ignore-missing", "--no-show-signature", _LOG_FORMAT, "HEAD", "--")
        result = await self._run("git_log", repo, *args, timeout=timeout)
        return _commits(result.stdout)

    async def commits_since(self, repo: Path, base: str, *, timeout: float) -> list[Commit]:
        span = f"{_checked(base, what='ref')}..HEAD"
        args = ("log", "-z", "--topo-order", "--no-show-signature", _LOG_FORMAT, span, "--")
        result = await self._run("git_commits_since", repo, *args, timeout=timeout)
        return _commits(result.stdout)

    async def changed_files(self, repo: Path, base: str, *, timeout: float) -> list[FileChange]:
        rev = _checked(base, what="ref")
        args = ("diff", "--name-status", "-z", "-M", "--no-color", "--no-ext-diff", rev, "--")
        result = await self._run("git_changed_files", repo, *args, timeout=timeout)
        return _changes(result.stdout)

    async def untracked_files(self, repo: Path, *, timeout: float) -> list[str]:
        result = await self._run(
            "git_untracked_files", repo, "ls-files", "-o", "--exclude-standard", "-z", timeout=timeout
        )
        return _nul_split(result.stdout)

    async def ignored(self, repo: Path, paths: Sequence[str], *, timeout: float) -> frozenset[str]:
        if not paths:
            return frozenset()
        stdin = b"".join(os.fsencode(path) + b"\0" for path in paths)
        result = await self._run(
            "git_ignored", repo, "check-ignore", "-z", "--stdin", timeout=timeout, stdin=stdin, answers=(0, 1)
        )
        return frozenset(_nul_split(result.stdout)) & frozenset(paths)

    async def default_branch(self, repo: Path, *, timeout: float) -> str | None:
        remote_head = f"refs/remotes/{REMOTE}/HEAD"
        args = ("for-each-ref", "--format=%(refname)%00%(symref)", remote_head, "refs/heads/main", "refs/heads/master")
        result = await self._run("git_default_branch", repo, *args, timeout=timeout)
        refs: dict[str, str] = {}
        for line in os.fsdecode(result.stdout).splitlines():
            name, _, target = line.partition("\0")
            refs[name] = target
        remote_prefix = f"refs/remotes/{REMOTE}/"
        if refs.get(remote_head, "").startswith(remote_prefix):
            return refs[remote_head].removeprefix(remote_prefix)
        for local in ("main", "master"):
            if f"refs/heads/{local}" in refs:
                return local
        return await self.current_branch(repo, timeout=timeout)

    async def remote_url(self, repo: Path, *, remote: str, timeout: float) -> str | None:
        key = f"remote.{_checked(remote, what='remote')}.url"
        result = await self._run("git_remote_url", repo, "config", "--get", key, timeout=timeout, answers=(0, 1))
        url = result.stdout.decode("utf-8", errors="replace").strip()
        return _without_credentials(url) if url else None

    async def tree_files(
        self, repo: Path, ref: str, directory: str, *, max_bytes: int, timeout: float
    ) -> Mapping[str, bytes | None]:
        folder = directory.strip("/")
        rev = _checked(ref, what="ref")
        try:
            listing = await self._run(
                "git_tree_files", repo, "ls-tree", "-z", "--long", rev, "--", f"{folder}/", timeout=timeout
            )
        except GitFailed as error:
            if error.reason == "bad_ref":  # no commit yet, or a ref that names nothing
                return {}
            raise
        files: dict[str, bytes | None] = {}
        to_read: dict[str, list[str]] = {}  # object id → the names holding it (identical files share one blob)
        for entry in _tree_entries(listing.stdout):
            if entry.kind != "blob":
                continue
            files[entry.name] = None  # a symlink or an oversized file stays None: present, not read
            if entry.mode != _SYMLINK_MODE and entry.size is not None and entry.size <= max_bytes:
                to_read.setdefault(entry.obj, []).append(entry.name)
        if to_read:
            stdin = "".join(f"{obj}\n" for obj in to_read).encode("ascii")
            batch = await self._run("git_tree_files", repo, "cat-file", "--batch", timeout=timeout, stdin=stdin)
            for obj, content in _blobs(batch.stdout).items():
                for name in to_read.get(obj, ()):
                    files[name] = content
        return files

    # --- writes ------------------------------------------------------------------------------------------------
    async def init(self, path: Path, *, branch: str, timeout: float) -> None:
        target = _checked(str(path), what="path")
        initial = f"--initial-branch={_checked(branch, what='branch')}"
        await self._run("git_init", None, "init", "-q", initial, target, timeout=timeout)

    async def create_branch(self, repo: Path, name: str, *, timeout: float) -> None:
        await self._run("git_create_branch", repo, "switch", "-q", "-c", _checked(name, what="branch"), timeout=timeout)

    async def checkout_paths(self, repo: Path, ref: str, paths: Sequence[str], *, timeout: float) -> None:
        if not paths:
            return
        rev = _checked(ref, what="ref")
        stdin = b"".join(os.fsencode(path) + b"\0" for path in paths)
        args = ("--literal-pathspecs", "checkout", "-q", rev, "--pathspec-from-file=-", "--pathspec-file-nul")
        await self._run("git_checkout_paths", repo, *args, timeout=timeout, stdin=stdin)

    async def _newest_first(self, repo: Path, revs: Sequence[str], *, timeout: float) -> list[str]:
        """The commits as full ids, children before parents. Commit times can tie (several commits in one second), so
        the order comes from the graph: a walk from the commits down to their common base."""
        resolved = await self._run("git_revert", repo, "rev-list", "--no-walk=unsorted", *revs, "--", timeout=timeout)
        wanted = resolved.stdout.decode("ascii").split()
        base = await self._run("git_revert", repo, "merge-base", "--octopus", *wanted, timeout=timeout, answers=(0, 1))
        bottom = base.stdout.decode("ascii").strip()
        if not bottom:  # unrelated histories: no graph order exists, so newest commit time first
            by_time = await self._run("git_revert", repo, "rev-list", "--no-walk", *wanted, "--", timeout=timeout)
            return by_time.stdout.decode("ascii").split()
        walk = await self._run(
            "git_revert", repo, "rev-list", "--topo-order", *wanted, "--not", f"{bottom}^@", "--", timeout=timeout
        )
        chosen = set(wanted)
        return [sha for sha in walk.stdout.decode("ascii").split() if sha in chosen]

    async def revert(self, repo: Path, commits: Sequence[str], *, timeout: float) -> None:
        if not commits:
            return
        revs = [_checked(commit, what="ref") for commit in commits]
        newest_first = await self._newest_first(repo, revs, timeout=timeout)
        try:
            await self._run("git_revert", repo, "revert", "--no-edit", *newest_first, timeout=timeout)
        except (GitFailed, CallTimedOut) as error:
            if not (isinstance(error, GitFailed) and error.reason == "locked"):  # a lock means nothing started
                await self._abort_revert(repo)
            raise

    async def _abort_revert(self, repo: Path) -> None:
        """Put the repo back as it was before the revert. An abort with nothing to abort fails harmlessly."""
        try:
            await self._run("git_revert_abort", repo, "revert", "--abort", timeout=ABORT_TIMEOUT_S, answers=(0, 128))
        except (GitFailed, CallTimedOut):
            log.warning("git_revert_abort_failed")
