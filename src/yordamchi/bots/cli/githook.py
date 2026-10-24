"""The pre-push secret scan (SPEC §5.7 Layer 2, UC-13, NFR-SEC-02/11): `githooks/pre-push` runs
`"$YORDAMCHI_PY" -I -m yordamchi.bots.cli.githook "$0" <remote> <url>` with git's ref lines on stdin.

Executor sessions get `core.hooksPath` = the release's `githooks/` through `GIT_CONFIG_*` (SPEC §5.3), so every
push an agent makes passes here, scripts included, without touching project files. For each pushed ref, every
commit not yet on that remote is scanned:
- added or changed files named like a secret: `.env*` (not `.example|.sample|.template`), `*.pem`, `id_rsa*`,
  `*.p12`, `*.keystore`, `credentials*.json`;
- added lines matching a `core.redact` pattern, or holding a value whose sha256 is in `run/secret-hashes.json`
  (`{"sha256": [hex…]}`, written by the vault next to the control socket).

A hit blocks the push (exit 1) with `YORDAMCHI_SECRET_SCAN_BLOCKED file=… line=… rule=…` (never the secret) and
reports `secret_blocked` to the bot, which tells the owner. Only the bot's explicit "not enforced" (the owner
switched `gate.secret_scan_enabled` off) lets a hit through. A scan that cannot run blocks (fail closed). A clean
push then runs the project's own pre-push hook (`git config --local core.hooksPath`, else `.git/hooks`).

Imports only `yordamchi.core` and the standard library (A9).
"""

import hashlib
import json
import os
import posixpath
import re
import sys
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Final

from yordamchi.core import control, proc, redact
from yordamchi.core.config import CliConfig, load_cli_config

EXIT_BLOCKED: Final = 1
GIT_TIMEOUT_S: Final = 120.0  # one `git log -p` over the pushed commits
PROJECT_HOOK_TIMEOUT_S: Final = 3600.0  # a project's own pre-push may run its test suite
ANSWER_TIMEOUT_S: Final = 30.0
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


def project_hook(own_hook: Path) -> Path | None:
    """The project's own pre-push hook: `core.hooksPath` from the repo's local config, else `<git dir>/hooks`."""
    configured = _git("config", "--local", "--get", "core.hooksPath", timeout=30)
    if configured.returncode == 0 and configured.stdout.strip():
        folder = Path(os.path.expanduser(configured.stdout.decode().strip()))  # noqa: PTH111  # git expands ~ too
        if not folder.is_absolute():
            top = _git("rev-parse", "--show-toplevel", timeout=30).stdout.decode().strip()
            folder = Path(top) / folder
    else:
        common = _git("rev-parse", "--git-common-dir", timeout=30).stdout.decode().strip()
        folder = Path(common or ".git") / "hooks"
    hook = folder / "pre-push"
    try:
        if hook.resolve() == own_hook.resolve():
            return None  # the repo points back at Yordamchi's hooks: do not loop
    except OSError:
        return None
    return hook if hook.is_file() and os.access(hook, os.X_OK) else None


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) < 2:
        return _block(Hit("-", 0, "usage"))
    own_hook, remote, rest = Path(args[0]), args[1], args[2:]
    stdin = sys.stdin.buffer.read()
    config = load_cli_config()
    hashes = load_hashes(config.secret_hashes)
    if hashes is None:
        return _block(Hit("-", 0, "scan_unavailable"))
    try:
        hit = scan_push(remote, stdin.decode("utf-8", errors="replace").splitlines(), hashes)
    except (OSError, TimeoutError, RuntimeError):
        return _block(Hit("-", 0, "scan_failed"))
    if hit is not None:
        answer = _report(config, hit)
        if answer is None or answer.enforce:
            return _block(hit)
    hook = project_hook(own_hook)
    if hook is None:
        return 0
    chained = proc.run_sync([str(hook), remote, *rest], timeout=PROJECT_HOOK_TIMEOUT_S, stdin=stdin, capture=False)
    return chained.returncode


if __name__ == "__main__":
    raise SystemExit(main())
