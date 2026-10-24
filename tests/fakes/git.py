"""`FakeGit` and `FakeGitHub`.

Git semantics are tested with the real adapter on `tmp_path` repos (CONVENTIONS §4); `FakeGit` is for services that
only need scripted facts (a status, a head) or a scripted failure. Unknown repos answer like an empty clean repo.
"""

from collections import defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path

from yordamchi.ports.git import Commit, FileChange, Git, GitStatus
from yordamchi.ports.github import GitHub

CLEAN = GitStatus(branch="main", head=None, upstream=None, ahead=0, behind=0, changed=0, untracked=0)


class FakeGit:
    def __init__(self) -> None:
        self.heads: dict[Path, str | None] = {}
        self.branches: dict[Path, str | None] = {}  # current branch; unknown repos are on "main"
        self.commits: dict[tuple[Path, str], Commit] = {}  # (repo, ref) → the commit it names
        self.trees: dict[tuple[Path, str, str], dict[str, bytes | None]] = {}  # (repo, ref, directory) → files
        self.statuses: dict[Path, GitStatus] = {}
        self.logs: defaultdict[Path, list[Commit]] = defaultdict(list)
        self.changes: defaultdict[Path, list[FileChange]] = defaultdict(list)
        self.untracked: defaultdict[Path, list[str]] = defaultdict(list)
        self.ignored_paths: defaultdict[Path, set[str]] = defaultdict(set)
        self.default_branches: dict[Path, str] = {}
        self.remotes: dict[tuple[Path, str], str] = {}
        self.failures: dict[str, Exception] = {}
        self.calls: list[tuple[str, Path, tuple[str, ...]]] = []

    def _call(self, method: str, repo: Path, *args: str) -> None:
        self.calls.append((method, repo, args))
        if method in self.failures:
            raise self.failures.pop(method)

    async def head(self, repo: Path, *, timeout: float) -> str | None:
        self._call("head", repo)
        return self.heads.get(repo)

    async def current_branch(self, repo: Path, *, timeout: float) -> str | None:
        self._call("current_branch", repo)
        return self.branches.get(repo, "main")

    async def find_commit(self, repo: Path, ref: str, *, timeout: float) -> Commit | None:
        self._call("find_commit", repo, ref)
        return self.commits.get((repo, ref))

    async def tree_files(
        self, repo: Path, ref: str, directory: str, *, max_bytes: int, timeout: float
    ) -> Mapping[str, bytes | None]:
        self._call("tree_files", repo, ref, directory)
        files = self.trees.get((repo, ref, directory.strip("/")), {})
        return {name: None if data is None or len(data) > max_bytes else data for name, data in files.items()}

    async def status(self, repo: Path, *, timeout: float) -> GitStatus:
        self._call("status", repo)
        return self.statuses.get(repo, CLEAN)

    async def log(self, repo: Path, *, since_days: int, limit: int, timeout: float) -> list[Commit]:
        self._call("log", repo, str(since_days), str(limit))
        return self.logs[repo][:limit]

    async def commits_since(self, repo: Path, base: str, *, timeout: float) -> list[Commit]:
        self._call("commits_since", repo, base)
        return list(self.logs[repo])

    async def changed_files(self, repo: Path, base: str, *, timeout: float) -> list[FileChange]:
        self._call("changed_files", repo, base)
        return list(self.changes[repo])

    async def untracked_files(self, repo: Path, *, timeout: float) -> list[str]:
        self._call("untracked_files", repo)
        return list(self.untracked[repo])

    async def ignored(self, repo: Path, paths: Sequence[str], *, timeout: float) -> frozenset[str]:
        self._call("ignored", repo, *paths)
        return frozenset(path for path in paths if path in self.ignored_paths[repo])

    async def default_branch(self, repo: Path, *, timeout: float) -> str | None:
        self._call("default_branch", repo)
        return self.default_branches.get(repo)

    async def remote_url(self, repo: Path, *, remote: str, timeout: float) -> str | None:
        self._call("remote_url", repo, remote)
        return self.remotes.get((repo, remote))

    async def init(self, path: Path, *, branch: str, timeout: float) -> None:
        self._call("init", path, branch)
        self.default_branches[path] = branch

    async def create_branch(self, repo: Path, name: str, *, timeout: float) -> None:
        self._call("create_branch", repo, name)

    async def checkout_paths(self, repo: Path, ref: str, paths: Sequence[str], *, timeout: float) -> None:
        self._call("checkout_paths", repo, ref, *paths)

    async def revert(self, repo: Path, commits: Sequence[str], *, timeout: float) -> None:
        self._call("revert", repo, *commits)


class FakeGitHub:
    def __init__(self, *, user: str | None = "abboskhanme") -> None:
        self.user = user
        self.created: list[tuple[str, str, Path]] = []

    async def auth_user(self, *, timeout: float) -> str | None:
        return self.user

    async def create_private_repo(self, owner: str, name: str, *, source: Path, timeout: float) -> str:
        self.created.append((owner, name, source))
        return f"https://github.com/{owner}/{name}"


_git: Git = FakeGit()
_github: GitHub = FakeGitHub()
