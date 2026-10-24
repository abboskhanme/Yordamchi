"""Yordamchi's git hooks (SPEC §5.7 Layer 2, UC-13, NFR-SEC-02/11): every `githooks/<name>` runs
`"$YORDAMCHI_PY" -I -m yordamchi.bots.cli.githook "$0" "$@"`, and the hook's name picks what happens.

Executor sessions get `core.hooksPath` = the release's `githooks/` through `GIT_CONFIG_*` (SPEC §5.3), so every
git command an agent runs, scripts included, uses these hooks without touching project files. That also hides the
project's own hooks (husky, pre-commit, `.git/hooks/*`) from git, so each hook here ends by running the project's
hook of the same name in its place (exec: the same argv, stdin, environment and exit code; `project_hook`).

`pre-push` first scans the pushed commits. For each pushed ref, every commit not yet on that remote is scanned:
- added or changed files named like a secret: `.env*` (not `.example|.sample|.template`), `*.pem`, `id_rsa*`,
  `*.p12`, `*.keystore`, `credentials*.json`;
- added lines matching a `core.redact` pattern, or holding a value whose sha256 is in `run/secret-hashes.json`
  (`{"sha256": [hex…]}`, written by the vault next to the control socket).

A hit blocks the push (exit 1) with `YORDAMCHI_SECRET_SCAN_BLOCKED file=… line=… rule=…` (never the secret) and
reports `secret_blocked` to the bot, which tells the owner. Only the bot's explicit "not enforced" (the owner
switched `gate.secret_scan_enabled` off) lets a hit through. A scan that cannot run blocks (fail closed). Only a
push that passed the scan reaches the project's own pre-push hook, so no project setting can skip the scan.

The other hooks (`PASS_THROUGH_HOOKS`) only run the project's hook (T18b).

Imports only `yordamchi.core` and the standard library (A9).
"""

import errno
import hashlib
import json
import os
import posixpath
import re
import sys
import tempfile
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Final

from yordamchi.core import control, proc, redact
from yordamchi.core.config import CliConfig, load_cli_config

EXIT_BLOCKED: Final = 1
PRE_PUSH: Final = "pre-push"
# Hooks that only hand over to the project's own hook of the same name (`githooks/<name>` exists for each).
PASS_THROUGH_HOOKS: Final = frozenset(
    {
        "pre-commit", "prepare-commit-msg", "commit-msg", "post-commit", "post-checkout", "post-merge", "pre-rebase",
        "post-rewrite",
    }
)  # fmt: skip
GIT_TIMEOUT_S: Final = 120.0  # one `git log -p` over the pushed commits
GIT_QUERY_TIMEOUT_S: Final = 30.0  # one `git config` or `git rev-parse`
ANSWER_TIMEOUT_S: Final = 30.0
SH: Final = "/bin/sh"  # git runs a hook without a `#!` line with sh
BLOCK_MARKER: Final = "YORDAMCHI_SECRET_SCAN_BLOCKED"
_EXAMPLE_SUFFIXES: Final = (".example", ".sample", ".template")
_NAME_RULES: Final = (
    ("pem_file", "*.pem"),
    ("ssh_key_file", "id_rsa*"),
    ("p12_file", "*.p12"),
    ("keystore_file", "*.keystore"),
    ("credentials_file", "credentials*.json"),
)
_HUNK: Final = re.compile(r"@@ -\S+ \+(\d+)(?:,\d+)? @@")
_COARSE: Final = re.compile(r"[\s\"'`]+")
_FINE: Final = re.compile(r"[=:@,;()\[\]{}<>/]+")
_EDGES: Final = ",;)(}{][<>."


@dataclass(frozen=True, slots=True)
class Hit:
    file: str
    line: int
    rule: str


def name_rule(path: str) -> str | None:
    """The rule a file name breaks, if any (SPEC §5.7)."""
    base = posixpath.basename(path).casefold()
    if base.startswith(".env") and not base.endswith(_EXAMPLE_SUFFIXES):
        return "env_file"
    return next((rule for rule, pattern in _NAME_RULES if fnmatchcase(base, pattern)), None)


def _candidates(text: str) -> Iterator[str]:
    """Every token an exact vault value could be: whitespace/quote-separated words, their trimmed forms, and the
    pieces between `= : @ , ; / ( ) [ ] { } < >` (a value may sit after `KEY=` or inside a URL)."""
    for word in _COARSE.split(text):
        if len(word) < redact.MIN_KNOWN_SECRET_LEN:
            continue
        yield word
        yield word.strip(_EDGES)
        for separator in ("=", ":"):
            if separator in word:
                yield word.split(separator, 1)[1]
        yield from _FINE.split(word)


def line_rule(text: str, hashes: frozenset[str]) -> str | None:
    """The rule an added line breaks: a `core.redact` pattern kind, or `vault_value` for a stored secret."""
    hits = redact.find_secrets(text)
    if hits:
        return hits[0].kind
    if hashes and any(hashlib.sha256(token.encode()).hexdigest() in hashes for token in _candidates(text)):
        return "vault_value"
    return None


def _unquote(path: str) -> str:
    """A path as git prints it: C-quoted when it holds special characters (`core.quotePath=false` keeps UTF-8)."""
    if not (path.startswith('"') and path.endswith('"')):
        return path
    raw = path[1:-1].encode("latin-1", "backslashreplace").decode("unicode_escape")
    return raw.encode("latin-1", "replace").decode("utf-8", "replace")


def _diff_path(header: str) -> str:
    """The new-side path of `diff --git a/P b/P` (renames are off, so both sides are the same path)."""
    rest = header.removeprefix("diff --git ")
    if rest.startswith('"'):
        quoted = re.findall(r'"(?:\\.|[^"\\])*"', rest)
        return _unquote(quoted[-1]).removeprefix("b/") if quoted else rest
    half = (len(rest) - 1) // 2
    return rest[half + 1 :].removeprefix("b/")


def scan_patch(patch: str, hashes: frozenset[str]) -> Hit | None:
    """The first secret in `git log -p --unified=0 --no-renames --diff-filter=d` output."""
    file: str | None = None
    line = 0
    in_header = False
    for raw in patch.split("\n"):
        if raw.startswith("diff --git "):
            file, in_header, line = _diff_path(raw), True, 0
            rule = name_rule(file)
            if rule is not None:
                return Hit(file, 1, rule)
            continue
        if in_header:
            hunk = _HUNK.match(raw)
            if hunk is not None:
                in_header, line = False, int(hunk.group(1))
            continue
        hunk = _HUNK.match(raw)
        if hunk is not None:
            line = int(hunk.group(1))
        elif raw.startswith("+") and file is not None:
            rule = line_rule(raw[1:], hashes)
            if rule is not None:
                return Hit(file, line, rule)
            line += 1
    return None


def load_hashes(path: Path | None) -> frozenset[str] | None:
    """The vault's value hashes, or None when the file is missing or malformed (the scan then cannot run)."""
    if path is None:
        return None
    try:
        document: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    items = document.get("sha256") if isinstance(document, dict) else None
    if not isinstance(items, list) or not all(isinstance(item, str) for item in items):
        return None
    return frozenset(item.casefold() for item in items if isinstance(item, str))


def _git(*args: str, timeout: float = GIT_TIMEOUT_S) -> proc.ProcResult:
    return proc.run_sync(["git", "-c", "core.quotePath=false", *args], timeout=timeout)


def _revisions(remote: str, local_sha: str, remote_sha: str) -> list[str]:
    """The commits a ref update sends: those after the remote's old tip, or, for a new ref (or an old tip this
    clone does not have), all not yet on any of the remote's branches."""
    if set(remote_sha) != {"0"} and _git("cat-file", "-e", f"{remote_sha}^{{commit}}").returncode == 0:
        return [f"{remote_sha}..{local_sha}"]
    return [local_sha, "--not", f"--remotes={remote}"]


def scan_push(remote: str, ref_lines: Sequence[str], hashes: frozenset[str]) -> Hit | None:
    """Scan every commit the push sends. Raises `OSError`/`TimeoutError`/`RuntimeError` when git cannot tell."""
    for ref_line in ref_lines:
        parts = ref_line.split()
        if len(parts) != 4 or set(parts[1]) == {"0"}:  # malformed is skipped by git too; all-zero = a deletion
            continue
        result = _git(
            "log", "-p", "--no-color", "--no-ext-diff", "--no-textconv", "--no-renames", "--diff-filter=d",
            "--unified=0", "--format=%H", "--src-prefix=a/", "--dst-prefix=b/",
            *_revisions(remote, parts[1], parts[3]),
        )  # fmt: skip
        if result.returncode != 0:
            raise RuntimeError("git_log_failed")
        hit = scan_patch(result.stdout.decode("utf-8", errors="replace"), hashes)
        if hit is not None:
            return hit
    return None


def _report(config: CliConfig, hit: Hit) -> control.SecretAnswer | None:
    if config.socket is None:
        return None
    report = control.SecretBlocked(str(Path.cwd()), hit.file, hit.line, hit.rule, config.session_token)
    answer = control.request(config.socket, control.secret_blocked_request(report), timeout=ANSWER_TIMEOUT_S)
    return control.read_secret_answer(answer)


def _block(hit: Hit) -> int:
    sys.stderr.write(
        f"{BLOCK_MARKER} file={hit.file} line={hit.line} rule={hit.rule}\n"
        f"yordamchi: push blocked: {hit.file} line {hit.line} holds a secret ({hit.rule}). Remove it from the "
        "commits being pushed (rewrite them), keep secrets out of git, and push again. The owner was told.\n"
    )
    return EXIT_BLOCKED


# --- the project's own hooks (T18b) ---
def _note(text: str) -> None:
    sys.stderr.write(f"yordamchi: {text}\n")


def _git_answer(*args: str) -> tuple[int, str]:
    """A one-line git answer: the exit code (128 when git could not run) and the line without its newline."""
    try:
        result = _git(*args, timeout=GIT_QUERY_TIMEOUT_S)
    except (OSError, TimeoutError):
        return 128, ""
    return result.returncode, os.fsdecode(result.stdout).removesuffix("\n")


def project_hooks_dir() -> Path | None:
    """The folder git would take the project's hooks from without Yordamchi's override, or None when it has none.

    That is the repo-local `core.hooksPath` (`git config --local`, `~` expanded by git; a relative path is taken from
    the repo root, where git runs hooks), else `<git common dir>/hooks`. An empty `core.hooksPath` turns hooks off,
    as in git. The folder must lie inside the repo root or its git dir once symlinks are followed: a hooks path that
    leads anywhere else is not followed (said on stderr). Run from the hook's working directory (the repo root).
    """
    cwd = Path.cwd()
    code, top = _git_answer("rev-parse", "--show-toplevel")
    root = Path(top) if code == 0 and top else cwd  # a bare repo has no work tree: git runs hooks in the git dir
    code, common = _git_answer("rev-parse", "--git-common-dir")
    if code != 0 or not common:
        return None
    git_dir = cwd / common
    code, configured = _git_answer("config", "--local", "--type=path", "--get", "core.hooksPath")
    # 1 = not set. Set but empty = hooks off. Any other code = git cannot read or expand it (and git itself fails).
    if code not in (0, 1) or (code == 0 and not configured):
        return None
    folder = root / configured if code == 0 else git_dir / "hooks"
    try:
        real = folder.resolve(strict=True)
        inside = real.is_relative_to(root.resolve()) or real.is_relative_to(git_dir.resolve())
    except OSError:
        return None  # no such folder: no hooks
    if not real.is_dir():
        return None  # e.g. `core.hooksPath=/dev/null`, the usual way to turn hooks off
    if not inside:
        _note(f"the project's hooks folder {folder} is outside the repository, so its hooks did not run")
        return None
    return folder


def project_hook(name: str, *, own_hook: Path) -> Path | None:
    """The project's own `name` hook, if it is an executable file. Never `own_hook` (the Yordamchi hook running
    now): a project whose hooks lead back to Yordamchi's would loop."""
    folder = project_hooks_dir()
    if folder is None:
        return None
    hook = folder / name
    try:
        if hook.resolve() == own_hook.resolve():
            return None
    except OSError:
        return None
    return hook if hook.is_file() and os.access(hook, os.X_OK) else None


def _cannot_start(hook: Path, error: OSError) -> int:
    _note(f"the project's {hook.name} hook could not start ({errno.errorcode.get(error.errno or 0, 'error')})")
    return EXIT_BLOCKED


def exec_project_hook(hook: Path, args: Sequence[str]) -> int:
    """Become `hook` (exec): git sees its argv, stdin, environment, process group and exit code as if it had run
    the hook itself. A hook without a `#!` line runs under sh, as git runs it. Returns only when it cannot start."""
    sys.stdout.flush()
    sys.stderr.flush()
    argv = [str(hook), *args]
    try:
        os.execv(hook, argv)  # noqa: S606  # the project's own hook, argv as git gave it
    except OSError as error:
        if error.errno != errno.ENOEXEC:
            return _cannot_start(hook, error)
    try:
        os.execv(SH, [SH, *argv])  # noqa: S606  # git's fallback for a script without `#!`
    except OSError as error:
        return _cannot_start(hook, error)


def pass_through(own_hook: Path, args: Sequence[str]) -> int:
    """A `PASS_THROUGH_HOOKS` hook: the project's hook of the same name, or 0 when it has none (as git without one)."""
    hook = project_hook(own_hook.name, own_hook=own_hook)
    return 0 if hook is None else exec_project_hook(hook, args)


def pre_push(own_hook: Path, args: Sequence[str]) -> int:
    """The secret scan, then, only for a push it lets through, the project's own pre-push hook."""
    if not args:
        return _block(Hit("-", 0, "usage"))
    stdin = sys.stdin.buffer.read()
    config = load_cli_config()
    hashes = load_hashes(config.secret_hashes)
    if hashes is None:
        return _block(Hit("-", 0, "scan_unavailable"))
    try:
        hit = scan_push(args[0], stdin.decode("utf-8", errors="replace").splitlines(), hashes)
    except (OSError, TimeoutError, RuntimeError):
        return _block(Hit("-", 0, "scan_failed"))
    if hit is not None:
        answer = _report(config, hit)
        if answer is None or answer.enforce:
            return _block(hit)
    hook = project_hook(PRE_PUSH, own_hook=own_hook)
    if hook is None:
        return 0
    with tempfile.TemporaryFile() as refs:  # the ref lines git gave, for the project's hook to read in turn
        refs.write(stdin)
        refs.flush()
        refs.seek(0)
        os.dup2(refs.fileno(), sys.stdin.fileno())
    return exec_project_hook(hook, args)


def main(argv: Sequence[str] | None = None) -> int:
    """`args[0]` is the hook git ran (`"$0"`); its name picks the job. Any name but a pass-through one gets the
    pre-push scan, so a mistaken name blocks rather than lets a push through."""
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        return _block(Hit("-", 0, "usage"))
    own_hook = Path(args[0])
    if own_hook.name in PASS_THROUGH_HOOKS:
        return pass_through(own_hook, args[1:])
    return pre_push(own_hook, args[1:])


if __name__ == "__main__":
    raise SystemExit(main())
