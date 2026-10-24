"""The gate, pure (SPEC §5.7, §3.2.3; UC-13, UC-15, NFR-SEC-06, NFR-SEC-11): classifier, policies, approvals, grants.

Flow (the service owns every fetch; this module only decides):
1. `needs(call, rules, ctx)` lists the facts the call depends on: untracked files, branches, deploy branches, the
   project's DB hosts, git remote URLs, whether the tree was dirty when the turn started, and script recipes
   (`./x.sh`, `make t`, `npm run s`, `just r`). The service fetches them into a new `GateContext` and calls `needs`
   again until it returns nothing new (recipes nest at most two levels). The parsers for the files behind recipes
   and DB hosts are pure and live at the end of this module; the service only reads the bytes.
2. `classify(call, rules, ctx)` → `GateVerdict(allow | ask | forbid)`. A fact that is still missing fails closed.
3. On `ask`: `grant_step(...)` picks the approved grant, the pending decision or a new question for the exact
   `(project_id, tool_name, input_sha256)`.
4. The owner's reply: `bind_approval(...)` decides which decision a "ha" / "yo'q" belongs to, and
   `resolve_approval(...)` turns it into a single-use grant with an expiry, or a fresh question if it expired.
5. Layer 2 (`shims/ssh` …): `shim_verdict(...)` allows a read-only remote command or one covered by a grant
   consumed moments ago in the same session.

Shell parsing is a small POSIX scanner rather than `shlex`: `shlex` drops quoting, cannot nest `$(…)` and does not
know heredocs, and all three decide whether text is data or a command. It splits on `; && || | & newline` like
`shlex` with `punctuation_chars`, keeps env-prefix assignments as facts, and recurses into shells (`-c`, heredocs,
`eval`), wrappers (`env`, `sudo`, `nohup`, `time`, `timeout`, `xargs`, package runners) and substitutions. Text it
cannot parse is keyword-scanned and the strongest class wins: an extra question is acceptable, a miss is not.

Policies (SPEC §5.3, §5.7, §3.2.8): FULL (coding); READ_ONLY (investigation: no write tool, Bash and remote commands
only from the read-only lists, nothing is ever asked, it is denied); PLAN_ONLY (writes only to `docs/**`,
`README.md`, `.gitignore`; Bash read-only plus `git add|commit|push`); SELF_UPDATE (FULL, and pushing or merging
into `main` of Yordamchi is forbidden: the supervisor merges).
"""

import hashlib
import posixpath
import re
import shlex
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from fnmatch import fnmatchcase
from typing import Final, Literal

from yordamchi.core.json_value import JsonValue, dumps
from yordamchi.core.paths import LAUNCHD_PREFIX
from yordamchi.dev.projects.domain import FAIL_CLOSED, DeployBranches
from yordamchi.ports.agent import ToolCall


# --- vocabulary ----------------------------------------------------------------------------------------------------
class GateClass(StrEnum):
    """The twelve UC-13 ask classes (`gate.ask_classes`); the order is SPEC §5.7's."""

    DEPLOY = "deploy"
    SERVER_WRITE = "server_write"
    DEPLOY_PUSH = "deploy_push"
    FORCE_PUSH = "force_push"
    DATA_LOSS_DB = "data_loss_db"
    DOCKER_DESTROY = "docker_destroy"
    FS_DESTROY = "fs_destroy"
    REPO_DELETE = "repo_delete"
    REPO_PUBLIC = "repo_public"
    SPEND = "spend"
    THIRD_PARTY_SEND = "third_party_send"
    ARCHITECTURE = "architecture"  # by prompt instruction only (AskUserQuestion); never detected here


# One question per call: when a call triggers several classes, the most irreversible one is asked about.
CLASS_SEVERITY: Final = (
    GateClass.REPO_DELETE,
    GateClass.DATA_LOSS_DB,
    GateClass.DOCKER_DESTROY,
    GateClass.FS_DESTROY,
    GateClass.FORCE_PUSH,
    GateClass.DEPLOY,
    GateClass.DEPLOY_PUSH,
    GateClass.SERVER_WRITE,
    GateClass.REPO_PUBLIC,
    GateClass.SPEND,
    GateClass.THIRD_PARTY_SEND,
    GateClass.ARCHITECTURE,
)


class Policy(StrEnum):
    """`agent_sessions.policy` (SPEC §4.2)."""

    FULL = "FULL"
    READ_ONLY = "READ_ONLY"
    PLAN_ONLY = "PLAN_ONLY"
    SELF_UPDATE = "SELF_UPDATE"


class Action(StrEnum):
    ALLOW = "allow"
    ASK = "ask"
    FORBID = "forbid"


class Reason(StrEnum):
    """Stable snake_case codes: audit `data_json`, the deny prompt slot and the owner's question key on them."""

    FREE = "free"
    # forbidden, never askable
    FORBIDDEN_PATH = "forbidden_path"
    KEYCHAIN = "keychain"
    BOT_CONTROL = "bot_control"
    YORDAMCHI_SOCKET = "yordamchi_socket"
    PUSH_NO_VERIFY = "push_no_verify"
    HOOKS_BYPASS = "hooks_bypass"
    CREDENTIAL_ENV = "credential_env"
    SELF_UPDATE_MAIN = "self_update_main"
    READ_ONLY_POLICY = "read_only_policy"
    PLAN_ONLY_POLICY = "plan_only_policy"
    # ask
    DEPLOY_CLI = "deploy_cli"
    DEPLOY_TARGET = "deploy_target"
    REMOTE_COMMAND = "remote_command"
    JUMP_ONLY_HOST = "jump_only_host"
    REMOTE_DOCKER = "remote_docker"
    REMOTE_COPY = "remote_copy"
    PUSH_TO_SERVER = "push_to_server"  # a git push whose remote is a server, not a git host
    DEPLOY_BRANCH = "deploy_branch"
    FORCE_DEFAULT_BRANCH = "force_default_branch"
    SQL_DESTRUCTIVE = "sql_destructive"
    DROP_DATABASE = "drop_database"
    MIGRATION_DESTRUCTIVE = "migration_destructive"
    MIGRATION_REMOTE_DB = "migration_remote_db"
    DOCKER_VOLUMES = "docker_volumes"
    DELETE_PROJECT_ROOT = "delete_project_root"
    DELETE_OUTSIDE_PROJECT = "delete_outside_project"
    DELETE_UNTRACKED = "delete_untracked"
    DELETE_GIT_DIR = "delete_git_dir"
    DELETE_UNKNOWN = "delete_unknown"
    DISCARD_CHANGES = "discard_changes"  # reset --hard, checkout -- ., restore, stash drop over pre-turn edits
    REPO_DELETE = "repo_delete"
    REPO_PUBLIC = "repo_public"
    CLOUD_SPEND = "cloud_spend"
    PACKAGE_PUBLISH = "package_publish"
    MAIL = "mail"
    HTTP_WRITE = "http_write"
    MCP_WRITE = "mcp_write"
    OPAQUE_COMMAND = "opaque_command"  # the program name is only known at run time
    UNPARSEABLE = "unparseable"  # keyword scan of text the scanner rejected
    RECIPE_UNKNOWN = "recipe_unknown"  # a script whose text the service did not supply


@dataclass(frozen=True, slots=True)
class Finding:
    """One reason a call cannot simply run. `gate_class` is None only for parse-safety asks (opaque, unknown)."""

    action: Literal[Action.ASK, Action.FORBID]
    reason: Reason
    target: str = ""
    gate_class: GateClass | None = None


@dataclass(frozen=True, slots=True)
class GateVerdict:
    """`classify()`'s answer. On ask, `gate_class`/`reason`/`target` name the one question (strongest finding)."""

    action: Action
    reason: Reason
    target: str = ""
    gate_class: GateClass | None = None
    findings: tuple[Finding, ...] = ()

    @property
    def allowed(self) -> bool:
        return self.action is Action.ALLOW


ALLOW: Final = GateVerdict(Action.ALLOW, Reason.FREE)


# --- inputs --------------------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class GateRules:
    """The `gate.*` setting values of this turn's snapshot (SPEC §4.3 DECISIONS)."""

    ask_classes: frozenset[GateClass]
    readonly_remote_commands: tuple[str, ...]
    jump_only_hosts: frozenset[str]
    git_hosts: frozenset[str]
    http_write_allow_hosts: frozenset[str]
    temp_roots: tuple[str, ...]

    @classmethod
    def from_settings(
        cls,
        *,
        ask_classes: Iterable[str],
        readonly_remote_commands: Iterable[str],
        jump_only_hosts: Iterable[str],
        git_hosts: Iterable[str],
        http_write_allow_hosts: Iterable[str],
        temp_roots: Iterable[str],
    ) -> "GateRules":
        known = {item.value for item in GateClass}
        return cls(
            ask_classes=frozenset(GateClass(name) for name in ask_classes if name in known),
            readonly_remote_commands=tuple(readonly_remote_commands),
            jump_only_hosts=frozenset(host.lower() for host in jump_only_hosts),
            git_hosts=frozenset(host.lower() for host in git_hosts),
            http_write_allow_hosts=frozenset(host.lower() for host in http_write_allow_hosts),
            temp_roots=tuple(_canonical(root) for root in temp_roots),
        )


type ScriptKind = Literal["file", "make", "npm", "just"]


@dataclass(frozen=True, slots=True)
class ScriptRef:
    """A script whose text decides the class: a shell file (absolute path), or a make/npm/just target in a directory.

    `name=""` for make is the default goal. The service supplies the text in `GateContext.recipes`: for make, the
    target's recipe lines (make variables may stay unexpanded); for npm, the `package.json` script string.
    """

    kind: ScriptKind
    name: str
    directory: str


# The `GateContext.remote_urls` key of the remote a bare `git push` uses (the current branch's upstream remote, else
# `origin`). `@` alone is not a valid remote name, so it never collides with one.
DEFAULT_PUSH_REMOTE: Final = "@"


@dataclass(frozen=True, slots=True)
class GateContext:
    """Facts about one call. `None` (or a missing recipe or remote key) = not fetched yet; `needs()` says which.

    Paths are absolute POSIX strings. `untracked` holds project-relative files from
    `git ls-files -o --exclude-standard`; `db_hosts` the hosts of the project's configured DB URLs (`()` = none
    configured, so tools use their local default); `deploy_branches` the pushes that start a deploying CI workflow
    (`dev/projects`, read fresh for the pushed refs; `FAIL_CLOSED` when the workflows could not be read).
    `dirty_before_turn`: the tree held uncommitted changes when the turn started (the owner's own edits, which a
    discarding command would destroy). `remote_urls`: git remote name → its URL (credential-free; None = no such
    remote), with `DEFAULT_PUSH_REMOTE` for a bare `git push`.
    """

    policy: Policy
    home: str
    cwd: str
    project_root: str
    data_dir: str
    config_dir: str
    default_branch: str | None = None
    current_branch: str | None = None
    deploy_branches: DeployBranches | None = None
    untracked: frozenset[str] | None = None
    db_hosts: tuple[str, ...] | None = None
    dirty_before_turn: bool | None = None
    remote_urls: Mapping[str, str | None] = field(default_factory=dict)
    recipes: Mapping[ScriptRef, str | None] = field(default_factory=dict)
    protected_pids: frozenset[int] = frozenset()
    mcp_read_only_tools: frozenset[str] = frozenset()
    mcp_preallowed_tools: frozenset[str] = frozenset()  # exact names or `mcp__<server>__*`


@dataclass(frozen=True, slots=True)
class GateNeeds:
    """Facts `classify` would use but `ctx` lacks. `deploy_refs` are the local refs the call's pushes send (`HEAD` for
    a bare push): the workflows committed there decide `deploy_branches`, so they matter only when that is listed."""

    untracked: bool = False
    default_branch: bool = False
    current_branch: bool = False
    deploy_branches: bool = False
    db_hosts: bool = False
    dirty_before_turn: bool = False
    remotes: tuple[str, ...] = ()
    scripts: tuple[ScriptRef, ...] = ()
    deploy_refs: tuple[str, ...] = ()

    @property
    def empty(self) -> bool:
        return not (
            self.untracked
            or self.default_branch
            or self.current_branch
            or self.deploy_branches
            or self.db_hosts
            or self.dirty_before_turn
            or self.remotes
            or self.scripts
        )


def _words(text: str) -> frozenset[str]:
    """A set from a space-separated table (CLI subcommands and options read best as one line of words)."""
    return frozenset(text.split())


def _canonical(path: str) -> str:
    """Normalised, with macOS's `/tmp`, `/var`, `/etc` → `/private/…` so both spellings compare equal."""
    normal = posixpath.normpath(path) if path else path
    for short in ("/tmp", "/var", "/etc"):  # noqa: S108  # data: macOS symlinks these to /private/…
        if normal == short or normal.startswith(short + "/"):
            return "/private" + normal
    return normal


# --- shell scanner -------------------------------------------------------------------------------------------------
# Markers inside `_Word.text`: a substitution's output, a variable reference `\x01NAME\x02`, an unquoted `~`.
_SUBST: Final = "\x00"
_VAR_OPEN: Final = "\x01"
_VAR_CLOSE: Final = "\x02"
_TILDE: Final = "\x03"
_UNKNOWN_VAR: Final = "?"
_MAX_DEPTH: Final = 8
_METACHARS: Final = frozenset(" \t\n;&|()<>")
_REDIRECT_OPS: Final = ("&>>", "&>", "<<<", "<<-", "<<", "<>", "<&", ">>", ">|", ">&", "<", ">")
_WRITE_OPS: Final = frozenset({">", ">>", ">|", "<>", "&>", "&>>", ">&"})
_NAME: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_ANSI_C_STRING: Final = re.compile(r"\$'((?:\\.|[^'\\])*)'", re.DOTALL)
_ANSI_C_ESCAPE: Final = re.compile(r"\\(x[0-9A-Fa-f]{1,2}|u[0-9A-Fa-f]{1,4}|[0-7]{1,3}|.)", re.DOTALL)
_ANSI_SIMPLE: Final = {"n": "\n", "t": "\t", "r": "\r", "a": "\a", "b": "\b", "e": "\x1b", "f": "\f", "v": "\v"}


class _Unparseable(Exception):
    """Unbalanced quotes or parentheses, or nesting too deep: the caller falls back to the keyword scan."""


@dataclass(slots=True)
class _HeredocBody:
    text: str = ""


@dataclass(frozen=True, slots=True)
class _Word:
    text: str  # de-quoted, with markers
    raw: str  # as written
    subs: tuple["_Script", ...] = ()
    glob: bool = False  # unquoted `*`, `?`, `[` or a brace expansion

    @property
    def literal(self) -> str | None:
        """The value, if it is fully known before the command runs."""
        if any(marker in self.text for marker in (_SUBST, _VAR_OPEN, _TILDE)):
            return None
        return self.text


@dataclass(frozen=True, slots=True)
class _Redirect:
    op: str
    target: _Word
    heredoc: _HeredocBody | None = None

    @property
    def writes_file(self) -> bool:
        if self.op not in _WRITE_OPS:
            return False
        value = self.target.literal
        return not (self.op == ">&" and value is not None and (value.isdigit() or value == "-"))

    @property
    def stdin_text(self) -> str | None:
        """What the command reads on stdin when the shell supplies it: a heredoc body or a here-string."""
        if self.heredoc is not None:
            return self.heredoc.text
        if self.op == "<<<":
            return self.target.text
        return None


@dataclass(frozen=True, slots=True)
class _Command:
    words: tuple[_Word, ...]
    redirects: tuple[_Redirect, ...] = ()
    piped: bool = False  # stdin is the previous pipeline stage


type _Item = _Command | Literal["(", ")"]
type _Script = tuple[_Item, ...]


def _decode_ansi_c(body: str) -> str:
    def one(match: re.Match[str]) -> str:
        code = match.group(1)
        if code[0] in "xu" and len(code) > 1:
            return chr(int(code[1:], 16))
        if code[0] in "01234567":
            return chr(int(code, 8) & 0xFF)
        return _ANSI_SIMPLE.get(code, code)

    return _ANSI_C_ESCAPE.sub(one, body)


@dataclass(slots=True)
class _Builder:
    words: list[_Word] = field(default_factory=list)
    redirects: list[_Redirect] = field(default_factory=list)
    piped: bool = False

    def flush(self, items: list[_Item], *, piped_next: bool) -> None:
        if self.words or self.redirects:
            items.append(_Command(tuple(self.words), tuple(self.redirects), self.piped))
        self.words, self.redirects, self.piped = [], [], piped_next


class _Scanner:
    def __init__(self, text: str, start: int = 0, depth: int = 0) -> None:
        if depth > _MAX_DEPTH:
            raise _Unparseable
        self.s = text
        self.i = start
        self.depth = depth
        self.heredocs: list[tuple[str, bool, _HeredocBody]] = []

    def script(self, *, closing: bool = False) -> _Script:  # noqa: PLR0912, PLR0915  # one branch per metachar
        items: list[_Item] = []
        current = _Builder()
        groups = 0
        s = self.s
        while True:
            if self.i >= len(s):
                if closing or groups:
                    raise _Unparseable
                current.flush(items, piped_next=False)
                self._read_heredoc_bodies()
                return tuple(items)
            ch = s[self.i]
            if ch in " \t":
                self.i += 1
            elif s.startswith("\\\n", self.i):
                self.i += 2
            elif ch == "\n":
                self.i += 1
                current.flush(items, piped_next=False)
                self._read_heredoc_bodies()
            elif ch == "#":  # only reached at a word boundary
                end = s.find("\n", self.i)
                self.i = len(s) if end < 0 else end
            elif ch == ";":
                self.i += 2 if s.startswith((";;", ";&"), self.i) else 1
                current.flush(items, piped_next=False)
            elif ch == "&" and not s.startswith("&>", self.i):
                self.i += 2 if s.startswith("&&", self.i) else 1
                current.flush(items, piped_next=False)
            elif ch == "|":
                is_or = s.startswith("||", self.i)
                self.i += 2 if is_or or s.startswith("|&", self.i) else 1
                current.flush(items, piped_next=not is_or)
            elif ch == "(":
                self.i += 1
                current.flush(items, piped_next=False)
                items.append("(")
                groups += 1
            elif ch == ")":
                self.i += 1
                current.flush(items, piped_next=False)
                if groups:
                    groups -= 1
                    items.append(")")
                elif closing:
                    return tuple(items)
                else:
                    raise _Unparseable
            elif ch in "<>&" and not s.startswith(("<(", ">("), self.i):
                self._redirect(current)
            else:
                start = self.i
                word = self._word()
                if self.i == start:
                    raise _Unparseable
                fd_prefix = word.raw.isdigit() and self.i < len(s) and s[self.i] in "<>"
                if fd_prefix and not s.startswith(("<(", ">("), self.i):
                    self._redirect(current)  # `2>file`: the digits are a file descriptor
                else:
                    current.words.append(word)

    # --- words ---
    def _word(self) -> _Word:  # noqa: PLR0912  # one branch per quoting form
        s = self.s
        start = self.i
        out: list[str] = []
        subs: list[_Script] = []
        glob = False
        if s.startswith(("<(", ">("), self.i):  # process substitution
            self.i += 2
            subs.append(self._nested())
            out.append(_SUBST)
        while self.i < len(s):
            ch = s[self.i]
            if ch in _METACHARS:
                break
            if ch == "\\":
                if self.i + 1 < len(s) and s[self.i + 1] != "\n":
                    out.append(s[self.i + 1])
                self.i += 2
            elif ch == "'":
                end = s.find("'", self.i + 1)
                if end < 0:
                    raise _Unparseable
                out.append(s[self.i + 1 : end])
                self.i = end + 1
            elif ch == '"':
                self.i += 1
                self._double_quoted(out, subs)
            elif ch == "$":
                self._dollar(out, subs, quoted=False)
            elif ch == "`":
                self._backtick(out, subs)
            elif ch == "~" and (self.i == start or s[self.i - 1] in "=:"):
                out.append(_TILDE)
                self.i += 1
            else:
                if ch in "*?[":
                    glob = True
                elif ch == "{":
                    close = s.find("}", self.i)
                    inner = s[self.i : close] if close > 0 else ""
                    glob = glob or "," in inner or ".." in inner
                out.append(ch)
                self.i += 1
        return _Word("".join(out), s[start : self.i], tuple(subs), glob)

    def _double_quoted(self, out: list[str], subs: list[_Script]) -> None:
        s = self.s
        while self.i < len(s):
            ch = s[self.i]
            if ch == '"':
                self.i += 1
                return
            if ch == "\\" and self.i + 1 < len(s):
                nxt = s[self.i + 1]
                if nxt in '$`"\\':
                    out.append(nxt)
                elif nxt != "\n":
                    out.append(ch + nxt)
                self.i += 2
            elif ch == "$":
                self._dollar(out, subs, quoted=True)
            elif ch == "`":
                self._backtick(out, subs)
            else:
                out.append(ch)
                self.i += 1
        raise _Unparseable

    def _dollar(self, out: list[str], subs: list[_Script], *, quoted: bool) -> None:
        s = self.s
        nxt = s[self.i + 1] if self.i + 1 < len(s) else ""
        if nxt == "'" and not quoted:  # ANSI-C quoting, decoded: `$'\x73sh'` is `ssh`
            match = _ANSI_C_STRING.match(s, self.i)
            if match is None:
                raise _Unparseable
            out.append(_decode_ansi_c(match.group(1)))
            self.i = match.end()
        elif nxt == '"' and not quoted:  # locale string: an ordinary double-quoted string
            self.i += 1
        elif s.startswith("$((", self.i):
            end = s.find("))", self.i)
            if end < 0 or "$(" in s[self.i + 3 : end] or "`" in s[self.i + 3 : end]:
                raise _Unparseable
            out.append(f"{_VAR_OPEN}{_UNKNOWN_VAR}{_VAR_CLOSE}")
            self.i = end + 2
        elif nxt == "(":
            self.i += 2
            subs.append(self._nested())
            out.append(_SUBST)
        elif nxt == "{":
            end = s.find("}", self.i)
            inner = s[self.i + 2 : end] if end > 0 else ""
            if end < 0 or "$(" in inner or "`" in inner:
                raise _Unparseable
            name = inner if _NAME.fullmatch(inner) else _UNKNOWN_VAR
            out.append(f"{_VAR_OPEN}{name}{_VAR_CLOSE}")
            self.i = end + 1
        elif (match := _NAME.match(s, self.i + 1)) is not None:
            out.append(f"{_VAR_OPEN}{match.group(0)}{_VAR_CLOSE}")
            self.i = match.end()
        elif nxt and nxt in "@*#?$!-0123456789":
            out.append(f"{_VAR_OPEN}{_UNKNOWN_VAR}{_VAR_CLOSE}")
            self.i += 2
        else:
            out.append("$")
            self.i += 1

    def _backtick(self, out: list[str], subs: list[_Script]) -> None:
        s = self.s
        j = self.i + 1
        inner: list[str] = []
        while j < len(s) and s[j] != "`":
            if s[j] == "\\" and j + 1 < len(s) and s[j + 1] in "`$\\":
                inner.append(s[j + 1])
                j += 2
            else:
                inner.append(s[j])
                j += 1
        if j >= len(s):
            raise _Unparseable
        subs.append(_Scanner("".join(inner), 0, self.depth + 1).script())
        out.append(_SUBST)
        self.i = j + 1

    def _nested(self) -> _Script:
        scanner = _Scanner(self.s, self.i, self.depth + 1)
        script = scanner.script(closing=True)
        self.i = scanner.i
        self.heredocs.extend(scanner.heredocs)  # a heredoc whose body starts after the `)`
        return script

    # --- redirections and heredocs ---
    def _redirect(self, current: _Builder) -> None:
        s = self.s
        op = next((candidate for candidate in _REDIRECT_OPS if s.startswith(candidate, self.i)), None)
        if op is None:
            raise _Unparseable
        self.i += len(op)
        while self.i < len(s) and s[self.i] in " \t":
            self.i += 1
        if self.i >= len(s) or s[self.i] in _METACHARS:
            raise _Unparseable
        target = self._word()
        if op in ("<<", "<<-"):
            body = _HeredocBody()
            self.heredocs.append((target.text.replace(_TILDE, "~"), op == "<<-", body))
            current.redirects.append(_Redirect(op, target, heredoc=body))
        else:
            current.redirects.append(_Redirect(op, target))

    def _read_heredoc_bodies(self) -> None:
        s = self.s
        for delimiter, strip_tabs, body in self.heredocs:
            lines: list[str] = []
            while self.i < len(s):
                end = s.find("\n", self.i)
                line = s[self.i :] if end < 0 else s[self.i : end]
                self.i = len(s) if end < 0 else end + 1
                if (line.lstrip("\t") if strip_tabs else line) == delimiter:
                    break
                lines.append(line)
            body.text = "\n".join(lines)
        self.heredocs.clear()


def _parse(text: str) -> _Script:
    """The commands of `text`, or `_Unparseable`."""
    return _Scanner(text).script()


# --- the walk: shared vocabulary -----------------------------------------------------------------------------------
class _Effect(StrEnum):
    """What a command does to the machine, for READ_ONLY and PLAN_ONLY."""

    READ = "read"
    WRITE = "write"
    GIT_WRITE = "git_write"  # `git add|commit|push`: allowed while planning
    PLAN_WRITE = "plan_write"  # `mkdir`/`touch` inside the planning paths
    TRANSPARENT = "transparent"  # a wrapper or keyword: its inner commands are judged themselves


_RECIPE_LEVELS: Final = 2
_TMPDIR_PATH: Final = "/private/var/folders/tmpdir"  # `$TMPDIR` on macOS lives under /private/var/folders
_MKTEMP_PATH: Final = "/private/var/folders/mktemp"  # the value of `$(mktemp …)`
_MAKE_VAR_PREFIX: Final = "__make_"
_DEV_SINKS: Final = frozenset({"/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tty", "/dev/fd/1", "/dev/fd/2"})
_LOCAL_HOSTS: Final = frozenset({"localhost", "127.0.0.1", "::1", "[::1]", "0.0.0.0", "host.docker.internal", ""})  # noqa: S104  # data
_RESERVED_LEADING: Final = frozenset({"if", "then", "else", "elif", "do", "while", "until", "!", "{", "}"})
_RESERVED_ALONE: Final = frozenset({"fi", "done", "esac", "}", "{", "then", "else", "do"})
_RESERVED_LISTS: Final = frozenset({"for", "case", "select", "function", "in"})
_DEPLOY_NAME: Final = re.compile(r"(?i)^(deploy|release|publish|prod)")
_FORBIDDEN_ENV_PREFIXES: Final = ("ANTHROPIC_", "CLAUDE_")  # covers the core.proc refuse-list (NFR-SEC-04)
_GIT_CONFIG_ENV: Final = re.compile(r"GIT_CONFIG(_COUNT|_KEY_\d+|_VALUE_\d+|_PARAMETERS)?$")
_SOCKET_VARS: Final = ("YORDAMCHI_SOCK", "YORDAMCHI_SESSION_TOKEN", "YORDAMCHI_PY")
_SOCKET_MODULES: Final = re.compile(r"yordamchi\.(shim|githook|helper|bots\.cli)\b")
_BOT_PROCESS_WORDS: Final = ("yordamchi", LAUNCHD_PREFIX, "python", ".venv")


def _strip_markers(text: str) -> str:
    return re.sub(f"{_VAR_OPEN}([^{_VAR_CLOSE}]*){_VAR_CLOSE}", r"$\1", text).replace(_SUBST, "$(…)")


def _split_marker(text: str) -> tuple[str, str, str]:
    """`(before, marker, after)` at the first substitution or variable marker; marker `""` when there is none."""
    positions = [index for index in (text.find(_SUBST), text.find(_VAR_OPEN)) if index >= 0]
    if not positions:
        return text, "", ""
    index = min(positions)
    return text[:index], text[index], text[index + 1 :]


def _is_assignment(word: _Word) -> bool:
    head, sep, _ = word.text.partition("=")
    return bool(sep) and _NAME.fullmatch(head) is not None and word.raw.startswith(head)


def _host_of(destination: str) -> str:
    """`user@host:port`, `ssh://user@host:22/path`, `host:path` → `host` (lower case)."""
    rest = destination.split("://", 1)[1] if "://" in destination else destination
    rest = rest.rsplit("@", 1)[-1]
    if rest.startswith("["):
        return rest[: rest.find("]") + 1].lower()
    return re.split(r"[:/]", rest, maxsplit=1)[0].lower()


def _scp_remote(arg: str) -> str | None:
    """The host of an scp/rsync operand that names a remote, else None (`./a:b` and `/x:y` are local)."""
    if re.match(r"(?i)(scp|sftp|rsync|ssh)://", arg):
        return _host_of(arg)
    head, sep, _ = arg.partition(":")
    if not sep or not head or "/" in head:
        return None
    return _host_of(head)


@dataclass(frozen=True, slots=True)
class _Pattern:
    """One `gate.readonly_remote_commands` entry: `N` = a number, `<x>` = one name, `/var/log/<path>` = under it."""

    tokens: tuple[str, ...]

    def matches(self, argv: Sequence[str]) -> bool:
        if len(argv) != len(self.tokens):
            return False
        return all(_part_matches(part, arg) for part, arg in zip(self.tokens, argv, strict=True))


def _part_matches(part: str, arg: str) -> bool:
    if part == "N":
        return arg.isdigit()
    if "<" in part and part.endswith(">"):
        prefix = part[: part.index("<")]
        rest = arg[len(prefix) :]
        if not arg.startswith(prefix) or not rest or rest.startswith("-") or ".." in rest.split("/"):
            return False
        return part[part.index("<") :] == "<path>" or "/" not in rest
    return part == arg


# Keyword scan for text the scanner rejected, inline interpreter code and heredocs into a shell (casefolded input).
_FORBIDDEN_KEYWORDS: Final = (
    (re.compile(r"\bsecurity\s+\S*(password|dump-keychain|export|unlock-keychain)"), Reason.KEYCHAIN),
    (re.compile(re.escape(LAUNCHD_PREFIX.casefold())), Reason.BOT_CONTROL),
    (re.compile(r"\bpush\b[^\n]*--no-verify"), Reason.PUSH_NO_VERIFY),
    (re.compile(r"core\.hookspath|\bgit_config_(count|key_|value_|parameters)"), Reason.HOOKS_BYPASS),
    (re.compile(r"\b(anthropic|claude)_[a-z0-9_]*\s*="), Reason.CREDENTIAL_ENV),
    (re.compile(r"yordamchi[-_](sock|session_token|py)\b|yordamchi-helper"), Reason.YORDAMCHI_SOCKET),
)
_ASK_KEYWORDS: Final = (
    (
        re.compile(
            r"\b(vercel|netlify\s+deploy|flyctl?\s+(deploy|scale)|railway\s+up|heroku|gcloud\s+(run|app)\s+deploy|"
            r"kubectl\s+(apply|delete|rollout|scale|set|patch|edit|replace|create)|"
            r"helm\s+(install|upgrade|rollback|uninstall)|(terraform|tofu)\s+(apply|destroy)|pulumi\s+(up|destroy)|"
            r"ansible-playbook|docker\s+push|eas\s+(submit|update)|fastlane|gh\s+workflow\s+run|gh\s+release\s+create|"
            r"firebase\s+deploy|wrangler\s+(deploy|publish)|cdk\s+(deploy|destroy)|(serverless|sls)\s+deploy|"
            r"aws\s+(deploy|ecs|lambda\s+update))\b"
        ),
        GateClass.DEPLOY,
        Reason.DEPLOY_CLI,
    ),
    (
        re.compile(r"\b(make|npm|pnpm|yarn|just|bun)\s+(run\s+)?(deploy|release|publish|prod)"),
        GateClass.DEPLOY,
        Reason.DEPLOY_TARGET,
    ),
    (re.compile(r"\b(ssh|scp|sftp|rsync|mosh|paramiko|fabric)\b"), GateClass.SERVER_WRITE, Reason.REMOTE_COMMAND),
    (
        re.compile(r"docker_host|-h\s*ssh://|--host[= ]ssh://|docker\s+--context"),
        GateClass.SERVER_WRITE,
        Reason.REMOTE_DOCKER,
    ),
    (re.compile(r"\bgit\s+push\b|\bgh\s+pr\s+merge\b"), GateClass.DEPLOY_PUSH, Reason.DEPLOY_BRANCH),
    (re.compile(r"\bpush\b[^\n]*(\s-f\b|--force|\s\+\w)"), GateClass.FORCE_PUSH, Reason.FORCE_DEFAULT_BRANCH),
    (
        re.compile(
            r"\b(drop\s+(table|database|schema|view|index|collection|column|user|role)|truncate\s+(table\s+)?\w|"
            r"dropdb|flushall|flushdb|dropdatabase\s*\(|deletemany\s*\(\s*\{\s*\}\s*\)|downgrade|"
            r"migrate:(reset|fresh|rollback|refresh)|db:(drop|reset|purge|rollback|schema:load)|migrate\s+reset)"
        ),
        GateClass.DATA_LOSS_DB,
        Reason.SQL_DESTRUCTIVE,
    ),
    (
        re.compile(r"\b(down\s+(-v|--volumes)|volume\s+(rm|prune)|system\s+prune)"),
        GateClass.DOCKER_DESTROY,
        Reason.DOCKER_VOLUMES,
    ),
    (
        re.compile(r"\b(rm|rmdir|unlink|shred|srm)\s|-delete\b|git\s+clean|rmtree|os\.remove|\.unlink\(|rimraf"),
        GateClass.FS_DESTROY,
        Reason.DELETE_UNKNOWN,
    ),
    (
        re.compile(r"gh\s+repo\s+delete|(-x\s*|--method\s+)delete[^\n]*repos/"),
        GateClass.REPO_DELETE,
        Reason.REPO_DELETE,
    ),
    (
        re.compile(r"--public\b|visibility[\s=:\"]+public|\"private\"\s*:\s*false"),
        GateClass.REPO_PUBLIC,
        Reason.REPO_PUBLIC,
    ),
    (
        re.compile(
            r"run-instances|\bcompute\s+[\w-]+\s+create\b|\b(doctl|hcloud|linode-cli|vultr-cli)\b[^\n]*\bcreate\b"
        ),
        GateClass.SPEND,
        Reason.CLOUD_SPEND,
    ),
    (
        re.compile(
            r"\b(sendmail|msmtp|swaks|mailx|mutt|smtplib|telegram-send)\b|\bmail\s+-s\b|"
            r"\b(npm|twine|cargo|gem|yarn|pnpm|poetry|uv|bun)\s+(publish|upload|push)\b"
        ),
        GateClass.THIRD_PARTY_SEND,
        Reason.PACKAGE_PUBLISH,
    ),
    (
        re.compile(
            r"\b(curl|wget|http|https|xh)\b[^\n]*(-x\s*(post|put|patch|delete)|--data|\s-d\s|--json|\s-f\s|--form|"
            r"--post|--method)|requests\.(post|put|patch|delete)|method\s*:\s*[\"']?(post|put|patch|delete)"
        ),
        GateClass.THIRD_PARTY_SEND,
        Reason.HTTP_WRITE,
    ),
)


def _make_recipe(text: str, kind: ScriptKind) -> str:
    """Make/just recipe lines as shell text: continuations joined, `@-+` dropped, `$$` → `$`, `$(shell x)` → `$(x)`,
    and every other make (or just `{{…}}`) variable → a shell variable `${__make_NAME}` whose value is unknown."""
    text = text.replace("\\\n", " ")
    lines = [line.strip().lstrip("@-+").strip() for line in text.splitlines()]
    joined = "\n".join(line for line in lines if line)
    if kind == "just":
        return re.sub(r"\{\{[^}]*\}\}", "${" + _MAKE_VAR_PREFIX + "just}", joined)
    joined = joined.replace("$$", "\x06")
    joined = re.sub(r"\$\(shell\s+", "$(", joined)
    joined = re.sub(
        r"\$[({]([A-Za-z_][A-Za-z0-9_.-]*)[)}]",
        lambda match: "${" + _MAKE_VAR_PREFIX + re.sub(r"\W", "_", match.group(1)) + "}",
        joined,
    )
    joined = re.sub(r"\$([@<^*?%])", "${" + _MAKE_VAR_PREFIX + "auto}", joined)
    return joined.replace("\x06", "$")


def _first_positional(args: Sequence[_Word], value_options: frozenset[str]) -> int:
    """Index of the first non-option word; `value_options` consume the next word; `--` ends the options."""
    i = 0
    while i < len(args):
        text = args[i].text
        if text == "--":
            return i + 1
        if not text.startswith("-") or text == "-":
            return i
        i += 2 if text in value_options else 1
    return i


def _is_mktemp(script: _Script) -> bool:
    commands = [item for item in script if isinstance(item, _Command)]
    return len(commands) == 1 and bool(commands[0].words) and commands[0].words[0].literal == "mktemp"


_ALWAYS_RECURSIVE: Final = frozenset({"rg", "ag", "ack", "tar", "zip", "ditto", "rsync"})  # read contents
_RECURSIVE_WITH_FLAG: Final = frozenset({"grep", "egrep", "fgrep", "cp", "chmod", "chown", "chflags", "scp"})


def _reads_recursively(key: str, args: Sequence[_Word]) -> bool:
    if key in _ALWAYS_RECURSIVE:
        return True
    if key not in _RECURSIVE_WITH_FLAG:
        return False
    flags = "".join(word.text[1:] for word in args if re.fullmatch(r"-[A-Za-z]+", word.text))
    return "r" in flags or "R" in flags or (key == "cp" and "a" in flags) or _has(args, "--recursive", "--archive")


def _covers(relative: str, item: str, *, glob: bool, names: Sequence[str] | None) -> bool:
    """Does deleting `relative` (project-relative, casefolded) remove the untracked file `item`?"""
    under = relative in (".", item) or item.startswith(relative.rstrip("/") + "/")
    if names:
        base = posixpath.basename(item)
        return under and any(fnmatchcase(base, name) or fnmatchcase(item, name.removeprefix("./")) for name in names)
    if glob:
        parts = item.split("/")
        return any(fnmatchcase("/".join(parts[:count]), relative) for count in range(1, len(parts) + 1))
    return under


def _remote_command_ok(command: _Command, patterns: Sequence[_Pattern]) -> bool:
    """One remote command: on the allowlist (or a filter after a pipe), fully literal, no redirection but fd dups
    and `/dev/null`."""
    for redirect in command.redirects:
        target = redirect.target.literal
        fd_dup = redirect.op in (">&", "<&") and target is not None and (target.isdigit() or target == "-")
        if not (fd_dup or (redirect.op in (">", ">>") and target == "/dev/null")):
            return False
    literals = [word.literal for word in command.words]
    argv = [value for value in literals if value is not None]
    if not argv or len(argv) != len(literals):
        return False
    if argv[0] == "sudo":
        argv = argv[2:] if argv[1:2] == ["-n"] else argv[1:]
    if command.piped:
        return _filter_ok(argv)
    return any(pattern.matches(argv) for pattern in patterns)


def _filter_ok(argv: Sequence[str]) -> bool:
    """A pipeline stage after a read-only remote command: a filter that writes nothing."""
    if not argv or argv[0] not in _REMOTE_FILTERS:
        return False
    if argv[0] == "sort" and any(arg.startswith(("-o", "--output", "-T", "--temporary-directory")) for arg in argv):
        return False
    return not (argv[0] == "uniq" and len([arg for arg in argv[1:] if not arg.startswith("-")]) > 1)


_UNKNOWN_WORD: Final = _Word(f"{_VAR_OPEN}{_UNKNOWN_VAR}{_VAR_CLOSE}", "{…}")


def _literal_word(text: str) -> _Word:
    return _Word(text, text)


# Wrappers that run their remaining words as a command: (options that take a value, positionals to skip first).
_WRAPPERS: Final[Mapping[str, tuple[frozenset[str], int]]] = {
    "nohup": (frozenset(), 0),
    "time": (frozenset({"-f", "-o", "--format", "--output"}), 0),
    "nice": (frozenset({"-n", "--adjustment"}), 0),
    "timeout": (frozenset({"-s", "-k", "--signal", "--kill-after"}), 1),
    "gtimeout": (frozenset({"-s", "-k", "--signal", "--kill-after"}), 1),
    "stdbuf": (frozenset({"-i", "-o", "-e"}), 0),
    "caffeinate": (frozenset({"-t", "-w"}), 0),
    "chronic": (frozenset(), 0),
    "unbuffer": (frozenset(), 0),
    "exec": (frozenset({"-a"}), 0),
    "command": (frozenset(), 0),
    "builtin": (frozenset(), 0),
    "arch": (frozenset(), 0),
    "ionice": (frozenset({"-c", "-n", "-p"}), 0),
    "doas": (frozenset({"-u", "-C"}), 0),
    "npx": (frozenset({"-p", "--package"}), 0),
    "pnpx": (frozenset({"-p", "--package"}), 0),
    "bunx": (frozenset({"-p", "--package"}), 0),
    "uvx": (frozenset({"--from", "--with", "--python", "-p", "--with-requirements", "--index", "--index-url"}), 0),
    "dotenv": (frozenset({"-e", "-c", "-v"}), 0),
}
_SUDO_VALUE_OPTIONS: Final = frozenset(
    {"-u", "-g", "-h", "-p", "-C", "-D", "-r", "-t", "-U", "-T", "--user", "--group", "--host", "--prompt", "--chdir"}
)
_XARGS_VALUE_OPTIONS: Final = frozenset({"-I", "-L", "-n", "-P", "-s", "-d", "-E", "-a", "-J", "-R", "-S"})
_SHELLS: Final = frozenset({"bash", "sh", "zsh", "dash", "ksh", "mksh", "ash", "fish", "tcsh", "csh"})
_SHELL_VALUE_OPTIONS: Final = frozenset({"-o", "+o", "-O", "+O", "--rcfile", "--init-file"})
_INLINE_CODE: Final[Mapping[str, tuple[str, ...]]] = {
    "python": ("-c",),
    "node": ("-e", "--eval", "-p", "--print"),
    "ruby": ("-e",),
    "perl": ("-e", "-E"),
    "php": ("-r",),
    "osascript": ("-e",),
    "lua": ("-e",),
    "rscript": ("-e",),
}
_INTERPRETER_VALUE_OPTIONS: Final = frozenset({"-W", "-X", "-r", "--require", "--import", "-I", "-x"})


class _Walk:
    """One pass over a call: collects findings and missing facts. Sequential shell state (cwd, variables) is
    tracked across `;`/`&&` and restored after `( … )`."""

    def __init__(self, rules: GateRules, ctx: GateContext) -> None:
        self.rules = rules
        self.ctx = ctx
        self.findings: list[Finding] = []
        self.missing: set[str] = set()
        self.scripts: list[ScriptRef] = []
        self.remote_calls: list[tuple[str | None, ...]] = []  # ssh-family argv seen (None = known at run time)
        self.remotes_needed: list[str] = []  # git remote names whose URL decides the call
        self.defined_remotes: dict[str, str | None] = {}  # `git remote add|set-url` earlier in this call
        self.push_sources: list[str] = []  # local refs a `git push` sends
        self.home = _canonical(ctx.home)
        self.root = _canonical(ctx.project_root)
        self.cwd: str | None = _canonical(ctx.cwd)
        self.vars: dict[str, str | None] = {}
        self.cmd_env: dict[str, str | None] = {}
        claude = (self.home + "/.claude").casefold()
        self.claude_dir = claude
        self.forbidden_roots = tuple(_canonical(path).casefold() for path in (ctx.data_dir, ctx.config_dir))
        self.forbidden_files = (claude + "/.credentials.json", claude + "/claude.md")
        self.fragments = (
            *("/".join(root.split("/")[-2:]) for root in self.forbidden_roots),
            ".claude/.credentials.json",
            ".claude/settings",
            ".claude/claude.md",
        )

    # --- findings and facts ---
    def ask(self, gate_class: GateClass | None, reason: Reason, target: str = "") -> None:
        self.findings.append(Finding(Action.ASK, reason, target[:200], gate_class))

    def forbid(self, reason: Reason, target: str = "") -> None:
        self.findings.append(Finding(Action.FORBID, reason, target[:200]))

    def need(self, fact: str) -> None:
        self.missing.add(fact)

    def need_remote(self, name: str) -> None:
        if name not in self.remotes_needed:
            self.remotes_needed.append(name)

    # --- values and paths ---
    def var(self, name: str) -> str | None:
        if name == "HOME":
            return self.home
        if name == "PWD":
            return self.cwd
        if name == "TMPDIR":
            return _TMPDIR_PATH
        if name in self.cmd_env:
            return self.cmd_env[name]
        return self.vars.get(name)

    def value(self, word: _Word) -> str | None:
        """The word as the command will see it, or None if part of it is only known at run time."""
        known, complete = self.expand(word)
        return known if complete else None

    def expand(self, word: _Word) -> tuple[str, bool]:
        """`(text, complete)`: the expanded word, or its known beginning when a later part is only known at run
        time (`"$scratch/$db.db"` → the temp dir)."""
        out: list[str] = []
        subs = iter(word.subs)
        rest = word.text.replace(_TILDE, self.home)
        while rest:
            head, marker, rest = _split_marker(rest)
            out.append(head)
            if marker == _SUBST:
                sub = next(subs, None)
                if sub is None or not _is_mktemp(sub):
                    return "".join(out), False
                out.append(_MKTEMP_PATH)
            elif marker == _VAR_OPEN:
                name, _, rest = rest.partition(_VAR_CLOSE)
                resolved = self.var(name)
                if resolved is None:
                    return "".join(out), False
                out.append(resolved)
        return "".join(out), True

    def path(self, value: str) -> str | None:
        if value.startswith("/"):
            return _canonical(value)
        return None if self.cwd is None else _canonical(posixpath.join(self.cwd, value))

    def inside_root(self, path: str) -> bool:
        folded, root = path.casefold(), self.root.casefold()
        return folded == root or folded.startswith(root + "/")

    # --- forbidden paths (SPEC §5.7) ---
    def is_forbidden(self, path: str, *, glob: bool, recursive: bool) -> bool:
        folded = path.casefold()
        protected = (*self.forbidden_roots, *self.forbidden_files, self.claude_dir + "/settings")
        if glob:
            prefix = re.split(r"[*?\[{]", folded, maxsplit=1)[0]
            if any(item.startswith(prefix) for item in protected):
                return True
            folded = prefix.rstrip("/") or "/"
        if any(folded == root or folded.startswith(root + "/") for root in self.forbidden_roots):
            return True
        if folded in self.forbidden_files or fnmatchcase(folded, self.claude_dir + "/settings*.json"):
            return True
        if recursive:  # a recursive reader or deleter started above a protected path reaches it
            base = folded.rstrip("/") + "/"
            return any(item.startswith(base) for item in protected)
        return False

    def check_word(self, word: _Word, *, recursive: bool = False) -> None:
        for name in _SOCKET_VARS:
            if f"{_VAR_OPEN}{name}{_VAR_CLOSE}" in word.text:
                self.forbid(Reason.YORDAMCHI_SOCKET, name)
        value = self.value(word)
        if value is None:
            if self.fragment_hit(_strip_markers(word.text).casefold()):
                self.forbid(Reason.FORBIDDEN_PATH, _strip_markers(word.text))
            return
        if posixpath.basename(value) == "yordamchi-helper":
            self.forbid(Reason.YORDAMCHI_SOCKET, value)
        candidates = [value]
        if "=" in value:
            candidates.append(value.split("=", 1)[1])
        for candidate in candidates:
            pieces = candidate.split(":") if ":" in candidate and "://" not in candidate else [candidate]
            for piece in pieces:
                resolved = self.path(piece) if piece else None
                if resolved is not None and self.is_forbidden(resolved, glob=word.glob, recursive=recursive):
                    self.forbid(Reason.FORBIDDEN_PATH, piece)
                    return

    def fragment_hit(self, folded: str) -> bool:
        return any(fragment in folded for fragment in self.fragments)

    def keyword_scan(self, text: str) -> None:
        folded = re.sub(r"[\"'\\]", "", text).casefold()
        if self.fragment_hit(folded):
            self.forbid(Reason.FORBIDDEN_PATH, "")
        for pattern, reason in _FORBIDDEN_KEYWORDS:
            if pattern.search(folded):
                self.forbid(reason, "")
        for pattern, gate_class, ask_reason in _ASK_KEYWORDS:
            match = pattern.search(folded)
            if match is not None:
                self.ask(gate_class, ask_reason, match.group(0).strip())

    # --- deletes (fs_destroy) ---
    def delete_word(self, word: _Word) -> None:
        self.check_word(word, recursive=True)
        value = self.value(word)
        resolved = self.path(value) if value is not None else None
        self.delete_path(resolved, glob=word.glob, label=value or _strip_markers(word.text))

    def delete_path(
        self,
        path: str | None,
        *,
        glob: bool,
        label: str,
        names: Sequence[str] | None = None,
        untracked_only: bool = False,
    ) -> None:
        """Ask before deleting the project root (or above it), anything outside the project except the temp roots,
        `.git`, or untracked non-ignored files. `names` = `find` predicates (None when it is not `find`);
        `untracked_only` = the command removes only untracked files under `path` (`git clean`)."""
        if path is None:
            self.ask(GateClass.FS_DESTROY, Reason.DELETE_UNKNOWN, label)
            return
        folded = path.casefold().rstrip("/") or "/"
        literal = re.split(r"[*?\[{]", folded, maxsplit=1)[0] if glob else folded
        root = self.root.casefold()
        above_root = root in (folded, literal) or root.startswith(literal.rstrip("/") + "/")
        whole_paths = names is None and not untracked_only
        if whole_paths and (above_root or (glob and root.startswith(literal))):
            self.ask(GateClass.FS_DESTROY, Reason.DELETE_PROJECT_ROOT, label)
            return
        if not (literal == root or literal.startswith(root + "/")):
            temp_roots = tuple(item.casefold() for item in self.rules.temp_roots)
            if not any(literal == item or literal.startswith(item + "/") for item in temp_roots):
                self.ask(GateClass.FS_DESTROY, Reason.DELETE_OUTSIDE_PROJECT, label)
            return
        relative = posixpath.relpath(folded, root)
        whole_tree = names is not None and not names and relative == "."
        git_dir = relative == ".git" or relative.startswith(".git/") or (glob and fnmatchcase(".git", relative))
        if not untracked_only and (git_dir or whole_tree):
            self.ask(GateClass.FS_DESTROY, Reason.DELETE_GIT_DIR, label)
            return
        untracked = self.ctx.untracked
        if untracked is None:
            self.need("untracked")
            self.ask(GateClass.FS_DESTROY, Reason.DELETE_UNTRACKED, label)
            return
        if any(_covers(relative, item.casefold(), glob=glob, names=names) for item in untracked):
            self.ask(GateClass.FS_DESTROY, Reason.DELETE_UNTRACKED, label)

    # --- databases (the `data_loss_db` test-DB exemption) ---
    def test_sqlite(self, database: str) -> bool:
        """`:memory:`, or a SQLite file under a temp root or a `tests`/`test` folder."""
        if database in (":memory:", ""):
            return True
        resolved = self.path(database)
        if resolved is None:
            return False
        folded = resolved.casefold()
        if any(folded.startswith(item.casefold() + "/") for item in self.rules.temp_roots):
            return True
        return any(part in ("tests", "test") for part in folded.split("/")[:-1])

    @staticmethod
    def test_server_db(host: str | None, database: str | None) -> bool:
        """A database on this Mac whose name contains `test`."""
        local = host is not None and host.casefold() in _LOCAL_HOSTS
        return local and database is not None and "test" in database.casefold()

    def db_targets(self, args: Sequence[_Word]) -> list[tuple[str, str | None, str | None]] | None:
        """DBs the command names itself (URL options, `-x key=value`, `DATABASE_URL`…); None if only known later."""
        found: list[tuple[str, str | None, str | None]] = []
        for word in _option_values(args, *_DB_URL_OPTIONS):
            value = self.value(word)
            if value is None and _strip_markers(word.text).endswith((".db", ".sqlite", ".sqlite3")):
                known = self.expand(word)[0]  # a SQLite file is local; its folder decides test-ness
                found.append(("", None, known.split("=", 1)[-1] or "?"))
                continue
            if value is None:
                return None
            head = value.split("=", 1)[0]
            candidate = value.split("=", 1)[1] if "=" in value and "://" not in head else value
            if "://" in candidate:
                found.append(_parse_db_url(candidate))
            elif candidate.endswith((".db", ".sqlite", ".sqlite3")) or candidate == ":memory:":
                found.append(("", None, candidate))
        for name in _DB_URL_ENV:
            if name in self.cmd_env or name in self.vars:
                value = self.var(name)
                if value is None:
                    return None
                if "://" in value:
                    found.append(_parse_db_url(value))
        return found

    def provably_test(self, targets: list[tuple[str, str | None, str | None]] | None, args: Sequence[_Word]) -> bool:
        if any((self.var(name) or "").casefold() in ("test", "testing") for name in _TEST_ENV):
            return True
        if any("test" in (self.value(word) or "").casefold() for word in _option_values(args, "--env", "--settings")):
            return True
        if not targets:
            return False
        return all(
            self.test_sqlite(sqlite) if sqlite is not None else self.test_server_db(host, name)
            for host, name, sqlite in targets
        )

    def remote_db(self, targets: list[tuple[str, str | None, str | None]] | None) -> str | None:
        """The first non-local DB host a migration reaches, `"?"` when unknown, None when every one is local."""
        if targets is None:
            return "?"
        if targets:
            hosts = [host for host, _, sqlite in targets if sqlite is None]
        elif self.ctx.db_hosts is None:
            self.need("db_hosts")
            return "?"
        else:
            hosts = list(self.ctx.db_hosts)
        return next((host for host in hosts if host.casefold() not in _LOCAL_HOSTS), None)

    def http_write(self, method: str, urls: Sequence[str | None], body: str, *, label: str) -> None:
        """Mutating HTTP: free to this Mac and `gate.http_write_allow_hosts`; GitHub repo deletion and visibility
        changes are their own classes; anything else is sending to a third party."""
        folded = body.casefold().replace(" ", "")
        for url in urls:
            host = _url_host(url) if url is not None else None
            if host is not None and (
                host in _LOCAL_HOSTS or host.endswith(".localhost") or host in self.rules.http_write_allow_hosts
            ):
                continue
            path = url.split("://", 1)[-1].partition("/")[2] if url else ""
            if host == "api.github.com" and method == "DELETE" and path.startswith("repos/"):
                self.ask(GateClass.REPO_DELETE, Reason.REPO_DELETE, path)
            elif host == "api.github.com" and ('"private":false' in folded or '"visibility":"public"' in folded):
                self.ask(GateClass.REPO_PUBLIC, Reason.REPO_PUBLIC, path)
            else:
                self.ask(GateClass.THIRD_PARTY_SEND, Reason.HTTP_WRITE, f"{label} {method} {host or '?'}")

    # --- the server read-only allowlist (SPEC §5.7) ---
    def remote_read_only(self, text: str) -> bool:
        """Every command is on `gate.readonly_remote_commands` (optionally after `sudo`/`sudo -n`), and pipes only
        feed `grep head tail wc sort uniq cut`; no redirection except fd duplication and `/dev/null`."""
        try:
            script = _parse(text)
        except _Unparseable:
            return False
        commands = [item for item in script if isinstance(item, _Command)]
        if not commands or len(commands) != len(script):
            return False
        patterns = [_Pattern(tuple(entry.split())) for entry in self.rules.readonly_remote_commands]
        return all(_remote_command_ok(command, patterns) for command in commands)

    # --- scripts ---
    def run_text(self, text: str, level: int, *, kind: ScriptKind | None = None) -> None:
        source = _make_recipe(text, kind) if kind in ("make", "just") else text
        try:
            script = _parse(source)
        except _Unparseable:
            self.ask(None, Reason.UNPARSEABLE, text[:80])
            self.keyword_scan(text)
            return
        self.walk(script, level)

    def walk(self, script: _Script, level: int) -> None:
        saved: list[tuple[str | None, dict[str, str | None]]] = []
        for item in script:
            if isinstance(item, str):
                if item == "(":
                    saved.append((self.cwd, dict(self.vars)))
                elif saved:
                    self.cwd, self.vars = saved.pop()
            else:
                self.command(item, level)

    def recipe(self, ref: ScriptRef, level: int) -> bool:
        """Classify a script's text (≤ 2 levels). True when its commands were judged one by one."""
        if level >= _RECIPE_LEVELS:
            return False
        if ref not in self.ctx.recipes:
            if ref not in self.scripts:
                self.scripts.append(ref)
            self.ask(None, Reason.RECIPE_UNKNOWN, ref.name or ref.kind)
            return False
        text = self.ctx.recipes[ref]
        if text is None:
            return True  # the service found nothing to run (missing or binary file, no such target)
        saved = (self.cwd, self.vars, self.cmd_env)
        self.cwd, self.vars, self.cmd_env = _canonical(ref.directory), {}, {}
        self.run_text(text, level + 1, kind=ref.kind)
        self.cwd, self.vars, self.cmd_env = saved
        return True

    def script_file(self, path: str | None, level: int, *, as_shell: bool) -> _Effect:
        if path is None:
            self.ask(None, Reason.OPAQUE_COMMAND, "script")
            return _Effect.WRITE
        name = posixpath.basename(path)
        if _DEPLOY_NAME.match(name):
            self.ask(GateClass.DEPLOY, Reason.DEPLOY_TARGET, name)
        if not (as_shell or posixpath.splitext(name)[1].lower() in ("", ".sh", ".bash", ".zsh", ".command")):
            return _Effect.WRITE
        resolved = self.path(path)
        if resolved is None:
            self.ask(None, Reason.OPAQUE_COMMAND, path)
            return _Effect.WRITE
        judged = self.recipe(ScriptRef("file", resolved, posixpath.dirname(resolved)), level)
        return _Effect.TRANSPARENT if judged else _Effect.WRITE

    # --- one simple command ---
    def command(self, cmd: _Command, level: int) -> None:
        words = list(cmd.words)
        split = 0
        while split < len(words) and _is_assignment(words[split]):
            split += 1
        for word in (*words, *(redirect.target for redirect in cmd.redirects)):
            self.check_word(word)
            for sub in word.subs:
                self.walk(sub, level)
        self.cmd_env = {}
        for word in words[:split]:
            self.assignment(word, persist=split == len(words))
        argv = words[split:]
        effect = self.run(argv, cmd, level) if argv else _Effect.TRANSPARENT
        self.apply_policy(effect, argv, cmd)
        self.cmd_env = {}

    def assignment(self, word: _Word, *, persist: bool) -> None:
        name = word.text.partition("=")[0]
        offset = len(name) + 1
        value = self.value(_Word(word.text[offset:], word.raw[offset:], word.subs, word.glob))
        self.check_env_name(name, value)
        if persist:
            self.vars[name] = value
        else:
            self.cmd_env[name] = value

    def check_env_name(self, name: str, value: str | None) -> None:
        upper = name.upper()
        if upper.startswith(_FORBIDDEN_ENV_PREFIXES):
            self.forbid(Reason.CREDENTIAL_ENV, name)
        elif _GIT_CONFIG_ENV.match(upper):
            self.forbid(Reason.HOOKS_BYPASS, name)
        elif upper == "DOCKER_HOST" and (value is None or _host_of(value) not in _LOCAL_HOSTS):
            if not (value or "").startswith(("unix://", "npipe://")):
                self.ask(GateClass.SERVER_WRITE, Reason.REMOTE_DOCKER, value or name)
        elif upper == "DOCKER_CONTEXT" and value not in ("", "default", "desktop-linux", "orbstack", "colima"):
            self.ask(GateClass.SERVER_WRITE, Reason.REMOTE_DOCKER, value or name)

    def run(self, argv: Sequence[_Word], cmd: _Command, level: int) -> _Effect:
        while argv and argv[0].literal in _RESERVED_LEADING:
            argv = argv[1:]
        if not argv or argv[0].literal in _RESERVED_ALONE | _RESERVED_LISTS:
            return _Effect.TRANSPARENT
        name = self.program_name(argv, cmd, level)
        if isinstance(name, _Effect):
            return name
        base = posixpath.basename(name)
        key = _program_key(base)
        if _reads_recursively(key, argv[1:]):  # started above a protected path, it reaches it
            for word in argv[1:]:
                self.check_word(word, recursive=True)
        handler = _HANDLERS.get(key)
        if handler is not None:
            return handler(self, key, list(argv[1:]), cmd, level)
        if "/" in name:
            return self.script_file(name, level, as_shell=False)
        if _DEPLOY_NAME.match(base) and not base.casefold().startswith("prod"):
            self.ask(GateClass.DEPLOY, Reason.DEPLOY_TARGET, base)
        return _local_effect(key, [self.value(word) for word in argv[1:]])

    def program_name(self, argv: Sequence[_Word], cmd: _Command, level: int) -> str | _Effect:
        """The program `argv` runs, or the effect when it is only known at run time (already judged)."""
        first = argv[0]
        name = self.value(first)
        if name is not None:
            return name
        tail = first.text.rsplit("/", 1)[-1] if "/" in first.text else ""
        if tail and _VAR_OPEN not in tail and _SUBST not in tail:
            return tail  # `"$VIRTUAL_ENV/bin/python"`: the program is known, its folder is not
        if not first.text.startswith(_VAR_OPEN + _MAKE_VAR_PREFIX):
            self.ask(None, Reason.OPAQUE_COMMAND, _strip_markers(first.text))
            return _Effect.WRITE
        # A make variable usually holds a runner prefix (`$(RUN) pytest`): judge the rest, and scan it with the
        # variable's name, which often names the tool (`$(SSH) prod reboot`).
        variable = first.text.split(_VAR_CLOSE, 1)[0].removeprefix(_VAR_OPEN + _MAKE_VAR_PREFIX)
        if variable == "MAKE":  # recursive make
            return self.run([_literal_word("make"), *argv[1:]], cmd, level)
        self.keyword_scan(" ".join([variable, *(_strip_markers(word.text) for word in argv[1:])]))
        return self.run(argv[1:], cmd, level)

    def apply_policy(self, effect: _Effect, argv: Sequence[_Word], cmd: _Command) -> None:
        policy = self.ctx.policy
        if policy not in (Policy.READ_ONLY, Policy.PLAN_ONLY):
            return
        name = _strip_markers(argv[0].text) if argv else ""
        writes = [redirect for redirect in cmd.redirects if redirect.writes_file]
        sinks = [redirect for redirect in writes if not self.harmless_sink(redirect.target)]
        if policy is Policy.READ_ONLY:
            if effect not in (_Effect.READ, _Effect.TRANSPARENT):
                self.forbid(Reason.READ_ONLY_POLICY, name)
            for redirect in sinks:
                self.forbid(Reason.READ_ONLY_POLICY, _strip_markers(redirect.target.text))
            return
        if effect not in (_Effect.READ, _Effect.TRANSPARENT, _Effect.GIT_WRITE, _Effect.PLAN_WRITE):
            self.forbid(Reason.PLAN_ONLY_POLICY, name)
        for redirect in sinks:
            if not self.plan_path(redirect.target):
                self.forbid(Reason.PLAN_ONLY_POLICY, _strip_markers(redirect.target.text))

    def harmless_sink(self, target: _Word) -> bool:
        value = self.value(target)
        return value is not None and _canonical(value) in _DEV_SINKS

    def plan_path(self, target: _Word | str) -> bool:
        """PLAN_ONLY may write only `docs/**`, `README.md` and `.gitignore` of the project (SPEC §3.2.8)."""
        value = self.value(target) if isinstance(target, _Word) else target
        resolved = self.path(value) if value else None
        if resolved is None or not self.inside_root(resolved):
            return False
        relative = posixpath.relpath(resolved.casefold(), self.root.casefold())
        return relative in ("readme.md", ".gitignore", "docs") or relative.startswith("docs/")

    # --- wrappers, shells, interpreters ---
    def h_wrapper(self, name: str, args: list[_Word], cmd: _Command, level: int) -> _Effect:
        value_options, skip = _WRAPPERS[name]
        start = _first_positional(args, value_options)
        if name == "command" and any(word.text in ("-v", "-V") for word in args[:start]):
            return _Effect.READ
        inner = args[start + skip :]
        return self.run(inner, cmd, level) if inner else _Effect.TRANSPARENT

    def h_env(self, name: str, args: list[_Word], cmd: _Command, level: int) -> _Effect:
        del name
        i = 0
        saved_cwd = self.cwd
        while i < len(args):
            text = args[i].text
            nxt = args[i + 1] if i + 1 < len(args) else _literal_word("")
            if text in ("-i", "--ignore-environment", "-"):
                self.forbid(Reason.HOOKS_BYPASS, "env -i")  # it drops GIT_CONFIG_* and with it the pre-push scan
            elif text in ("-u", "--unset"):
                self.unset(nxt.text)
                i += 1
            elif text.startswith(("--unset=", "-u")):
                self.unset(text.removeprefix("--unset=").removeprefix("-u"))
            elif text in ("-C", "--chdir"):
                self.chdir(nxt)
                i += 1
            elif text.startswith("--chdir="):
                self.chdir(_literal_word(text.removeprefix("--chdir=")))
            elif text in ("-S", "--split-string") or text.startswith(("--split-string=", "-S")):
                inline = text.removeprefix("--split-string=").removeprefix("-S")
                source = inline or self.value(nxt)
                i += 0 if inline else 1
                if source is None:
                    self.ask(None, Reason.OPAQUE_COMMAND, "env -S")
                    return _Effect.WRITE
                self.run_text(" ".join([source, *(word.raw for word in args[i + 1 :])]), level)
                self.cwd = saved_cwd
                return _Effect.TRANSPARENT
            elif text == "--":
                i += 1
                break
            elif text.startswith("-"):
                pass
            elif _is_assignment(args[i]):
                self.assignment(args[i], persist=False)
            else:
                break
            i += 1
        effect = self.run(args[i:], cmd, level) if args[i:] else _Effect.READ
        self.cwd = saved_cwd
        return effect

    def unset(self, name: str) -> None:
        if _GIT_CONFIG_ENV.match(name.upper()):
            self.forbid(Reason.HOOKS_BYPASS, name)
        self.vars.pop(name, None)

    def chdir(self, target: _Word | None) -> None:
        if target is None:
            self.cwd = self.home
            return
        value = self.value(target)
        self.cwd = None if value is None or value == "-" or target.glob else self.path(value)

    def h_sudo(self, name: str, args: list[_Word], cmd: _Command, level: int) -> _Effect:
        del name
        i, shell = 0, False
        while i < len(args) and args[i].text.startswith("-"):
            text = args[i].text
            if text == "--":
                i += 1
                break
            if text in ("-e", "--edit"):
                return _Effect.WRITE
            shell = shell or text in ("-s", "--shell", "-i", "--login")
            i += 2 if text in _SUDO_VALUE_OPTIONS else 1
        inner = args[i:]
        if not inner:
            return _Effect.TRANSPARENT
        if shell:
            return self.inline_shell(inner, level)
        return self.run(inner, cmd, level)

    def inline_shell(self, words: Sequence[_Word], level: int) -> _Effect:
        values = [self.value(word) for word in words]
        if any(value is None for value in values):
            self.ask(None, Reason.OPAQUE_COMMAND, " ".join(_strip_markers(word.text) for word in words))
            return _Effect.WRITE
        self.run_text(" ".join(value for value in values if value is not None), level)
        return _Effect.TRANSPARENT

    def h_eval(self, name: str, args: list[_Word], cmd: _Command, level: int) -> _Effect:
        del name, cmd
        return self.inline_shell(args, level) if args else _Effect.TRANSPARENT

    def h_watch(self, name: str, args: list[_Word], cmd: _Command, level: int) -> _Effect:
        del name, cmd
        start = _first_positional(args, frozenset({"-n", "--interval", "-q", "--equexit"}))
        return self.inline_shell(args[start:], level) if args[start:] else _Effect.TRANSPARENT

    def h_xargs(self, name: str, args: list[_Word], cmd: _Command, level: int) -> _Effect:
        del name
        start = _first_positional(args, _XARGS_VALUE_OPTIONS)
        placeholders = {args[i + 1].text for i in range(start - 1) if args[i].text in ("-I", "-J", "-R")}
        inner = [_UNKNOWN_WORD if word.text in placeholders else word for word in args[start:]]
        return self.run([*(inner or [_literal_word("echo")]), _UNKNOWN_WORD], cmd, level)

    def h_trap(self, name: str, args: list[_Word], cmd: _Command, level: int) -> _Effect:
        del name, cmd
        if args and args[0].text not in ("-l", "-p", "-"):
            return self.inline_shell(args[:1], level)
        return _Effect.READ

    def h_shell(self, name: str, args: list[_Word], cmd: _Command, level: int) -> _Effect:
        del name
        i, command_mode, stdin_mode = 0, False, False
        while i < len(args):
            text = args[i].text
            if text == "--":
                i += 1
                break
            if text in _SHELL_VALUE_OPTIONS:
                i += 2
                continue
            if not text.startswith(("-", "+")) or text in ("-", "+"):
                break
            if not text.startswith("--"):
                command_mode = command_mode or "c" in text[1:]
                stdin_mode = stdin_mode or "s" in text[1:]
            i += 1
        rest = args[i:]
        if command_mode:
            return self.inline_shell(rest[:1], level) if rest else _Effect.TRANSPARENT
        if rest and not stdin_mode and rest[0].text != "-":
            return self.script_file(self.value(rest[0]), level, as_shell=True)
        return self.stdin_code(cmd, level, as_shell=True)

    def stdin_code(self, cmd: _Command, level: int, *, as_shell: bool) -> _Effect:
        """Code read from stdin: a heredoc or here-string is judged (and keyword-scanned); a pipe is opaque."""
        bodies = [text for redirect in cmd.redirects if (text := redirect.stdin_text) is not None]
        for body in bodies:
            if as_shell:
                self.run_text(body, level)
            self.keyword_scan(body)
        if bodies:
            return _Effect.TRANSPARENT if as_shell else _Effect.WRITE
        files = [redirect.target for redirect in cmd.redirects if redirect.op == "<"]
        if files:
            return self.script_file(self.value(files[0]), level, as_shell=as_shell)
        if cmd.piped:
            self.ask(None, Reason.OPAQUE_COMMAND, "code from a pipe")
            return _Effect.WRITE
        return _Effect.TRANSPARENT  # interactive: nothing runs headless

    def h_source(self, name: str, args: list[_Word], cmd: _Command, level: int) -> _Effect:
        del name, cmd
        return self.script_file(self.value(args[0]), level, as_shell=True) if args else _Effect.READ

    def h_interpreter(  # noqa: PLR0911  # one return per invocation form (-c, -m, -, file, stdin)
        self, name: str, args: list[_Word], cmd: _Command, level: int
    ) -> _Effect:
        flags = _INLINE_CODE.get(name, ())
        if len(args) == 1 and args[0].text in ("--version", "-V", "-v", "--help", "-h"):
            return _Effect.READ
        i = 0
        while i < len(args):
            text = args[i].text
            if name == "python" and text.startswith("-m"):
                module = text[2:] or (self.value(args[i + 1]) if i + 1 < len(args) else "") or ""
                rest = args[i + 1 :] if text[2:] else args[i + 2 :]
                return self.python_module(module, rest, cmd, level)
            flag = next((flag for flag in flags if text == flag or (len(flag) == 2 and text.startswith(flag))), None)
            if flag is None and name == "python" and re.fullmatch(r"-[A-Za-z]*c", text):
                flag = text
            if flag is not None:
                inline = text[len(flag) :]
                code = inline or (self.value(args[i + 1]) if i + 1 < len(args) else "") or args[-1].raw
                self.keyword_scan(code)
                return _Effect.WRITE
            if text == "-":
                return self.stdin_code(cmd, level, as_shell=False)
            if text.startswith("-"):
                i += 2 if text in _INTERPRETER_VALUE_OPTIONS else 1
                continue
            script = self.value(args[i])
            if script is not None and posixpath.basename(script) in _MIGRATIONS:  # `python manage.py migrate`
                return _h_migration(self, posixpath.basename(script), args[i + 1 :], cmd, level)
            return self.script_file(script, level, as_shell=False)
        return self.stdin_code(cmd, level, as_shell=False)

    def python_module(self, module: str, args: list[_Word], cmd: _Command, level: int) -> _Effect:
        if _SOCKET_MODULES.match(module):
            self.forbid(Reason.YORDAMCHI_SOCKET, module)
            return _Effect.WRITE
        return self.run([_literal_word(module.split(".", maxsplit=1)[0]), *args], cmd, level)


# --- program handlers ----------------------------------------------------------------------------------------------
# Each takes (walk, program, args, command, recipe level) and returns the command's effect; findings go on the walk.
type _Handler = Callable[[_Walk, str, list[_Word], _Command, int], _Effect]


def _positionals(args: Sequence[_Word], value_options: frozenset[str] = frozenset()) -> list[_Word]:
    """Non-option words; `value_options` consume the next word; everything after `--` is positional."""
    out: list[_Word] = []
    i, ended = 0, False
    while i < len(args):
        text = args[i].text
        if ended or not text.startswith("-") or text == "-":
            out.append(args[i])
        elif text == "--":
            ended = True
        elif text in value_options:
            i += 1
        i += 1
    return out


def _option_values(args: Sequence[_Word], *names: str) -> list[_Word]:
    """Values of `-x V`, `--name V` and `--name=V` (as words)."""
    found: list[_Word] = []
    for i, word in enumerate(args):
        for name in names:
            if word.text == name and i + 1 < len(args):
                found.append(args[i + 1])
            elif name.startswith("--") and word.text.startswith(name + "="):
                found.append(_Word(word.text[len(name) + 1 :], word.raw, word.subs, word.glob))
    return found


def _has(args: Sequence[_Word], *names: str) -> bool:
    return any(word.text in names or word.text.split("=", 1)[0] in names for word in args)


def _short_flag(args: Sequence[_Word], letter: str) -> bool:
    """A single-dash cluster containing `letter` (`-fu` has `f`)."""
    return any(re.fullmatch(r"-[A-Za-z]+", word.text) and letter in word.text[1:] for word in args)


def _texts(words: Sequence[_Word]) -> list[str]:
    return [word.text for word in words]


def _after(args: Sequence[_Word], word: _Word) -> list[_Word]:
    """The words after `word`, found by identity (two words with the same text are different words)."""
    index = next((k for k, item in enumerate(args) if item is word), len(args))
    return list(args[index + 1 :])


# --- git ---
_GIT_VALUE_OPTIONS: Final = frozenset(
    {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path", "--super-prefix", "--config-env"}
)
_GIT_READ: Final = frozenset(
    {
        "status", "log", "show", "diff", "blame", "rev-parse", "ls-files", "ls-tree", "cat-file", "grep", "describe",
        "shortlog", "show-ref", "for-each-ref", "rev-list", "name-rev", "merge-base", "count-objects", "whatchanged",
        "check-ignore", "var", "help", "version", "annotate", "cherry", "range-diff",
    }
)  # fmt: skip
_GIT_PUSH_VALUE_OPTIONS: Final = frozenset({"-o", "--push-option", "--repo", "--receive-pack", "--exec"})


def _h_git(walk: _Walk, name: str, args: list[_Word], cmd: _Command, level: int) -> _Effect:
    del name, cmd, level
    i = 0
    git_cwd = walk.cwd
    while i < len(args):
        text = args[i].text
        nxt = args[i + 1].text if i + 1 < len(args) else ""
        if text in ("-c", "--config-env") or text.startswith(("--config-env=", "-c")):
            setting = nxt if text in ("-c", "--config-env") else text.split("=", 1)[-1] if "=" in text else text[2:]
            if setting.casefold().replace(" ", "").startswith("core.hookspath"):
                walk.forbid(Reason.HOOKS_BYPASS, "-c core.hooksPath")
        if text == "-C" and i + 1 < len(args):
            value = walk.value(args[i + 1])
            git_cwd = walk.path(value) if value is not None else None
        if not text.startswith("-"):
            break
        i += 2 if text in _GIT_VALUE_OPTIONS else 1
    if i >= len(args):
        return _Effect.READ
    sub, rest = args[i].text, args[i + 1 :]
    saved, walk.cwd = walk.cwd, git_cwd
    try:
        return _git_subcommand(walk, sub, rest)
    finally:
        walk.cwd = saved


def _git_subcommand(walk: _Walk, sub: str, rest: list[_Word]) -> _Effect:
    words = _texts(_positionals(rest))
    if sub == "push":
        _git_push(walk, rest)
        return _Effect.GIT_WRITE
    if sub in ("add", "commit"):
        return _Effect.GIT_WRITE
    if sub == "clean":
        _git_clean(walk, rest)
        return _Effect.WRITE
    if sub == "send-email":
        walk.ask(GateClass.THIRD_PARTY_SEND, Reason.MAIL, "git send-email")
        return _Effect.WRITE
    discards = _discard_label(sub, rest)
    if discards is not None:
        _discard(walk, discards)  # never on the read list: the effect below is WRITE
    if sub == "remote":
        _git_remote(walk, rest)
    if sub in _GIT_READ:
        return _Effect.READ
    listing = {
        "branch": not words or _has(rest, "-a", "-r", "--list", "-l", "-v", "-vv", "--show-current", "--contains"),
        "tag": not words or _has(rest, "-l", "--list"),
        "remote": not words or words[0] in ("show", "get-url") or words == ["-v"],
        "stash": bool(words) and words[0] in ("list", "show"),
        "worktree": bool(words) and words[0] == "list",
        "config": _has(rest, "--get", "--get-all", "--list", "-l", "--get-regexp", "--show-origin"),
        "reflog": not words or words[0] in ("show",),
    }
    return _Effect.READ if listing.get(sub, False) else _Effect.WRITE


@dataclass(slots=True)
class _PushFlags:
    force: bool = False
    delete: bool = False
    everything: bool = False  # --all, --branches, --mirror, --prune
    tags: bool = False  # --tags: tags instead of the current branch
    follow_tags: bool = False  # --follow-tags: tags as well
    dry: bool = False
    no_verify: bool = False
    positional: list[_Word] = field(default_factory=list)


def _push_flags(rest: Sequence[_Word]) -> _PushFlags:
    flags = _PushFlags()
    i = 0
    while i < len(rest):
        text = rest[i].text
        if text == "--no-verify":
            flags.no_verify = True
        elif text in ("--force", "--force-if-includes") or text.startswith("--force-with-lease"):
            flags.force = flags.force or text != "--force-if-includes"
        elif text == "--mirror":
            flags.force = flags.delete = flags.everything = True
        elif text in ("--all", "--branches"):
            flags.everything = True
        elif text == "--tags":
            flags.tags = True
        elif text == "--follow-tags":
            flags.follow_tags = True
        elif text in ("--delete", "--prune"):
            flags.delete = True
            flags.everything = flags.everything or text == "--prune"
        elif text == "--dry-run":
            flags.dry = True
        elif text in _GIT_PUSH_VALUE_OPTIONS:
            i += 1
        elif re.fullmatch(r"-[A-Za-z0-9]+", text):
            flags.force = flags.force or "f" in text
            flags.delete = flags.delete or "d" in text
            flags.dry = flags.dry or "n" in text
        elif not text.startswith("-"):
            flags.positional.append(rest[i])
        i += 1
    return flags


def _git_push(walk: _Walk, rest: list[_Word]) -> None:
    flags = _push_flags(rest)
    if flags.no_verify:
        walk.forbid(Reason.PUSH_NO_VERIFY, "push --no-verify")
    if flags.dry:
        return
    _push_remote(walk, flags.positional[0] if flags.positional else None)
    refspecs = flags.positional[1:]
    targets, tag_push = _push_targets(walk, refspecs, flags)
    if not refspecs:
        walk.push_sources.append("HEAD")
        if flags.everything:
            targets.append(_PushTarget("*", flags.force, flags.delete))
        elif not flags.tags:
            targets.append(_PushTarget(_current_branch(walk), flags.force, flags.delete))
    _judge_push_targets(walk, targets, tag_push=tag_push or flags.tags or flags.follow_tags)


@dataclass(frozen=True, slots=True)
class _PushTarget:
    branch: str | None  # None = known only at run time; "*" = every branch
    forced: bool
    deleted: bool
    maybe_tag: bool = False  # a bare name (`push origin v1.0`) names a tag if no such branch exists


def _push_targets(walk: _Walk, refspecs: Sequence[_Word], flags: _PushFlags) -> tuple[list[_PushTarget], bool]:
    """The branch each refspec updates, and whether a tag is pushed. Records the local refs sent (`push_sources`)."""
    targets: list[_PushTarget] = []
    tag_push = False
    j = 0
    while j < len(refspecs):
        spec = walk.value(refspecs[j])
        if spec == "tag":  # `push origin tag v1`
            name = walk.value(refspecs[j + 1]) if j + 1 < len(refspecs) else None
            walk.push_sources.append(f"refs/tags/{name}" if name else "HEAD")
            tag_push, j = True, j + 2
            continue
        j += 1
        if spec is None:
            walk.push_sources.append("HEAD")
            targets.append(_PushTarget(None, flags.force, flags.delete))
            continue
        source, colon, destination = spec.lstrip("+").partition(":")
        if source:
            walk.push_sources.append("HEAD" if source == "@" else source)
        destination = destination if colon else source
        if destination.startswith("refs/tags/"):
            tag_push = True
            continue
        explicit_branch = destination.startswith("refs/heads/")
        destination = destination.removeprefix("refs/heads/")
        if destination in ("HEAD", "@", ""):
            destination, explicit_branch = _current_branch(walk) or "", True
        forced = flags.force or spec.startswith("+")
        deleted = flags.delete or (bool(colon) and not source)
        targets.append(_PushTarget(destination or None, forced, deleted, maybe_tag=not explicit_branch))
    return targets, tag_push


def _current_branch(walk: _Walk) -> str | None:
    if walk.ctx.current_branch is None:
        walk.need("current_branch")
    return walk.ctx.current_branch


def _default_branches(walk: _Walk) -> frozenset[str]:
    if walk.ctx.default_branch is None:
        walk.need("default_branch")
        return frozenset({"main", "master"})
    return frozenset({walk.ctx.default_branch})


def _deploy_rules(walk: _Walk) -> DeployBranches:
    if walk.ctx.deploy_branches is None:
        walk.need("deploy_branches")
        return FAIL_CLOSED  # not known yet: every push deploys
    return walk.ctx.deploy_branches


def _any_branch_deploys(rules: DeployBranches) -> bool:
    return rules.every_branch or bool(rules.branches)


def _deploys(walk: _Walk, target: _PushTarget) -> bool:
    rules = _deploy_rules(walk)
    if target.branch is None or target.branch == "*":
        return _any_branch_deploys(rules)
    return rules.covers_branch(target.branch) or (target.maybe_tag and rules.covers_tag(target.branch))


def _judge_push_targets(walk: _Walk, targets: Sequence[_PushTarget], *, tag_push: bool) -> None:
    defaults = _default_branches(walk)
    for target in targets:
        branch = target.branch
        label = branch or "?"
        hits_default = branch is None or branch == "*" or branch in defaults
        if walk.ctx.policy is Policy.SELF_UPDATE and (hits_default or branch == "main"):
            walk.forbid(Reason.SELF_UPDATE_MAIN, label)
        if (target.forced or target.deleted) and hits_default:
            walk.ask(GateClass.FORCE_PUSH, Reason.FORCE_DEFAULT_BRANCH, label)
        if _deploys(walk, target):
            walk.ask(GateClass.DEPLOY_PUSH, Reason.DEPLOY_BRANCH, label)
    if tag_push and _deploy_rules(walk).deploys:  # which tags is often unknown: any deploying push counts
        walk.ask(GateClass.DEPLOY_PUSH, Reason.DEPLOY_BRANCH, "tags")


def _push_remote(walk: _Walk, remote: _Word | None) -> None:
    """A push to a server (not a git host) changes that server: a URL or `user@host:path`, or a remote name whose
    URL is one (the `remote_urls` fact). No remote named = the current branch's push remote."""
    if remote is None:
        name = DEFAULT_PUSH_REMOTE
    else:
        value = walk.value(remote)
        if value is None:
            walk.ask(GateClass.SERVER_WRITE, Reason.PUSH_TO_SERVER, "git push ?")
            return
        if ":" in value or value.startswith(("/", ".", "~")) or "/" in value:  # a remote name has neither
            _judge_push_url(walk, value, label="git push")
            return
        name = value
    label = "git push" if name == DEFAULT_PUSH_REMOTE else f"git push {name}"
    if name in walk.defined_remotes or "?" in walk.defined_remotes:
        url = walk.defined_remotes.get(name)
        if url is None:
            walk.ask(GateClass.SERVER_WRITE, Reason.PUSH_TO_SERVER, f"{label} ?")
        else:
            _judge_push_url(walk, url, label=label)
        return
    if name not in walk.ctx.remote_urls:
        walk.need_remote(name)
        walk.ask(GateClass.SERVER_WRITE, Reason.PUSH_TO_SERVER, f"{label} ?")  # not looked up yet: fail closed
        return
    url = walk.ctx.remote_urls[name]
    if url is not None:  # None: no such remote, so git reads the name as a local path
        _judge_push_url(walk, url, label=label)


def _judge_push_url(walk: _Walk, url: str, *, label: str) -> None:
    host = remote_host(url)
    if host and host not in walk.rules.git_hosts and host not in _LOCAL_HOSTS:
        walk.ask(GateClass.SERVER_WRITE, Reason.PUSH_TO_SERVER, f"{label} → {host}")
        walk.remote_calls.extend(_git_ssh_calls(url))


def remote_host(url: str) -> str:
    """The host a git remote URL reaches, lower case; `""` for a local path or `file://` (SPEC §5.7 `server_write`)."""
    text = url.strip()
    if not text or text.casefold().startswith("file://"):
        return ""
    if "://" in text:
        return _host_of(text)
    return _scp_remote(text) or ""


def _shell_quote(text: str) -> str:
    """git's `sq_quote`: the whole word in single quotes, each `'` as `'\\''`."""
    return "'" + text.replace("'", "'\\''") + "'"


def _git_ssh_calls(url: str) -> list[tuple[str | None, ...]]:
    """The ssh argv git runs to push over an ssh URL (connect.c `fill_ssh_args`), with and without the protocol-v2
    `SendEnv`: what a grant for that push lets through the ssh shim. Empty for other transports."""
    if "://" in url:
        scheme, _, rest = url.partition("://")
        if scheme.casefold() not in ("ssh", "git+ssh", "ssh+git"):
            return []
        netloc, _, path = rest.partition("/")
        path = "/" + path
        path = path[1:] if path.startswith("/~") else path
        user, at, hostport = netloc.rpartition("@")
        if hostport.startswith("["):
            host, _, port = hostport[1:].partition("]")
            port = port.removeprefix(":")
        else:
            host, _, port = hostport.partition(":")
        destination = f"{user}{at}{host}"
    else:
        destination, _, path = url.partition(":")
        port = ""
    command = f"git-receive-pack {_shell_quote(path)}"
    port_args = ("-p", port) if port else ()
    return [
        ("ssh", *port_args, destination, command),
        ("ssh", "-o", "SendEnv=GIT_PROTOCOL", *port_args, destination, command),
    ]


def _git_remote(walk: _Walk, rest: list[_Word]) -> None:
    """`git remote add NAME URL` / `set-url NAME URL` earlier in the call wins over the fetched `remote_urls`."""
    words = _positionals(rest, frozenset({"-t", "--track", "-m", "--master"}))
    verb = walk.value(words[0]) if words else None
    if verb not in ("add", "set-url") or len(words) < 3:
        return
    name, url = walk.value(words[1]), walk.value(words[2])
    walk.defined_remotes["?" if name is None else name] = url


# --- discarding uncommitted work (`fs_destroy` over the owner's pre-turn edits) ---
_CHECKOUT_VALUE_OPTIONS: Final = frozenset({"-b", "-B", "--orphan", "--conflict", "--pathspec-from-file"})


def _discard_label(sub: str, rest: Sequence[_Word]) -> str | None:
    """A label when the git command throws away uncommitted changes: `reset --hard`, a path checkout (`-- .`, `.`,
    `<tree> <path>`, `-f`), `restore` of the worktree, `stash drop|clear`, `switch --discard-changes|-f`."""
    texts = _texts(rest)
    words = _texts(_positionals(rest, _CHECKOUT_VALUE_OPTIONS))
    forced = _has(rest, "--force") or _short_flag(rest, "f")
    staged_only = (_has(rest, "--staged") or _short_flag(rest, "S")) and not (
        _has(rest, "--worktree") or _short_flag(rest, "W")
    )
    discards = {
        "reset": "--hard" in texts,
        "checkout": forced or "." in words or len(words) > 1 or ("--" in texts and texts[-1] != "--"),
        "restore": not staged_only,
        "stash": words[:1] in (["drop"], ["clear"]),
        "switch": forced or "--discard-changes" in texts,
    }
    return " ".join(_strip_markers(text) for text in (sub, *texts))[:120] if discards.get(sub, False) else None


def _discard(walk: _Walk, label: str) -> None:
    """Free unless the tree held changes before the turn started: the agent may throw away its own work, never the
    owner's (orchestrator decision 2026-10-24). Unknown counts as dirty."""
    dirty = walk.ctx.dirty_before_turn
    if dirty is None:
        walk.need("dirty_before_turn")
    if dirty is not False:
        walk.ask(GateClass.FS_DESTROY, Reason.DISCARD_CHANGES, f"git {label}")


def _git_clean(walk: _Walk, rest: list[_Word]) -> None:
    flags = "".join(word.text[1:] for word in rest if re.fullmatch(r"-[A-Za-z]+", word.text))
    if "n" in flags or _has(rest, "--dry-run"):
        return
    if "X" in flags and "x" not in flags:
        return  # only ignored files: never asked
    paths = _positionals(rest, frozenset({"-e", "--exclude"}))
    if not paths:
        walk.delete_path(walk.cwd, glob=False, label="clean", untracked_only=True)
    for word in paths:
        walk.check_word(word, recursive=True)
        value = walk.value(word)
        resolved = walk.path(value) if value is not None else None
        walk.delete_path(resolved, glob=word.glob, label=f"clean {value or '?'}", untracked_only=True)


# --- gh ---
def _h_gh(walk: _Walk, name: str, args: list[_Word], cmd: _Command, level: int) -> _Effect:
    del name, cmd, level
    words = _texts(_positionals(args, frozenset({"-R", "--repo", "--hostname", "-X", "--method", "-f", "-F"})))
    group, verb = [*words, "", ""][:2]
    visibility = [walk.value(word) or "?" for word in _option_values(args, "--visibility")]
    if group == "repo" and verb == "delete":
        walk.ask(GateClass.REPO_DELETE, Reason.REPO_DELETE, " ".join(words[2:3]) or "repo")
    elif group == "repo" and verb in ("create", "edit") and (_has(args, "--public") or "public" in visibility):
        walk.ask(GateClass.REPO_PUBLIC, Reason.REPO_PUBLIC, " ".join(words[2:3]) or "repo")
    elif group == "gist" and verb == "create" and (_has(args, "--public") or _short_flag(args, "p")):
        walk.ask(GateClass.REPO_PUBLIC, Reason.REPO_PUBLIC, "gist")
    elif group == "workflow" and verb == "run":
        walk.ask(GateClass.DEPLOY, Reason.DEPLOY_CLI, "gh workflow run")
    elif group == "release" and verb in ("create", "upload", "edit"):
        walk.ask(GateClass.DEPLOY, Reason.DEPLOY_CLI, f"gh release {verb}")
    elif group == "pr" and verb == "merge":
        if walk.ctx.policy is Policy.SELF_UPDATE:
            walk.forbid(Reason.SELF_UPDATE_MAIN, "gh pr merge")
        elif _any_branch_deploys(_deploy_rules(walk)):  # the PR's base is not in the command: any deploy branch asks
            walk.ask(GateClass.DEPLOY_PUSH, Reason.DEPLOY_BRANCH, "gh pr merge")
        return _Effect.WRITE
    elif group == "api":
        return _gh_api(walk, args, words)
    read = {
        "pr": ("view", "list", "status", "diff", "checks"),
        "issue": ("view", "list", "status"),
        "run": ("view", "list", "watch"),
        "repo": ("view", "list"),
        "release": ("view", "list"),
        "workflow": ("view", "list"),
        "gist": ("view", "list"),
        "auth": ("status",),
        "search": ("repos", "issues", "prs", "code", "commits"),
    }
    if group in ("status", "browse", "help", "version") or verb in read.get(group, ()):
        return _Effect.READ
    return _Effect.WRITE


def _gh_api(walk: _Walk, args: list[_Word], words: list[str]) -> _Effect:
    endpoint = words[1] if len(words) > 1 else ""
    methods = [(walk.value(word) or "?").upper() for word in _option_values(args, "-X", "--method")]
    fields = [walk.value(word) or "?" for word in _option_values(args, "-f", "-F", "--field", "--raw-field")]
    has_body = bool(fields) or _has(args, "--input")
    method = methods[-1] if methods else ("POST" if has_body else "GET")
    folded = " ".join(fields).casefold().replace(" ", "")
    target = endpoint.lstrip("/")
    if method == "DELETE" and target.startswith("repos/"):
        walk.ask(GateClass.REPO_DELETE, Reason.REPO_DELETE, target)
    if "visibility=public" in folded or "private=false" in folded or "visibility:public" in folded:
        walk.ask(GateClass.REPO_PUBLIC, Reason.REPO_PUBLIC, target)
    if "deleterepository" in folded:
        walk.ask(GateClass.REPO_DELETE, Reason.REPO_DELETE, target)
    if _has(args, "--input") and method in ("PATCH", "POST", "PUT") and target.startswith(("repos/", "user/repos")):
        walk.ask(GateClass.REPO_PUBLIC, Reason.REPO_PUBLIC, target)  # an unseen body could set visibility
    return _Effect.READ if method == "GET" and not has_body else _Effect.WRITE


# --- ssh, scp, sftp, rsync, mosh ---
_SSH_VALUE_OPTIONS: Final = _words("-b -B -c -D -E -e -F -I -i -J -L -l -m -O -o -p -Q -R -S -W -w")
_SSH_TUNNEL_OPTIONS: Final = frozenset({"-L", "-R", "-D", "-W", "-w"})
_SSH_LOCAL_COMMAND_KEYS: Final = frozenset({"proxycommand", "localcommand", "permitlocalcommand", "remotecommand"})
_SCP_VALUE_OPTIONS: Final = _words("-c -D -F -i -J -l -o -P -S -X")
_RSYNC_VALUE_OPTIONS: Final = frozenset(
    {
        "-e", "--rsh", "-f", "--filter", "--exclude", "--include", "--exclude-from", "--include-from", "--files-from",
        "--rsync-path", "--log-file", "--partial-dir", "-T", "--temp-dir", "--compare-dest", "--copy-dest",
        "--link-dest", "--backup-dir", "--suffix", "--chmod", "--chown", "--port", "--password-file", "--bwlimit",
        "--timeout", "--max-size", "--min-size", "--max-delete", "-M", "--remote-option",
    }
)  # fmt: skip
_REMOTE_FILTERS: Final = frozenset({"grep", "egrep", "fgrep", "head", "tail", "wc", "sort", "uniq", "cut"})


@dataclass(frozen=True, slots=True)
class _SshOptions:
    destination: int | None  # index of the destination word; None = nothing leaves the Mac (-V, -G, -Q)
    tunnel: bool  # -L/-R/-D/-W/-w/-N or a *Forward option: a way into the server outside the gate
    local_command: bool  # ProxyCommand/LocalCommand run a command on the Mac


def _ssh_options(args: Sequence[_Word]) -> _SshOptions:
    i, tunnel, local_command = 0, False, False
    while i < len(args):
        text = args[i].text
        if text == "--":
            i += 1
            break
        if not text.startswith("-") or text == "-":
            break
        option = text[:2]
        if option in _SSH_VALUE_OPTIONS:
            value = text[2:] or (args[i + 1].text if i + 1 < len(args) else "")
            tunnel = tunnel or option in _SSH_TUNNEL_OPTIONS
            if option == "-o":
                key = re.split(r"[=\s]", value.strip(), maxsplit=1)[0].casefold()
                local_command = local_command or key in _SSH_LOCAL_COMMAND_KEYS
                tunnel = tunnel or key in ("localforward", "remoteforward", "dynamicforward", "tunnel")
            i += 1 if text[2:] else 2
            continue
        if any(letter in text for letter in "VGQ"):
            return _SshOptions(None, tunnel, local_command)
        tunnel = tunnel or "N" in text
        i += 1
    return _SshOptions(i if i < len(args) else None, tunnel, local_command)


def _h_ssh(walk: _Walk, name: str, args: list[_Word], cmd: _Command, level: int) -> _Effect:
    del level
    options = _ssh_options(args)
    if options.destination is None:
        return _Effect.READ
    destination = walk.value(args[options.destination])
    walk.remote_calls.append(tuple(walk.value(word) for word in (_literal_word(name), *args)))
    host = _host_of(destination) if destination is not None else "?"
    problem = "?" if destination is None else "ProxyCommand/LocalCommand" if options.local_command else None
    if problem is None and host in walk.rules.git_hosts:
        return _Effect.READ
    if problem is None and host in walk.rules.jump_only_hosts:
        walk.ask(GateClass.SERVER_WRITE, Reason.JUMP_ONLY_HOST, host)
        return _Effect.WRITE
    if problem is None and options.tunnel:
        problem = "tunnel"
    if problem is None:
        command = _remote_command(walk, args[options.destination + 1 :], cmd)
        if command is not None and walk.remote_read_only(command):
            return _Effect.READ
        problem = (command or "?")[:80]
    walk.ask(GateClass.SERVER_WRITE, Reason.REMOTE_COMMAND, f"{host}: {problem}")
    return _Effect.WRITE


def _remote_command(walk: _Walk, words: Sequence[_Word], cmd: _Command) -> str | None:
    """The text the remote shell runs: the words after the host, or a heredoc on stdin; None if not known now
    (a variable, or an interactive session)."""
    if not words:
        bodies = [body for redirect in cmd.redirects if (body := redirect.stdin_text) is not None]
        return "\n".join(bodies) if bodies else None
    values = [walk.value(word) for word in words]
    if any(value is None for value in values):
        return None
    return " ".join(value for value in values if value is not None)


def _remote_copy(walk: _Walk, name: str, args: list[_Word], value_options: frozenset[str]) -> list[str | None]:
    """Judge an scp/rsync/sftp transfer; returns the operand values."""
    operands = _positionals(args, value_options)
    values = [walk.value(word) for word in operands]
    walk.remote_calls.append(tuple(walk.value(word) for word in (_literal_word(name), *args)))
    if None in values:
        walk.ask(GateClass.SERVER_WRITE, Reason.REMOTE_COPY, f"{name} ?")
        return values
    for index, value in enumerate(values):
        host = _scp_remote(value) if value is not None else None
        if host is None and value is not None and value.startswith("rsync://"):
            host = _host_of(value)
        if host is None or host in walk.rules.git_hosts:
            continue
        if host in walk.rules.jump_only_hosts:
            walk.ask(GateClass.SERVER_WRITE, Reason.JUMP_ONLY_HOST, host)
        elif index == len(values) - 1 or name == "sftp":
            walk.ask(GateClass.SERVER_WRITE, Reason.REMOTE_COPY, f"{name} → {host}")
    return values


def _h_scp(walk: _Walk, name: str, args: list[_Word], cmd: _Command, level: int) -> _Effect:
    del cmd, level
    _remote_copy(walk, name, args, _SCP_VALUE_OPTIONS)
    return _Effect.WRITE


def _h_sftp(walk: _Walk, name: str, args: list[_Word], cmd: _Command, level: int) -> _Effect:
    del cmd, level
    values = _remote_copy(walk, name, args, _SCP_VALUE_OPTIONS)
    if values and values[0] is not None and _scp_remote(values[0]) is None:  # `sftp host`: a session, not a copy
        host = _host_of(values[0])
        if host not in walk.rules.git_hosts and host not in walk.rules.jump_only_hosts:
            walk.ask(GateClass.SERVER_WRITE, Reason.REMOTE_COPY, f"sftp {host}")
    return _Effect.WRITE


def _h_rsync(walk: _Walk, name: str, args: list[_Word], cmd: _Command, level: int) -> _Effect:
    del cmd, level
    operands = _positionals(args, _RSYNC_VALUE_OPTIONS)
    values = _remote_copy(walk, name, args, _RSYNC_VALUE_OPTIONS)
    remote = any(value is not None and (_scp_remote(value) or value.startswith("rsync://")) for value in values)
    if remote and (_has(args, "--remove-source-files", "--rsync-path", "--remote-option") or _has(args, "-M")):
        walk.ask(GateClass.SERVER_WRITE, Reason.REMOTE_COPY, "rsync: remote side effects")
    deletes = any(word.text.startswith("--delete") for word in args)
    if deletes and operands and values[-1] is not None and _scp_remote(values[-1]) is None:
        walk.delete_word(operands[-1])  # `--delete` removes what the source lacks from a local destination
    return _Effect.WRITE


# --- docker ---
_DOCKER_VALUE_OPTIONS: Final = frozenset(
    {"-H", "--host", "-c", "--context", "--config", "-l", "--log-level", "--tlscacert", "--tlscert", "--tlskey"}
)
_COMPOSE_VALUE_OPTIONS: Final = frozenset(
    {"-f", "--file", "-p", "--project-name", "--profile", "--env-file", "--project-directory", "--parallel", "--ansi"}
)
_LOCAL_DOCKER_CONTEXTS: Final = frozenset({"default", "desktop-linux", "orbstack", "colima", "rancher-desktop"})
_DOCKER_READ: Final = frozenset(
    {
        "ps",
        "logs",
        "images",
        "inspect",
        "version",
        "info",
        "stats",
        "top",
        "port",
        "diff",
        "history",
        "events",
        "search",
    }
)


def _h_docker(walk: _Walk, name: str, args: list[_Word], cmd: _Command, level: int) -> _Effect:
    del cmd, level
    i = 0
    while i < len(args) and args[i].text.startswith("-"):
        text = args[i].text
        inline = text.split("=", 1)[1] if "=" in text else None
        value_word = _literal_word(inline) if inline is not None else (args[i + 1] if i + 1 < len(args) else None)
        option = text.split("=", 1)[0]
        value = walk.value(value_word) if value_word is not None else ""
        remote_host = value is None or (_host_of(value) not in _LOCAL_HOSTS and not value.startswith("unix://"))
        if option in ("-H", "--host") and remote_host:
            walk.ask(GateClass.SERVER_WRITE, Reason.REMOTE_DOCKER, value or "?")
        if option in ("-c", "--context") and value not in _LOCAL_DOCKER_CONTEXTS:
            walk.ask(GateClass.SERVER_WRITE, Reason.REMOTE_DOCKER, value or "?")
        i += 2 if option in _DOCKER_VALUE_OPTIONS and inline is None else 1
    rest = args[i:]
    if name == "docker-compose":
        return _compose(walk, rest)
    sub = rest[0].text if rest else ""
    tail = _texts(rest[1:])
    if sub == "compose":
        return _compose(walk, rest[1:])
    if sub == "push" or (sub == "image" and tail[:1] == ["push"]) or (sub == "buildx" and "--push" in tail):
        walk.ask(GateClass.DEPLOY, Reason.DEPLOY_CLI, "docker push")
    elif (sub == "volume" and tail[:1] in (["rm"], ["prune"], ["remove"])) or (
        sub == "system" and tail[:1] == ["prune"]
    ):
        walk.ask(GateClass.DOCKER_DESTROY, Reason.DOCKER_VOLUMES, f"docker {sub} {tail[0]}")
    elif sub == "context" and tail[:1] == ["use"] and (len(tail) < 2 or tail[1] not in _LOCAL_DOCKER_CONTEXTS):
        walk.ask(GateClass.SERVER_WRITE, Reason.REMOTE_DOCKER, f"docker context use {' '.join(tail[1:2])}")
    if sub in _DOCKER_READ or (
        sub in ("container", "image", "volume", "network") and tail[:1] in (["ls"], ["inspect"])
    ):
        return _Effect.READ
    return _Effect.WRITE


def _compose(walk: _Walk, args: list[_Word]) -> _Effect:
    words = _positionals(args, _COMPOSE_VALUE_OPTIONS)
    sub = words[0].text if words else ""
    after = _after(args, words[0]) if words else []
    if sub in ("down", "rm") and (_has(after, "--volumes") or _short_flag(after, "v")):
        walk.ask(GateClass.DOCKER_DESTROY, Reason.DOCKER_VOLUMES, f"compose {sub} -v")
    return _Effect.READ if sub in ("ps", "logs", "config", "images", "ls", "top", "version", "port") else _Effect.WRITE


# --- deploy CLIs (SPEC §5.7 `deploy`) ---
_LISTING_VERBS: Final = frozenset({"list", "ls", "show", "status", "info", "get", "logs"})
_HELP_FLAGS: Final = ("--version", "-V", "--help", "-h", "-v")


@dataclass(frozen=True, slots=True)
class _Tool:
    """`safe` set: every other subcommand (and the bare tool) asks. Otherwise only `asks` ask.
    `listing` subcommands are safe only with a listing verb after them (`fly apps list`)."""

    gate_class: GateClass
    asks: frozenset[str] = frozenset()
    safe: frozenset[str] | None = None
    listing: frozenset[str] = frozenset()
    read: frozenset[str] = frozenset()
    value_options: frozenset[str] = frozenset()


def _tool(
    gate_class: GateClass, *, asks: str = "", safe: str | None = None, listing: str = "", read: str = ""
) -> _Tool:
    return _Tool(
        gate_class,
        asks=frozenset(asks.split()),
        safe=None if safe is None else frozenset(safe.split()),
        listing=frozenset(listing.split()),
        read=frozenset(read.split()),
    )


_DEPLOY: Final = GateClass.DEPLOY
_TOOLS: Final[Mapping[str, _Tool]] = {
    "vercel": _tool(_DEPLOY, safe="dev build whoami help ls list logs inspect login logout link pull switch teams",
                    listing="env projects domains", read="whoami ls list logs inspect"),
    "netlify": _tool(_DEPLOY, asks="deploy env:set env:unset env:import sites:delete functions:deploy"),
    "fly": _tool(_DEPLOY, safe="status logs version help auth info regions releases doctor dashboard platform orgs "
                 "open history checks ping docs",
                 listing="apps machine machines secrets volumes ips image config postgres certs scale",
                 read="status logs version info releases"),
    "railway": _tool(_DEPLOY, safe="status logs whoami help link login logout list open docs version environment",
                     read="status logs whoami list"),
    "heroku": _tool(_DEPLOY, safe="logs ps apps apps:info info releases whoami auth:whoami help login logout status "
                    "version config config:get addons domains regions", read="logs ps apps info releases config"),
    "firebase": _tool(_DEPLOY, asks="deploy hosting:disable functions:delete database:remove database:set "
                      "database:update database:push firestore:delete hosting:channel:deploy"),
    "wrangler": _tool(_DEPLOY, safe="dev whoami login logout init types tail help version build generate docs"),
    "serverless": _tool(_DEPLOY, asks="deploy remove rollback"),
    "sls": _tool(_DEPLOY, asks="deploy remove rollback"),
    "cdk": _tool(_DEPLOY, asks="deploy destroy bootstrap", read="synth diff ls list"),
    "sam": _tool(_DEPLOY, asks="deploy delete sync"),
    "amplify": _tool(_DEPLOY, asks="push publish delete"),
    "surge": _tool(_DEPLOY, safe="list whoami login logout help"),
    "eas": _tool(_DEPLOY, asks="submit update"),
    "fastlane": _tool(_DEPLOY, safe="test tests scan lint lanes list env help init actions action"),
    "kamal": _tool(_DEPLOY, safe="version help config details audit init"),
    "cap": _tool(_DEPLOY, safe=""),
    "dokku": _tool(_DEPLOY, safe="help version"),
    "caprover": _tool(_DEPLOY, asks="deploy"),
    "terraform": _tool(_DEPLOY, asks="apply destroy import taint untaint force-unlock",
                       read="plan validate fmt show output version providers graph"),
    "tofu": _tool(_DEPLOY, asks="apply destroy import taint untaint force-unlock",
                  read="plan validate fmt show output version providers graph"),
    "pulumi": _tool(_DEPLOY, asks="up update destroy import refresh cancel", read="preview whoami version about logs"),
    "helm": _tool(_DEPLOY, asks="install upgrade rollback uninstall delete push",
                  read="list ls status template lint show get history search version env"),
}  # fmt: skip
_REMOTE_SHELL_SUBCOMMANDS: Final = {"fly": "ssh console", "heroku": "run", "railway": "shell"}


def _h_tool(walk: _Walk, name: str, args: list[_Word], cmd: _Command, level: int) -> _Effect:
    tool = _TOOLS[name]
    words = _texts(_positionals(args, tool.value_options))
    sub, second = [*words, "", ""][:2]
    if not words and _has(args, *_HELP_FLAGS):
        return _Effect.READ
    if name == "railway" and sub == "run":  # local command with the production environment
        walk.ask(GateClass.DEPLOY, Reason.DEPLOY_CLI, "railway run")
        return walk.run(args[1:], cmd, level)
    if sub in _REMOTE_SHELL_SUBCOMMANDS.get(name, "").split():
        walk.ask(GateClass.SERVER_WRITE, Reason.REMOTE_COMMAND, f"{name} {sub}")
        return _Effect.WRITE
    listing = sub in tool.listing and second in _LISTING_VERBS
    fail_closed = not listing and (sub not in tool.safe or sub in tool.listing) if tool.safe is not None else False
    asks = fail_closed or sub in tool.asks
    asks = asks or (name == "eas" and sub == "build" and _has(args, "--auto-submit"))
    if name in ("terraform", "tofu"):
        asks = asks or (sub == "state" and second in ("rm", "mv", "push", "replace-provider"))
        asks = asks or (sub == "workspace" and second == "delete")
    if name == "pulumi":
        asks = asks or (sub == "stack" and second == "rm")
    if asks:
        walk.ask(tool.gate_class, Reason.DEPLOY_CLI, f"{name} {sub}".strip())
        return _Effect.WRITE
    return _Effect.READ if sub in tool.read or listing else _Effect.WRITE


_KUBE_VALUE_OPTIONS: Final = _words(
    "-n --namespace --context --kubeconfig -s --server --cluster --user --as --as-group --token -l --selector -o "
    "--output -f --filename -c --container"
)
_KUBE_DEPLOY: Final = _words(
    "apply delete rollout scale set patch edit replace create annotate label cordon uncordon drain taint autoscale "
    "expose run"
)
_KUBE_REMOTE: Final = _words("exec cp attach port-forward debug proxy")
_KUBE_READ: Final = _words(
    "get describe logs top version config explain api-resources api-versions auth diff cluster-info events wait "
    "kustomize completion plugin"
)


def _h_kubectl(walk: _Walk, name: str, args: list[_Word], cmd: _Command, level: int) -> _Effect:
    del cmd, level
    words = _texts(_positionals(args, _KUBE_VALUE_OPTIONS))
    verb, second = [*words, "", ""][:2]
    if verb in _KUBE_READ or not verb or (verb == "rollout" and second in ("status", "history")):
        return _Effect.READ
    if verb in _KUBE_REMOTE:
        walk.ask(GateClass.SERVER_WRITE, Reason.REMOTE_COMMAND, f"{name} {verb}")
    else:
        walk.ask(GateClass.DEPLOY, Reason.DEPLOY_CLI, f"{name} {verb}")
    return _Effect.WRITE


def _h_ansible(walk: _Walk, name: str, args: list[_Word], cmd: _Command, level: int) -> _Effect:
    del cmd, level
    if _has(args, "--check", "-C", "--syntax-check", "--list-hosts", "--list-tasks", "--list-tags", "--version"):
        return _Effect.READ
    if name == "ansible-playbook":
        walk.ask(GateClass.DEPLOY, Reason.DEPLOY_CLI, "ansible-playbook")
    else:
        walk.ask(GateClass.SERVER_WRITE, Reason.REMOTE_COMMAND, name)
    return _Effect.WRITE


def _h_fab(walk: _Walk, name: str, args: list[_Word], cmd: _Command, level: int) -> _Effect:
    del name, cmd, level
    if _has(args, "-l", "--list", "--help", "-h", "--version", "-V"):
        return _Effect.READ
    tasks = _texts(_positionals(args, frozenset({"-H", "--hosts", "-i", "-c", "--config"})))
    if any(_DEPLOY_NAME.match(task) for task in tasks):
        walk.ask(GateClass.DEPLOY, Reason.DEPLOY_TARGET, " ".join(tasks))
    else:
        walk.ask(GateClass.SERVER_WRITE, Reason.REMOTE_COMMAND, "fab " + " ".join(tasks))
    return _Effect.WRITE


# --- cloud CLIs (deploy, spend, data loss); a read verb is free, anything unrecognised asks ---
_AWS_VALUE_OPTIONS: Final = frozenset(
    {"--profile", "--region", "--output", "--endpoint-url", "--query", "--color", "--ca-bundle"}
)
_AWS_SPEND_SERVICES: Final = _words(
    "ec2 rds eks lightsail elasticache redshift opensearch es docdb emr sagemaker autoscaling"
)
_AWS_SPEND_OPS: Final = (
    "run-instances",
    "create-",
    "allocate-",
    "purchase-",
    "request-",
    "start-",
    "modify-",
    "scale-",
)
_AWS_DB_SERVICES: Final = _words("rds dynamodb docdb neptune redshift elasticache timestream")


def _h_aws(walk: _Walk, name: str, args: list[_Word], cmd: _Command, level: int) -> _Effect:
    del name, cmd, level
    words = _texts(_positionals(args, _AWS_VALUE_OPTIONS))
    service, op = [*words, "", ""][:2]
    label = f"aws {service} {op}".strip()
    if not service or service in ("help", "configure", "sts"):
        return _Effect.READ
    if op.startswith(("describe-", "list-", "get-", "head-")) or op in ("ls", "help", "wait", "tail", "presign"):
        return _Effect.READ
    if service == "s3":
        if op in ("rm", "rb"):
            walk.ask(GateClass.FS_DESTROY, Reason.DELETE_OUTSIDE_PROJECT, label)
        elif op in ("cp", "mv", "sync") and words[-1].startswith("s3://"):
            walk.ask(GateClass.DEPLOY, Reason.DEPLOY_CLI, label)
        elif op == "mb":
            walk.ask(GateClass.SPEND, Reason.CLOUD_SPEND, label)
        return _Effect.WRITE
    if service in _AWS_DB_SERVICES and op.startswith("delete-"):
        walk.ask(GateClass.DATA_LOSS_DB, Reason.DROP_DATABASE, label)
    elif service in _AWS_SPEND_SERVICES and op.startswith(_AWS_SPEND_OPS):
        walk.ask(GateClass.SPEND, Reason.CLOUD_SPEND, label)
    else:
        walk.ask(GateClass.DEPLOY, Reason.DEPLOY_CLI, label)
    return _Effect.WRITE


_GCLOUD_VALUE_OPTIONS: Final = frozenset(
    {"--project", "--region", "--zone", "--format", "--account", "--configuration", "--impersonate-service-account"}
)
_GCLOUD_SPEND_GROUPS: Final = _words(
    "compute sql container redis filestore spanner bigtable dataproc memcache alloydb workstations tpus"
)
_GCLOUD_DB_GROUPS: Final = _words("sql spanner bigtable firestore alloydb")
_CLOUD_READ_VERBS: Final = _words(
    "list describe get-iam-policy read tail get-value show get version help login logout account find ls whoami auth"
)


def _h_gcloud(walk: _Walk, name: str, args: list[_Word], cmd: _Command, level: int) -> _Effect:
    del cmd, level
    words = _texts(_positionals(args, _GCLOUD_VALUE_OPTIONS))
    label = " ".join([name, *words[:4]])
    if not words or words[0] in ("config", "auth", "info", "version", "help", "components", "init", "topic"):
        return _Effect.READ
    if any(word in _CLOUD_READ_VERBS for word in words):
        return _Effect.READ
    if "deploy" in words:
        walk.ask(GateClass.DEPLOY, Reason.DEPLOY_CLI, label)
    elif words[0] in _GCLOUD_DB_GROUPS and "delete" in words:
        walk.ask(GateClass.DATA_LOSS_DB, Reason.DROP_DATABASE, label)
    elif words[0] in _GCLOUD_SPEND_GROUPS and any(word in ("create", "resize", "scale") for word in words):
        walk.ask(GateClass.SPEND, Reason.CLOUD_SPEND, label)
    else:
        walk.ask(GateClass.DEPLOY, Reason.DEPLOY_CLI, label)
    return _Effect.WRITE


def _h_cloud(walk: _Walk, name: str, args: list[_Word], cmd: _Command, level: int) -> _Effect:
    """az, doctl, hcloud, linode-cli, vultr-cli, scw: deploy → spend (create, resize) → destroy → read → deploy."""
    del cmd, level
    words = _texts(_positionals(args, frozenset({"-o", "--output", "--context", "-t", "--access-token", "-g"})))
    label = " ".join([name, *words[:4]])
    if any("deploy" in word for word in words):
        walk.ask(GateClass.DEPLOY, Reason.DEPLOY_CLI, label)
    elif any(word.startswith(("create", "resize", "change-type", "rebuild", "upgrade", "scale")) for word in words):
        walk.ask(GateClass.SPEND, Reason.CLOUD_SPEND, label)
    elif not words or any(word in _CLOUD_READ_VERBS for word in words):
        return _Effect.READ
    else:
        walk.ask(GateClass.DEPLOY, Reason.DEPLOY_CLI, label)
    return _Effect.WRITE


def _h_stripe(walk: _Walk, name: str, args: list[_Word], cmd: _Command, level: int) -> _Effect:
    del cmd, level
    words = _texts(_positionals(args, frozenset({"-d", "--data", "--api-key", "-X", "--method"})))
    methods = [word.text.upper() for word in _option_values(args, "-X", "--method")]
    if any(word in ("create", "update", "delete", "post") for word in words) or any(m != "GET" for m in methods):
        walk.ask(GateClass.SPEND, Reason.CLOUD_SPEND, " ".join([name, *words[:3]]))
        return _Effect.WRITE
    return _Effect.READ


# --- databases (SPEC §5.7 `data_loss_db`) ---
_SQL_DESTRUCTIVE: Final = re.compile(
    r"(?ism)\b(drop\s+(table|database|schema|view|materialized|index|collection|column|user|role|owned|extension|"
    r"sequence|type|function|trigger|keyspace)|truncate\b|alter\s+table\s+\S+\s+drop\b|"
    r"delete\s+from\s+[^\s;]+\s*(;|$)|flushall|flushdb|dropdatabase\s*\(|\.drop\s*\(|"
    r"deletemany\s*\(\s*\{\s*\}\s*\)|\.remove\s*\(\s*\{\s*\}\s*\))"
)


@dataclass(frozen=True, slots=True)
class _SqlClient:
    sql_options: tuple[str, ...] = ()  # the option's value is SQL
    file_options: tuple[str, ...] = ()  # the option's value is a file of SQL (content unseen)
    host_options: tuple[str, ...] = ()
    db_options: tuple[str, ...] = ()
    value_options: frozenset[str] = frozenset()
    positional_sql: bool = False  # words after the database are statements (sqlite3, duckdb, redis-cli)
    file_database: bool = False  # the first positional is a database file
    host_env: str = ""
    db_env: str = ""


_PSQL: Final = _SqlClient(
    ("-c", "--command"), ("-f", "--file"), ("-h", "--host"), ("-d", "--dbname"),
    _words("-c --command -f --file -h --host -d --dbname -U --username -p --port -v --set -P -o --output"),
    host_env="PGHOST", db_env="PGDATABASE",
)  # fmt: skip
_MYSQL: Final = _SqlClient(
    ("-e", "--execute"), (), ("-h", "--host"), ("-D", "--database"),
    _words("-e --execute -h --host -D --database -u --user -P --port -S --socket --defaults-file"),
    host_env="MYSQL_HOST",
)  # fmt: skip
_SQLITE: Final = _SqlClient(
    ("-cmd",), ("-init",), value_options=_words("-cmd -init -separator -newline -nullvalue"),
    positional_sql=True, file_database=True,
)  # fmt: skip
_SQL_CLIENTS: Final[Mapping[str, _SqlClient]] = {
    "psql": _PSQL,
    "mysql": _MYSQL,
    "mariadb": _MYSQL,
    "sqlite3": _SQLITE,
    "sqlite": _SQLITE,
    "duckdb": _SqlClient(("-c",), ("-init",), value_options=frozenset({"-c", "-init"}), positional_sql=True,
                         file_database=True),
    "mongosh": _SqlClient(("--eval",), ("--file", "-f"), ("--host",), (),
                          _words("--eval --file -f --host --port -u --username -p --password")),
    "mongo": _SqlClient(("--eval",), ("--file", "-f"), ("--host",), (),
                        _words("--eval --file -f --host --port -u --username -p --password")),
    "clickhouse": _SqlClient(("-q", "--query"), ("--queries-file",), ("-h", "--host"), ("-d", "--database"),
                             _words("-q --query --queries-file -h --host -d --database -u --user --port")),
    "clickhouse-client": _SqlClient(("-q", "--query"), ("--queries-file",), ("-h", "--host"), ("-d", "--database"),
                                    _words("-q --query --queries-file -h --host -d --database -u --user")),
    "redis-cli": _SqlClient((), (), ("-h",), ("-n",), _words("-h -p -a -n -u --user --pass -s"),
                            positional_sql=True),
    "cqlsh": _SqlClient(("-e", "--execute"), ("-f", "--file"), (), ("-k", "--keyspace"),
                        _words("-e --execute -f --file -k --keyspace -u --username -p --password")),
}  # fmt: skip


def _parse_db_url(url: str) -> tuple[str, str | None, str | None]:
    """`(host, database, sqlite_path)` of a DB URL; host `""` = the local default."""
    scheme, _, rest = url.partition("://")
    if scheme.casefold().startswith("sqlite"):
        path = rest[1:] if rest.startswith("/") else rest
        return "", None, path.split("?", 1)[0] or ":memory:"
    netloc, _, path = rest.partition("/")
    return (_host_of(netloc) if netloc else ""), (path.split("?", 1)[0] or None), None


def _h_sql(walk: _Walk, name: str, args: list[_Word], cmd: _Command, level: int) -> _Effect:
    del level
    client = _SQL_CLIENTS[name]
    texts: list[str | None] = [walk.value(word) for word in _option_values(args, *client.sql_options)]
    unseen = bool(_option_values(args, *client.file_options)) or cmd.piped or any(r.op == "<" for r in cmd.redirects)
    positionals = [walk.value(word) for word in _positionals(args, client.value_options)]
    if name.startswith("clickhouse") and positionals[:1] in (["client"], ["local"]):
        positionals = positionals[1:]
    texts.extend(body for redirect in cmd.redirects if (body := redirect.stdin_text) is not None)
    if client.file_database:
        texts.extend(positionals[1:])
    elif client.positional_sql and positionals:
        texts.append(" ".join(value or "?" for value in positionals))
    if not unseen and not any(text is None or _SQL_DESTRUCTIVE.search(text) for text in texts):
        return _Effect.WRITE
    if client.file_database:
        database = positionals[0] if positionals else ":memory:"
        if database is not None and walk.test_sqlite(database):
            return _Effect.WRITE
        walk.ask(GateClass.DATA_LOSS_DB, Reason.SQL_DESTRUCTIVE, f"{name} {database or '?'}")
        return _Effect.WRITE
    urls = [_parse_db_url(value) for value in positionals if value and "://" in value]
    hosts = [walk.value(word) for word in _option_values(args, *client.host_options)]
    names = [walk.value(word) for word in _option_values(args, *client.db_options)]
    names += [value for value in positionals[:1] if value and "://" not in value and not client.positional_sql]
    host = urls[0][0] if urls else hosts[-1] if hosts else walk.var(client.host_env) if client.host_env else ""
    database = urls[0][1] if urls else names[-1] if names else walk.var(client.db_env) if client.db_env else None
    if not walk.test_server_db(host, database):
        walk.ask(GateClass.DATA_LOSS_DB, Reason.SQL_DESTRUCTIVE, f"{name} {database or '?'}@{host or 'localhost'}")
    return _Effect.WRITE


def _h_dropdb(walk: _Walk, name: str, args: list[_Word], cmd: _Command, level: int) -> _Effect:
    del cmd, level
    value_options = _words("-h --host -p --port -U --username")
    positionals = [walk.value(word) for word in _positionals(args, value_options)]
    if name == "mysqladmin":
        if "drop" not in positionals:
            return _Effect.WRITE
        positionals = positionals[positionals.index("drop") + 1 :]
    hosts = [walk.value(word) for word in _option_values(args, "-h", "--host")]
    host = hosts[-1] if hosts else walk.var("PGHOST") if name == "dropdb" else ""
    database = positionals[0] if positionals else None
    if not walk.test_server_db(host, database):
        walk.ask(GateClass.DATA_LOSS_DB, Reason.DROP_DATABASE, f"{database or '?'}@{host or 'localhost'}")
    return _Effect.WRITE


_RAILS_DESTRUCTIVE: Final = (
    "db:drop,db:reset,db:schema:load,db:structure:load,db:rollback,db:migrate:redo,db:migrate:reset,db:migrate:down,"
    "db:purge,db:truncate_all,db:seed:replant"
)
_RAILS_APPLYING: Final = "db:migrate,db:prepare,db:setup,db:migrate:up"
# Migration tools: (destructive verbs, applying verbs). A verb may be two words (`migrate reset`).
_MIGRATIONS: Final[Mapping[str, tuple[frozenset[str], frozenset[str]]]] = {
    tool: (frozenset(destructive.split(",")), frozenset(applying.split(",")))
    for tool, destructive, applying in (
        ("alembic", "downgrade", "upgrade,stamp"),
        ("rake", _RAILS_DESTRUCTIVE, _RAILS_APPLYING),
        ("rails", _RAILS_DESTRUCTIVE, _RAILS_APPLYING),
        ("artisan", "migrate:fresh,migrate:reset,migrate:rollback,migrate:refresh,db:wipe", "migrate"),
        ("prisma", "migrate reset", "migrate deploy,migrate dev,db push"),
        ("knex", "migrate:rollback,migrate:down", "migrate:latest,migrate:up"),
        ("sequelize", "db:migrate:undo,db:migrate:undo:all,db:drop,db:seed:undo,db:seed:undo:all", "db:migrate"),
        ("sequelize-cli", "db:migrate:undo,db:migrate:undo:all,db:drop,db:seed:undo,db:seed:undo:all", "db:migrate"),
        ("typeorm", "migration:revert,schema:drop", "migration:run,schema:sync"),
        ("flyway", "clean,undo", "migrate,repair,baseline"),
        ("liquibase", "rollback,rollback-count,rollback-to-date,rollbackcount,drop-all,dropall", "update"),
        ("goose", "down,reset,down-to", "up,up-to,up-by-one"),
        ("migrate", "down,drop,force", "up,goto"),
        ("dbmate", "down,rollback,drop", "up,migrate"),
        ("supabase", "db reset", "db push"),
        ("drizzle-kit", "drop", "push,migrate"),
        ("sqlx", "database drop,database reset,migrate revert", "migrate run,database setup"),
        ("diesel", "database reset,database drop,migration revert,migration redo", "migration run,database setup"),
        ("mix", "ecto.drop,ecto.reset,ecto.rollback", "ecto.migrate"),
        ("mikro-orm", "schema:drop,schema:fresh,migration:down,migration:fresh", "migration:up,schema:update"),
        ("yoyo", "rollback", "apply"),
        ("manage.py", "flush,reset_db", "migrate"),
        ("django-admin", "flush,reset_db", "migrate"),
    )
}  # fmt: skip
_MIGRATION_VALUE_OPTIONS: Final = _words(
    "-c --config -n --name -x -u --url --database-url --db-url -database --database -e --env --settings"
)
_DB_URL_OPTIONS: Final = ("-x", "-u", "--url", "--database-url", "--db-url", "-database")
_DB_URL_ENV: Final = ("DATABASE_URL", "DB_URL", "SQLALCHEMY_DATABASE_URI", "SQLALCHEMY_URL")
_TEST_ENV: Final = ("RAILS_ENV", "RACK_ENV", "MIX_ENV", "APP_ENV", "NODE_ENV", "DJANGO_ENV", "ENV")
_ALEMBIC_READ: Final = _words("history current heads show check branches")


def _h_migration(walk: _Walk, name: str, args: list[_Word], cmd: _Command, level: int) -> _Effect:
    del cmd, level
    destructive, applying = _MIGRATIONS[name]
    words = _texts(_positionals(args, _MIGRATION_VALUE_OPTIONS))
    one = words[0] if words else ""
    two = " ".join(words[:2])
    verb = two if two in destructive | applying else one
    if name in ("manage.py", "django-admin") and one == "migrate" and "zero" in words:
        verb = "flush"
    if name == "prisma" and two == "db push" and _has(args, "--force-reset", "--accept-data-loss"):
        verb = "migrate reset"
    targets = walk.db_targets(args)
    if verb in destructive:
        if not walk.provably_test(targets, args):
            walk.ask(GateClass.DATA_LOSS_DB, Reason.MIGRATION_DESTRUCTIVE, f"{name} {verb}")
        return _Effect.WRITE
    if verb in applying:
        if name == "supabase" and not _has(args, "--local"):
            walk.ask(GateClass.DATA_LOSS_DB, Reason.MIGRATION_REMOTE_DB, "supabase db push")
        elif not walk.provably_test(targets, args):
            remote = walk.remote_db(targets)
            if remote is not None:
                walk.ask(GateClass.DATA_LOSS_DB, Reason.MIGRATION_REMOTE_DB, f"{name} {verb} → {remote}")
        return _Effect.WRITE
    return _Effect.READ if name == "alembic" and one in _ALEMBIC_READ else _Effect.WRITE


# --- files (SPEC §5.7 `fs_destroy`) ---
def _h_rm(walk: _Walk, name: str, args: list[_Word], cmd: _Command, level: int) -> _Effect:
    del name, cmd, level
    ended = False
    for word in args:
        if ended or not word.text.startswith("-") or word.text == "-":
            walk.delete_word(word)
        elif word.text == "--":
            ended = True
    return _Effect.WRITE


_FIND_LEADING: Final = frozenset({"-H", "-L", "-P", "-E", "-X", "-s", "-x", "-d"})
_FIND_WRITES: Final = frozenset({"-fprint", "-fprint0", "-fprintf", "-fls"})
_DELETERS: Final = frozenset({"rm", "rmdir", "unlink", "shred", "srm"})


def _h_find(walk: _Walk, name: str, args: list[_Word], cmd: _Command, level: int) -> _Effect:
    del name
    i = 0
    while i < len(args) and args[i].text in _FIND_LEADING:
        i += 1
    starts: list[_Word] = []
    while i < len(args) and not args[i].text.startswith(("-", "(", "!", ")")):
        starts.append(args[i])
        i += 1
    expression = args[i:]
    names = [walk.value(word) or "*" for word in _option_values(expression, "-name", "-iname", "-path", "-ipath")]
    deleting = any(word.text == "-delete" for word in expression)
    effect = _Effect.WRITE if any(word.text in _FIND_WRITES for word in expression) else _Effect.READ
    k = 0
    while k < len(expression):
        if expression[k].text in ("-exec", "-execdir", "-ok", "-okdir"):
            end = next((j for j in range(k + 1, len(expression)) if expression[j].text in (";", "+")), len(expression))
            inner = [_UNKNOWN_WORD if "{}" in word.text else word for word in expression[k + 1 : end]]
            if inner and posixpath.basename(inner[0].text) in _DELETERS:
                deleting = True
            elif inner:
                walk.run(inner, cmd, level)
            effect = _Effect.WRITE
            k = end
        k += 1
    for start in starts or [_literal_word(".")]:
        walk.check_word(start, recursive=True)
        if deleting:
            value = walk.value(start)
            resolved = walk.path(value) if value is not None else None
            walk.delete_path(resolved, glob=start.glob, label=f"find {value or '?'} -delete", names=names)
    return _Effect.WRITE if deleting else effect


def _h_dd(walk: _Walk, name: str, args: list[_Word], cmd: _Command, level: int) -> _Effect:
    del name, cmd, level
    for word in args:
        value = walk.value(word) or ""
        if value.startswith("of=/dev/") and _canonical(value[3:]) not in _DEV_SINKS:
            walk.ask(GateClass.FS_DESTROY, Reason.DELETE_OUTSIDE_PROJECT, value[3:])
    return _Effect.WRITE


_DISKUTIL_DESTROY: Final = _words(
    "erasedisk erasevolume reformat zerodisk randomdisk secureerase partitiondisk deletevolume deletecontainer "
    "splitpartition mergepartitions"
)


def _h_diskutil(walk: _Walk, name: str, args: list[_Word], cmd: _Command, level: int) -> _Effect:
    del cmd, level
    words = [text.casefold() for text in _texts(_positionals(args))]
    if any(word in _DISKUTIL_DESTROY for word in words[:2]) or name.startswith(("mkfs", "newfs")):
        walk.ask(GateClass.FS_DESTROY, Reason.DELETE_OUTSIDE_PROJECT, " ".join([name, *words[:3]]))
        return _Effect.WRITE
    return _Effect.READ if words[:1] in (["list"], ["info"], ["apfs"]) else _Effect.WRITE


# --- HTTP and mail (SPEC §5.7 `third_party_send`) ---
def _url_host(url: str) -> str:
    if url.startswith(":"):  # httpie's `:3000/path` is localhost
        return "localhost"
    return _host_of(url)


_CURL_VALUE_OPTIONS: Final = _words(
    "-X --request -H --header -d --data --data-raw --data-binary --data-urlencode --data-ascii -F --form --form-string "
    "-T --upload-file --json -o --output -u --user -A --user-agent -e --referer -b --cookie -c --cookie-jar -K "
    "--config --url -m --max-time --connect-timeout -w --write-out -x --proxy --resolve --cacert --cert --key -r "
    "--range -C --continue-at --retry -E --limit-rate -y -Y -z --time-cond --interface"
)
_CURL_DATA: Final = (
    "-d", "--data", "--data-raw", "--data-binary", "--data-urlencode", "--data-ascii", "-F", "--form", "--form-string",
    "--json",
)  # fmt: skip
_SAFE_METHODS: Final = frozenset({"GET", "HEAD", "OPTIONS"})


def _h_curl(walk: _Walk, name: str, args: list[_Word], cmd: _Command, level: int) -> _Effect:
    del cmd, level
    methods = [(walk.value(word) or "?").upper() for word in _option_values(args, "-X", "--request")]
    data = _option_values(args, *_CURL_DATA)
    upload = _option_values(args, "-T", "--upload-file")
    method = (
        methods[-1] if methods else "PUT" if upload else "POST" if data and not _has(args, "-G", "--get") else "GET"
    )
    config = bool(_option_values(args, "-K", "--config"))
    writes_file = _has(args, "-o", "--output", "-O", "--remote-name", "--remote-name-all") or _short_flag(args, "O")
    if method in _SAFE_METHODS and not config:
        return _Effect.WRITE if writes_file else _Effect.READ
    urls = [walk.value(word) for word in [*_positionals(args, _CURL_VALUE_OPTIONS), *_option_values(args, "--url")]]
    walk.http_write(method, urls or [None], " ".join(walk.value(word) or "?" for word in data), label=name)
    return _Effect.WRITE


_WGET_VALUE_OPTIONS: Final = _words(
    "-O --output-document -o --output-file -P --directory-prefix -U --user-agent --header -e --execute -i "
    "--input-file -t --tries -T --timeout --post-data --post-file --body-data --body-file --method --user "
    "--password"
)


def _h_wget(walk: _Walk, name: str, args: list[_Word], cmd: _Command, level: int) -> _Effect:
    del cmd, level
    methods = [(walk.value(word) or "?").upper() for word in _option_values(args, "--method")]
    data = _option_values(args, "--post-data", "--post-file", "--body-data", "--body-file")
    method = methods[-1] if methods else "POST" if data else "GET"
    if method in _SAFE_METHODS:
        outputs = [walk.value(word) for word in _option_values(args, "-O", "--output-document")]
        to_stdout = "-" in outputs or _has(args, "-O-", "--spider")
        return _Effect.READ if to_stdout else _Effect.WRITE
    urls = [walk.value(word) for word in _positionals(args, _WGET_VALUE_OPTIONS)]
    walk.http_write(method, urls or [None], " ".join(walk.value(word) or "?" for word in data), label=name)
    return _Effect.WRITE


_HTTPIE_METHODS: Final = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})
_HTTPIE_VALUE_OPTIONS: Final = _words(
    "-a --auth -A --auth-type --session --session-read-only -o --output --verify --cert --cert-key --proxy "
    "--timeout -p --print"
)


def _h_httpie(walk: _Walk, name: str, args: list[_Word], cmd: _Command, level: int) -> _Effect:
    del cmd, level
    positionals = [walk.value(word) for word in _positionals(args, _HTTPIE_VALUE_OPTIONS)]
    method: str | None = None
    if len(positionals) > 1 and positionals[0] is not None and positionals[0].upper() in _HTTPIE_METHODS:
        method = positionals.pop(0)
        method = method.upper() if method else None
    url, items = (positionals[0] if positionals else None), positionals[1:]
    data = [item for item in items if item is None or ("==" not in item and ("=" in item or "@" in item))]
    form = _has(args, "-f", "--form", "--raw", "--multipart")
    mutating = method not in _SAFE_METHODS if method else bool(data) or form
    if not mutating:
        return _Effect.WRITE if _has(args, "-d", "--download", "-o", "--output") else _Effect.READ
    walk.http_write(method or "POST", [url], " ".join(item or "?" for item in items), label=name)
    return _Effect.WRITE


def _h_mail(walk: _Walk, name: str, args: list[_Word], cmd: _Command, level: int) -> _Effect:
    del args, cmd, level
    walk.ask(GateClass.THIRD_PARTY_SEND, Reason.MAIL, name)
    return _Effect.WRITE


# --- packages: publish (third_party_send), scripts and targets (deploy*), runners (inner commands) ---
_PUBLISH_VERBS: Final[Mapping[str, frozenset[str]]] = {
    "twine": frozenset({"upload", "register"}),
    "cargo": frozenset({"publish", "yank", "owner"}),
    "gem": frozenset({"push", "yank", "owner"}),
    "flit": frozenset({"publish"}),
    "mvn": frozenset({"deploy"}),
    "dotnet": frozenset({"push"}),
    "gradle": frozenset({"publish"}),
    "gradlew": frozenset({"publish"}),
}


def _h_publish(walk: _Walk, name: str, args: list[_Word], cmd: _Command, level: int) -> _Effect:
    del cmd, level
    words = [text.casefold() for text in _texts(_positionals(args))]
    verbs = _PUBLISH_VERBS[name]
    gradle = name.startswith("gradle")
    if any(word in verbs or (gradle and word.lstrip(":").split(":")[-1].startswith("publish")) for word in words):
        walk.ask(GateClass.THIRD_PARTY_SEND, Reason.PACKAGE_PUBLISH, " ".join([name, *words[:2]]))
    return _Effect.WRITE


_MAKE_VALUE_OPTIONS: Final = _words(
    "-C --directory -f --file --makefile -I --include-dir -o --old-file -W --what-if --assume-new"
)


def _h_make(walk: _Walk, name: str, args: list[_Word], cmd: _Command, level: int) -> _Effect:
    del name, cmd
    directory = walk.cwd
    for word in _option_values(args, "-C", "--directory"):
        value = walk.value(word)
        directory = walk.path(value) if value is not None and directory is not None else None
    files = _option_values(args, "-f", "--file", "--makefile")
    makefile = walk.value(files[-1]) if files else ""
    targets = [
        word for word in _positionals(args, _MAKE_VALUE_OPTIONS) if "=" not in word.text and not word.text.isdigit()
    ]
    if directory is None or makefile is None:
        walk.ask(None, Reason.OPAQUE_COMMAND, "make")
        return _Effect.WRITE
    judged = True
    target_names = [walk.value(target) for target in targets] if targets else [""]  # "" = the default goal
    for target_name in target_names:
        if target_name is None:
            walk.ask(None, Reason.OPAQUE_COMMAND, "make ?")
            judged = False
            continue
        if _DEPLOY_NAME.match(target_name):
            walk.ask(GateClass.DEPLOY, Reason.DEPLOY_TARGET, f"make {target_name}")
        ref = ScriptRef("make", f"{makefile}:{target_name}" if makefile else target_name, directory)
        judged = walk.recipe(ref, level) and judged
    return _Effect.TRANSPARENT if judged else _Effect.WRITE


_JUST_VALUE_OPTIONS: Final = _words(
    "-f --justfile -d --working-directory --dotenv-filename --dotenv-path --shell --shell-arg --color"
)


def _h_just(walk: _Walk, name: str, args: list[_Word], cmd: _Command, level: int) -> _Effect:
    del name, cmd
    if _has(args, "-l", "--list", "--summary", "--show", "--dump", "--evaluate", "--variables", "--help", "--version"):
        return _Effect.READ
    i, directory = 0, walk.cwd
    while i < len(args) and args[i].text.startswith("-"):
        text = args[i].text
        if text in ("-d", "--working-directory") and i + 1 < len(args):
            value = walk.value(args[i + 1])
            directory = walk.path(value) if value is not None else None
        i += 3 if text == "--set" else 2 if text in _JUST_VALUE_OPTIONS else 1
    recipe = walk.value(args[i]) if i < len(args) else ""
    if recipe is None or directory is None:
        walk.ask(None, Reason.OPAQUE_COMMAND, "just ?")
        return _Effect.WRITE
    if _DEPLOY_NAME.match(recipe):
        walk.ask(GateClass.DEPLOY, Reason.DEPLOY_TARGET, f"just {recipe}")
    return _Effect.TRANSPARENT if walk.recipe(ScriptRef("just", recipe, directory), level) else _Effect.WRITE


_NPM_BUILTINS: Final = _words(
    "install i ci add uninstall remove rm un update up upgrade audit ls list ll la outdated view info show v pack "
    "version init create config c cache prune dedupe ddp rebuild rb link ln doctor help explain why pkg fund login "
    "logout whoami adduser search s ping root bin prefix query sbom set get import licenses store env patch "
    "patch-commit setup self-update node workspaces build pm"
)
_NPM_READ: Final = _words(
    "ls list ll la outdated view info show v help explain why audit fund whoami search s ping root bin prefix query "
    "licenses"
)
_NPM_PUBLISH: Final = frozenset({"publish", "unpublish", "deprecate", "dist-tag", "owner", "access"})
_NPM_EXEC: Final = frozenset({"exec", "x", "dlx"})
_NPM_SCRIPT_ALIASES: Final = {"t": "test", "tst": "test", "test": "test", "start": "start", "stop": "stop",
                              "restart": "restart"}  # fmt: skip
_NPM_INSTALLS: Final = frozenset({"install", "i", "ci"})
_INSTALL_SCRIPTS: Final = ("preinstall", "install", "postinstall", "prepare")
_NPM_VALUE_OPTIONS: Final[Mapping[str, frozenset[str]]] = {
    "npm": frozenset({"--prefix", "-w", "--workspace", "-C"}),
    "pnpm": frozenset({"-C", "--dir", "--filter", "-F", "--workspace-dir"}),
    "yarn": frozenset({"--cwd"}),
    "bun": frozenset({"--cwd", "--filter", "-F"}),
}


def _h_npm(  # noqa: PLR0911, PLR0912  # npm, pnpm, yarn and bun share one command grammar
    walk: _Walk, name: str, args: list[_Word], cmd: _Command, level: int
) -> _Effect:
    value_options = _NPM_VALUE_OPTIONS[name]
    positional = _positionals(args, value_options)
    words = _texts(positional)
    directory = walk.cwd
    for word in _option_values(args, "--prefix", "-C", "--dir", "--cwd"):
        value = walk.value(word)
        directory = walk.path(value) if value is not None else None
    sub = words[0] if words else ("install" if name != "npm" else "help")
    after = _after(args, positional[0]) if positional else []
    if sub in _NPM_PUBLISH or (name == "yarn" and words[:2] == ["npm", "publish"]):
        walk.ask(GateClass.THIRD_PARTY_SEND, Reason.PACKAGE_PUBLISH, f"{name} {sub}")
        return _Effect.WRITE
    if sub in _NPM_EXEC:
        inner = [word for word in after if word.text != "--"]
        calls = _option_values(inner, "-c", "--call")
        if calls:
            return walk.inline_shell(calls[:1], level)
        start = _first_positional(inner, frozenset({"-p", "--package"}))
        return walk.run(inner[start:], cmd, level) if inner[start:] else _Effect.WRITE
    if name == "yarn" and sub == "workspace" and len(after) > 1:
        return _h_npm(walk, name, after[1:], cmd, level)
    scripts: list[str] = []
    if sub in ("run", "run-script", "rum", "urn"):
        if len(words) < 2:
            return _Effect.READ
        scripts = [words[1]]
    elif sub in _NPM_SCRIPT_ALIASES:
        scripts = [_NPM_SCRIPT_ALIASES[sub]]
    elif sub in _NPM_BUILTINS:
        if sub in _NPM_INSTALLS and len(words) == 1:
            scripts = list(_INSTALL_SCRIPTS)  # a project install runs its lifecycle scripts
        else:
            return _Effect.READ if sub in _NPM_READ else _Effect.WRITE
    elif name == "bun" and posixpath.splitext(sub)[1] in (".ts", ".tsx", ".js", ".mjs", ".cjs"):
        return walk.script_file(sub, level, as_shell=False)
    else:
        scripts = [sub]  # pnpm, yarn and bun run a script by its bare name
    if directory is None:
        walk.ask(None, Reason.OPAQUE_COMMAND, f"{name} {sub}")
        return _Effect.WRITE
    judged = True
    for script in scripts:
        if _DEPLOY_NAME.match(script):
            walk.ask(GateClass.DEPLOY, Reason.DEPLOY_TARGET, f"{name} {script}")
        names = [script] if script in _INSTALL_SCRIPTS else [f"pre{script}", script, f"post{script}"]
        for item in names:
            judged = walk.recipe(ScriptRef("npm", item, directory), level) and judged
    return _Effect.TRANSPARENT if judged else _Effect.WRITE


_UV_RUN_VALUE_OPTIONS: Final = _words(
    "--with --with-editable --with-requirements --python -p --directory --project --package --env-file --extra "
    "--group --only-group --no-group --index --index-url --default-index --extra-index-url --config-file "
    "--cache-dir --from"
)


def _h_uv(walk: _Walk, name: str, args: list[_Word], cmd: _Command, level: int) -> _Effect:
    del name
    positional = _positionals(args, _UV_RUN_VALUE_OPTIONS)
    words = _texts(positional)
    sub, second = [*words, "", ""][:2]
    if sub == "publish":
        walk.ask(GateClass.THIRD_PARTY_SEND, Reason.PACKAGE_PUBLISH, "uv publish")
        return _Effect.WRITE
    if sub in ("run", "tool") and positional:
        anchor = positional[1] if sub == "tool" and len(positional) > 1 else positional[0]
        inner = _after(args, anchor)
        modules = _option_values(inner, "-m", "--module")
        if modules:
            module = walk.value(modules[0]) or ""
            rest = _after(inner, modules[0])
            return walk.python_module(module, rest, cmd, level)
        start = _first_positional(inner, _UV_RUN_VALUE_OPTIONS)
        return walk.run(inner[start:], cmd, level) if inner[start:] else _Effect.WRITE
    if sub in ("tree", "version", "help") or (sub == "pip" and second in ("list", "show", "freeze", "check", "tree")):
        return _Effect.READ
    return _Effect.WRITE


_RUNNER_EXEC: Final = {"poetry": "run", "pipenv": "run", "pdm": "run", "hatch": "run", "rye": "run", "bundle": "exec",
                       "pixi": "run", "conda": "run"}  # fmt: skip
_RUNNER_PUBLISH: Final = frozenset({"poetry", "pdm", "hatch", "rye"})


def _h_runner(walk: _Walk, name: str, args: list[_Word], cmd: _Command, level: int) -> _Effect:
    positional = _positionals(args, frozenset({"-C", "--directory", "-p", "--project", "-n", "--name", "-e", "--env"}))
    words = _texts(positional)
    sub = words[0] if words else ""
    if sub == _RUNNER_EXEC[name]:
        inner = _after(args, positional[0])
        start = _first_positional(inner, frozenset({"-n", "--name", "-e", "--env"}))
        return walk.run(inner[start:], cmd, level) if inner[start:] else _Effect.WRITE
    if sub == "publish" and name in _RUNNER_PUBLISH:
        walk.ask(GateClass.THIRD_PARTY_SEND, Reason.PACKAGE_PUBLISH, f"{name} publish")
        return _Effect.WRITE
    return _Effect.READ if sub in ("show", "check", "version", "list", "info") else _Effect.WRITE


def _h_pip(walk: _Walk, name: str, args: list[_Word], cmd: _Command, level: int) -> _Effect:
    del walk, name, cmd, level
    words = _texts(_positionals(args))
    reads = ("list", "show", "freeze", "check", "help", "index", "inspect")
    return _Effect.READ if (words[:1] and words[0] in reads) or _has(args, "--version", "-V") else _Effect.WRITE


# --- process control and the keychain (forbidden: SPEC §5.7) ---
_BOT_PROCESS_NAMES: Final = ("python3.12", "python3", "python", "Python")


def _h_kill(walk: _Walk, name: str, args: list[_Word], cmd: _Command, level: int) -> _Effect:
    del name, cmd, level
    if _has(args, "-l", "-L"):
        return _Effect.READ
    targets = args
    if args and args[0].text == "-s":
        targets = args[2:]
    elif args and args[0].text.startswith("-") and len(args) > 1:
        targets = args[1:]  # the first option is the signal
    for word in targets:
        value = walk.value(word)
        if value is None:
            if any(item in word.raw.casefold() for item in _BOT_PROCESS_WORDS):
                walk.forbid(Reason.BOT_CONTROL, word.raw)
            continue
        number = value.lstrip("-")
        if value == "-1" or (number.isdigit() and int(number) in walk.ctx.protected_pids):
            walk.forbid(Reason.BOT_CONTROL, value)
    return _Effect.WRITE


def _h_pkill(walk: _Walk, name: str, args: list[_Word], cmd: _Command, level: int) -> _Effect:
    del cmd, level
    full = name == "pkill" and _short_flag(args, "f")
    regex = name == "pkill" or _short_flag(args, "m")
    value_options = _words("-u -U -g -G -P -s -t -F --signal -c")
    sample = f"{walk.ctx.data_dir}/releases/current/.venv/bin/python3.12 -m yordamchi.bots.dev"
    samples = (sample,) if full else _BOT_PROCESS_NAMES
    for word in _positionals(args, value_options):
        value = walk.value(word)
        if value is None:
            walk.forbid(Reason.BOT_CONTROL, word.raw)
            continue
        if any(item in value.casefold() for item in ("yordamchi", LAUNCHD_PREFIX)):
            walk.forbid(Reason.BOT_CONTROL, value)
            continue
        try:
            hit = any(
                re.search(value, text, re.IGNORECASE) if regex else value.casefold() == text.casefold()
                for text in samples
            )
        except re.error:
            hit = True
        if hit:
            walk.forbid(Reason.BOT_CONTROL, value)
    return _Effect.WRITE


_LAUNCHCTL_READ: Final = _words(
    "list print print-disabled blame dumpstate help version getenv managerpid manageruid managername"
)


def _h_launchctl(walk: _Walk, name: str, args: list[_Word], cmd: _Command, level: int) -> _Effect:
    del name, cmd, level
    values = [walk.value(word) for word in args]
    for word, value in zip(args, values, strict=True):
        if LAUNCHD_PREFIX in (value if value is not None else word.raw).casefold():
            walk.forbid(Reason.BOT_CONTROL, value or word.raw)
    if values[:1] == ["setenv"] and len(values) > 1 and values[1] is not None:
        walk.check_env_name(values[1], values[2] if len(values) > 2 else None)
    return _Effect.READ if values[:1] and values[0] in _LAUNCHCTL_READ else _Effect.WRITE


_KEYCHAIN_SECRETS: Final = frozenset(
    {"dump-keychain", "export", "import", "unlock-keychain", "delete-keychain", "set-key-partition-list", "find-key"}
)
_KEYCHAIN_READ: Final = _words(
    "list-keychains default-keychain login-keychain show-keychain-info verify-cert find-certificate find-identity "
    "cms help"
)


def _h_security(walk: _Walk, name: str, args: list[_Word], cmd: _Command, level: int) -> _Effect:
    del name, cmd, level
    sub = (walk.value(args[0]) or "?") if args else ""
    if "password" in sub or sub in _KEYCHAIN_SECRETS or sub == "?":
        walk.forbid(Reason.KEYCHAIN, f"security {sub}")
        return _Effect.WRITE
    return _Effect.READ if sub in _KEYCHAIN_READ or not sub else _Effect.WRITE


def _h_mkdir(walk: _Walk, name: str, args: list[_Word], cmd: _Command, level: int) -> _Effect:
    """`mkdir`/`touch` create, never destroy: free in FULL; while planning only inside the planning paths."""
    del name, cmd, level
    operands = _positionals(args, frozenset({"-m", "--mode", "-t", "-d", "-r", "--reference"}))
    return _Effect.PLAN_WRITE if operands and all(walk.plan_path(word) for word in operands) else _Effect.WRITE


# --- shell builtins with state ---
def _h_cd(walk: _Walk, name: str, args: list[_Word], cmd: _Command, level: int) -> _Effect:
    del cmd, level
    operands = [word for word in args if not word.text.startswith("-") or word.text == "-"]
    if name == "popd":
        walk.cwd = None
    else:
        walk.chdir(operands[0] if operands else None)
    return _Effect.READ


def _h_export(walk: _Walk, name: str, args: list[_Word], cmd: _Command, level: int) -> _Effect:
    del name, cmd, level
    for word in args:
        if _is_assignment(word):
            walk.assignment(word, persist=True)
    return _Effect.READ


def _h_unset(walk: _Walk, name: str, args: list[_Word], cmd: _Command, level: int) -> _Effect:
    del name, cmd, level
    for word in args:
        if not word.text.startswith("-"):
            walk.unset(word.text)
    return _Effect.READ


# --- read-only programs (READ_ONLY and PLAN_ONLY Bash) ---
_READ_ONLY_PROGRAMS: Final = _words(
    """
    ls cat head tail wc pwd echo printf date whoami uname uptime df du ps which type file stat tree grep egrep fgrep
    rg ag ack cut tr nl diff cmp comm jq yq basename dirname realpath readlink id groups printenv true false test [ [[
    lsof netstat sw_vers vm_stat column fold md5 shasum sha256sum sha1sum md5sum base64 xxd hexdump od strings seq
    sleep expr locale tput pgrep last w who man help hostname sysctl pmset defaults top mdfind mdls plutil codesign
    otool nm lipo xcode-select xcrun system_profiler sed awk gawk sort uniq : set shopt exit return wait jobs alias
    hash umask ulimit read history ioreg scutil csrutil spctl env"""
)
_SED_SUBST: Final = re.compile(r"s(.)(?:\\.|(?!\1).)*\1(?:\\.|(?!\1).)*\1([A-Za-z0-9]*)")
_SED_ADDRESS: Final = re.compile(r"/(?:\\.|[^/\\])*/")
_AWK_WRITES: Final = re.compile(r"system\s*\(|\|\s*\"|\bprintf?\b[^;{}]*>|getline|close\s*\(|fflush")


def _sed_writes(script: str) -> bool:
    if any(set(match.group(2)) & set("wWe") for match in _SED_SUBST.finditer(script)):
        return True
    rest = _SED_ADDRESS.sub("", _SED_SUBST.sub(";", script))
    return re.search(r"[wWe]", rest) is not None


def _local_effect(key: str, values: Sequence[str | None]) -> _Effect:  # noqa: PLR0911  # one rule per program
    """READ for a program on the read-only list whose arguments write nothing; WRITE otherwise."""
    if key not in _READ_ONLY_PROGRAMS:
        if len(values) == 1 and values[0] in ("--version", "-V", "--help", "version"):
            return _Effect.READ
        return _Effect.WRITE
    texts = [value or "" for value in values]
    options = [text for text in texts if text.startswith("-")]
    operands = [text for text in texts if not text.startswith("-")]
    if key in ("sed", "awk", "gawk") and None in values:
        return _Effect.WRITE
    if key == "sed":
        if any(text.startswith(("-i", "--in-place", "-I")) for text in options):
            return _Effect.WRITE
        scripts = [texts[i + 1] for i, text in enumerate(texts[:-1]) if text in ("-e", "--expression")] or operands[:1]
        return _Effect.WRITE if any(_sed_writes(script) for script in scripts) else _Effect.READ
    if key in ("awk", "gawk"):
        inplace = "inplace" in texts or any(text.startswith("-f") for text in options)
        return _Effect.WRITE if inplace or any(_AWK_WRITES.search(text) for text in operands[:1]) else _Effect.READ
    writes = {
        "sort": any(text.startswith(("-o", "--output")) for text in options),
        "uniq": len(operands) > 1,
        "hostname": bool(operands),
        "sysctl": "-w" in options or any("=" in text for text in operands),
        "pmset": "-g" not in options,
        "defaults": operands[:1] not in (["read"], ["read-type"], ["domains"], ["find"]),
        "date": any(not text.startswith("+") for text in operands),
        "yq": any(text in ("-i", "--inplace") for text in options),
        "xxd": "-r" in options and len(operands) > 1,
        "base64": any(text.startswith(("-o", "--output")) for text in options),
        "env": bool(operands),  # `env cmd` is a wrapper (handled elsewhere); here only bare `env`
    }
    return _Effect.WRITE if writes.get(key, False) else _Effect.READ


_PROGRAM_ALIASES: Final = {"flyctl": "fly", "nodejs": "node", "pypy": "python", "pypy3": "python", "gmake": "make",
                           "sequelize-cli": "sequelize", "mkfs.ext4": "mkfs"}  # fmt: skip


def _program_key(base: str) -> str:
    key = base.casefold()  # APFS is case-insensitive: `SSH` runs ssh
    if re.fullmatch(r"python[0-9.]*", key):
        return "python"
    if re.fullmatch(r"pip[0-9.]*", key):
        return "pip"
    if key.startswith(("mkfs", "newfs")):
        return "mkfs"
    return _PROGRAM_ALIASES.get(key, key)


_HANDLERS: Final[Mapping[str, _Handler]] = {
    **dict.fromkeys(_WRAPPERS, _Walk.h_wrapper),
    "env": _Walk.h_env,
    "sudo": _Walk.h_sudo,
    "eval": _Walk.h_eval,
    "watch": _Walk.h_watch,
    "xargs": _Walk.h_xargs,
    "trap": _Walk.h_trap,
    "source": _Walk.h_source,
    ".": _Walk.h_source,
    **dict.fromkeys(_SHELLS, _Walk.h_shell),
    **dict.fromkeys(_INLINE_CODE, _Walk.h_interpreter),
    "git": _h_git,
    "gh": _h_gh,
    "ssh": _h_ssh,
    "mosh": _h_ssh,
    "scp": _h_scp,
    "sftp": _h_sftp,
    "rsync": _h_rsync,
    **dict.fromkeys(("docker", "podman", "nerdctl", "docker-compose"), _h_docker),
    **dict.fromkeys(_TOOLS, _h_tool),
    **dict.fromkeys(("kubectl", "oc"), _h_kubectl),
    **dict.fromkeys(("ansible", "ansible-playbook"), _h_ansible),
    "fab": _h_fab,
    "aws": _h_aws,
    "gcloud": _h_gcloud,
    **dict.fromkeys(("az", "doctl", "hcloud", "linode-cli", "vultr-cli", "scw"), _h_cloud),
    "stripe": _h_stripe,
    **dict.fromkeys(_SQL_CLIENTS, _h_sql),
    **dict.fromkeys(("dropdb", "mysqladmin"), _h_dropdb),
    **dict.fromkeys(_MIGRATIONS, _h_migration),
    **dict.fromkeys(_DELETERS, _h_rm),
    "find": _h_find,
    "dd": _h_dd,
    **dict.fromkeys(("diskutil", "mkfs"), _h_diskutil),
    "curl": _h_curl,
    "wget": _h_wget,
    **dict.fromkeys(("http", "https", "xh", "xhs"), _h_httpie),
    **dict.fromkeys(("mail", "mailx", "sendmail", "msmtp", "swaks", "mutt", "telegram-send"), _h_mail),
    **dict.fromkeys(_PUBLISH_VERBS, _h_publish),
    "make": _h_make,
    "just": _h_just,
    **dict.fromkeys(("npm", "pnpm", "yarn", "bun"), _h_npm),
    "uv": _h_uv,
    **dict.fromkeys(_RUNNER_EXEC, _h_runner),
    "pip": _h_pip,
    "kill": _h_kill,
    **dict.fromkeys(("pkill", "killall"), _h_pkill),
    "launchctl": _h_launchctl,
    "security": _h_security,
    **dict.fromkeys(("mkdir", "touch"), _h_mkdir),
    **dict.fromkeys(("cd", "pushd", "popd"), _h_cd),
    **dict.fromkeys(("export", "declare", "typeset", "local", "readonly"), _h_export),
    "unset": _h_unset,
}


# --- tool calls ----------------------------------------------------------------------------------------------------
_WRITE_TOOLS: Final = {
    "Write": "file_path",
    "Edit": "file_path",
    "MultiEdit": "file_path",
    "NotebookEdit": "notebook_path",
}
_READ_PATH_TOOLS: Final = {"Read": "file_path", "NotebookRead": "notebook_path", "LS": "path"}
_WEB_TOOLS: Final = frozenset({"WebFetch", "WebSearch"})
_META_TOOLS: Final = frozenset(
    {
        "TodoWrite", "AskUserQuestion", "Task", "Agent", "ExitPlanMode", "BashOutput", "ToolSearch",
        "WaitForMcpServers", "ListMcpResourcesTool", "ReadMcpResourceTool",
    }
)  # fmt: skip
_INVESTIGATION_META: Final = frozenset({"ToolSearch", "WaitForMcpServers"})
_GLOB_CHARS: Final = re.compile(r"[*?\[{]")


def _tool_word(text: str) -> _Word:
    """A path from a tool's JSON input as a word: no shell quoting, but a leading `~` still means home."""
    marked = _TILDE + text[1:] if text.startswith("~") else text
    return _Word(marked, text, glob=_GLOB_CHARS.search(text) is not None)


def _text_input(call: ToolCall, key: str) -> str | None:
    value = call.tool_input.get(key)
    return value if isinstance(value, str) else None


def _restricted_reason(policy: Policy) -> Reason:
    return Reason.READ_ONLY_POLICY if policy is Policy.READ_ONLY else Reason.PLAN_ONLY_POLICY


def _judge_tool(walk: _Walk, call: ToolCall) -> None:  # noqa: PLR0911, PLR0912  # one branch per tool family
    name, policy = call.tool_name, walk.ctx.policy
    if name == "Bash":
        command = _text_input(call, "command")
        if command is None:
            walk.ask(None, Reason.UNPARSEABLE, "Bash")
            return
        walk.run_text(command, 0)
        return
    if name.startswith("mcp__"):
        _judge_mcp(walk, name)
        return
    if name in _WRITE_TOOLS:
        path = _text_input(call, _WRITE_TOOLS[name])
        word = _tool_word(path) if path else None
        if word is not None:
            walk.check_word(word)
        if policy is Policy.READ_ONLY:
            walk.forbid(Reason.READ_ONLY_POLICY, name)
        elif policy is Policy.PLAN_ONLY and (word is None or not walk.plan_path(word)):
            walk.forbid(Reason.PLAN_ONLY_POLICY, path or name)
        return
    if name in _READ_PATH_TOOLS:
        path = _text_input(call, _READ_PATH_TOOLS[name])
        if path:
            walk.check_word(_tool_word(path))
        return
    if name == "Grep":  # recursive from `path`
        walk.check_word(_tool_word(_text_input(call, "path") or walk.ctx.cwd), recursive=True)
        return
    if name == "Glob":
        pattern = _text_input(call, "pattern") or "*"
        base = _text_input(call, "path") or walk.ctx.cwd
        walk.check_word(_tool_word(pattern if pattern.startswith(("/", "~")) else posixpath.join(base, pattern)))
        return
    if name in _WEB_TOOLS:
        return
    if name in _META_TOOLS:
        if policy is Policy.READ_ONLY and name not in _INVESTIGATION_META:
            walk.forbid(Reason.READ_ONLY_POLICY, name)
        return
    for key, value in call.tool_input.items():  # an unknown tool: guard its path-like inputs
        if isinstance(value, str) and (value.startswith(("/", "~")) or key.endswith("path")):
            walk.check_word(_tool_word(value))
    if policy in (Policy.READ_ONLY, Policy.PLAN_ONLY):
        walk.forbid(_restricted_reason(policy), name)


def _judge_mcp(walk: _Walk, name: str) -> None:
    """MCP tools: read-only ones run; a write tool runs only if pre-allowed, else it is sending to others."""
    server = name.split("__")[1] if name.count("__") >= 2 else ""
    read_only = name in walk.ctx.mcp_read_only_tools
    preallowed = name in walk.ctx.mcp_preallowed_tools or f"mcp__{server}__*" in walk.ctx.mcp_preallowed_tools
    if walk.ctx.policy in (Policy.READ_ONLY, Policy.PLAN_ONLY):
        if not read_only:
            walk.forbid(_restricted_reason(walk.ctx.policy), name)
        return
    if not (read_only or preallowed):
        walk.ask(GateClass.THIRD_PARTY_SEND, Reason.MCP_WRITE, name)


def _severity(finding: Finding) -> int:
    if finding.gate_class is None:
        return len(CLASS_SEVERITY)  # parse-safety asks rank after every real class
    return CLASS_SEVERITY.index(finding.gate_class)


def _verdict(findings: Sequence[Finding], rules: GateRules, policy: Policy) -> GateVerdict:
    items = tuple(findings)
    if policy is Policy.READ_ONLY:  # an investigation never asks: what is not read-only is denied
        items = tuple(
            item if item.action is Action.FORBID else Finding(Action.FORBID, Reason.READ_ONLY_POLICY, item.target)
            for item in items
        )
    forbidden = [item for item in items if item.action is Action.FORBID]
    if forbidden:
        first = forbidden[0]
        return GateVerdict(Action.FORBID, first.reason, first.target, first.gate_class, items)
    asks = [item for item in items if item.gate_class is None or item.gate_class in rules.ask_classes]
    if not asks:
        return GateVerdict(Action.ALLOW, Reason.FREE, findings=items)  # keeps asks the owner switched off, for audit
    top = min(asks, key=_severity)
    return GateVerdict(Action.ASK, top.reason, top.target, top.gate_class, tuple(asks))


def classify(call: ToolCall, rules: GateRules, ctx: GateContext) -> GateVerdict:
    """The gate's decision for one tool call (SPEC §3.2.3 step 2). A fact still missing from `ctx` fails closed."""
    walk = _Walk(rules, ctx)
    _judge_tool(walk, call)
    return _verdict(walk.findings, rules, ctx.policy)


def needs(call: ToolCall, rules: GateRules, ctx: GateContext) -> GateNeeds:
    """Facts `classify` would use that `ctx` lacks. A fact the service could not determine stays None and is listed
    again; the service fetches each fact once and stops when nothing new is listed (recipes nest ≤ 2 levels)."""
    walk = _Walk(rules, ctx)
    _judge_tool(walk, call)
    missing = walk.missing
    return GateNeeds(
        untracked="untracked" in missing,
        default_branch="default_branch" in missing,
        current_branch="current_branch" in missing,
        deploy_branches="deploy_branches" in missing,
        db_hosts="db_hosts" in missing,
        dirty_before_turn="dirty_before_turn" in missing,
        remotes=tuple(walk.remotes_needed),
        scripts=tuple(walk.scripts),
        deploy_refs=tuple(dict.fromkeys(walk.push_sources)),
    )


def remote_invocations(call: ToolCall, rules: GateRules, ctx: GateContext) -> tuple[tuple[str | None, ...], ...]:
    """The ssh/scp/sftp/rsync argv a call runs, scripts included (None = known only at run time). Stored with a
    consumed grant, so the Layer 2 shims let exactly those through."""
    walk = _Walk(rules, ctx)
    _judge_tool(walk, call)
    return tuple(walk.remote_calls)


# --- approvals (SPEC §3.2.3 steps 5–7) -----------------------------------------------------------------------------
APPROVAL_WORDS: Final = frozenset({"ha", "xa", "qil", "mayli", "bo'pti", "roziman", "ok", "yes", "da"})
DENIAL_WORDS: Final = frozenset({"yo'q", "yoq", "kerakmas", "bekor", "qilma", "no", "net"})
# Words that may follow the verdict without changing it ("ha, qil", "yo'q, kerak emas").
_APPROVAL_FILLER: Final = APPROVAL_WORDS | frozenset(
    {"okay", "bajar", "bajaring", "qiling", "albatta", "ruxsat", "beraman", "davom", "et", "ettir", "boshla", "go"}
)
_DENIAL_FILLER: Final = DENIAL_WORDS | frozenset({"qilmang", "kerak", "emas", "shart", "et", "hozir", "hozircha"})
_APOSTROPHES: Final = str.maketrans(dict.fromkeys("ʻʼ’‘`´", "'"))  # noqa: RUF001  # the variants owners type
_REPLY_TOKEN: Final = re.compile(r"#\d+|[\w']+")


class OwnerVerdict(StrEnum):
    APPROVE = "approve"
    DECLINE = "decline"
    INSTRUCT = "instruct"  # declined, and the owner's text is delivered as the instruction


@dataclass(frozen=True, slots=True)
class PendingItem:
    """An open item in the group: a decision or an agent question (both count for "the only pending item")."""

    kind: Literal["decision", "question"]
    id: int
    project_id: str | None


@dataclass(frozen=True, slots=True)
class Bound:
    """The reply belongs to `decision_id` (whatever its status: the service checks expiry with `resolve_approval`)."""

    decision_id: int
    verdict: OwnerVerdict
    instruction: str = ""


@dataclass(frozen=True, slots=True)
class Ambiguous:
    """`S.WHICH_DECISION(list)`: name the candidates, approve nothing."""

    decision_ids: tuple[int, ...]
    question_ids: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class NotAnApproval:
    """Not about a decision: the message goes on to question binding or routing."""


type ApprovalBinding = Bound | Ambiguous | NotAnApproval


def _reply_tokens(text: str, names: Iterable[str]) -> list[str]:
    folded = text.translate(_APOSTROPHES).casefold()
    spellings = {name.casefold() for name in names}
    tokens = [token for token in _REPLY_TOKEN.findall(folded) if not token.startswith("#")]
    return [token for token in tokens if token.split("'", 1)[0] not in spellings and token not in spellings]


def reply_verdict(text: str, *, names: Iterable[str] = ()) -> OwnerVerdict | None:
    """APPROVE / DECLINE for a bare approval or denial word (project names and `#id` aside); INSTRUCT when anything
    substantive follows ("ha, lekin staging'ga" is not a plain yes); None when it starts with neither."""
    tokens = _reply_tokens(text, names)
    if not tokens or tokens[0] not in APPROVAL_WORDS | DENIAL_WORDS:
        return None
    approve = tokens[0] in APPROVAL_WORDS
    filler = _APPROVAL_FILLER if approve else _DENIAL_FILLER
    if any(token not in filler for token in tokens[1:]):
        return OwnerVerdict.INSTRUCT
    return OwnerVerdict.APPROVE if approve else OwnerVerdict.DECLINE


def bind_approval(  # noqa: PLR0911  # one return per binding rule
    text: str,
    *,
    reply_to: int | None,
    pending: Sequence[PendingItem],
    mentioned_projects: Sequence[str] = (),
    names: Iterable[str] = (),
    numbers: Sequence[int] | None = None,
) -> ApprovalBinding:
    """Which decision an owner reply decides (SPEC §3.2.3 step 5, R-18). It binds only if it replies to the
    decision (`reply_to` = the decision behind the replied-to message), names its `#id` or project, or the decision
    is the only pending item, questions included. `names` are the spellings routing matched for the projects."""
    ids = sorted(set(numbers if numbers is not None else (int(number) for number in re.findall(r"#(\d+)", text))))
    verdict = reply_verdict(text, names=[*mentioned_projects, *names])
    if verdict is None:
        if reply_to is not None and not ids:
            return Bound(reply_to, OwnerVerdict.INSTRUCT, text.strip())
        return NotAnApproval()
    instruction = text.strip() if verdict is OwnerVerdict.INSTRUCT else ""
    if reply_to is not None:
        if any(number != reply_to for number in ids):
            return Ambiguous(tuple(sorted({reply_to, *ids})))
        return Bound(reply_to, verdict, instruction)
    if ids:
        return Bound(ids[0], verdict, instruction) if len(ids) == 1 else Ambiguous(tuple(ids))
    decisions = [item for item in pending if item.kind == "decision"]
    questions = [item for item in pending if item.kind == "question"]
    if mentioned_projects:
        named = [item.id for item in decisions if item.project_id in mentioned_projects]
        if len(named) == 1:
            return Bound(named[0], verdict, instruction)
        return Ambiguous(tuple(named)) if named else NotAnApproval()
    if len(decisions) == 1 and not questions:
        return Bound(decisions[0].id, verdict, instruction)
    if decisions:
        return Ambiguous(tuple(item.id for item in decisions), tuple(item.id for item in questions))
    return NotAnApproval()


# --- decisions and grants (SPEC §3.2.3 steps 3, 6, 8; §4.6) --------------------------------------------------------
type DecisionStatus = Literal["pending", "approved", "denied", "expired", "consumed", "superseded", "cancelled"]
_BINDING_IGNORED_KEYS: Final = frozenset({"description", "timeout", "run_in_background"})


def input_sha256(tool_name: str, tool_input: Mapping[str, JsonValue]) -> str:
    """The grant key: the exact tool and input (a different command or host is a different key). Cosmetic fields the
    agent rewrites between attempts (`description`, `timeout`) are left out; the Bash command is stripped."""
    material: dict[str, JsonValue] = {
        key: value for key, value in tool_input.items() if key not in _BINDING_IGNORED_KEYS
    }
    command = material.get("command")
    if tool_name == "Bash" and isinstance(command, str):
        material["command"] = command.strip()
    return hashlib.sha256(dumps({"tool": tool_name, "input": material}).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class DecisionState:
    """The `decisions` columns the rules read."""

    id: int
    status: DecisionStatus
    created_at: datetime
    expires_at: datetime
    grant_expires_at: datetime | None = None
    consumed_at: datetime | None = None


OPEN_STATUSES: Final[tuple[DecisionStatus, ...]] = ("pending", "approved")
TERMINAL_STATUSES: Final[tuple[DecisionStatus, ...]] = ("denied", "expired", "consumed", "superseded", "cancelled")
DECISION_KEEP: Final = timedelta(days=90)  # SPEC §4.2; open rows are never deleted
FACT_ROUNDS: Final = 3  # fetch facts, ask `needs` again, at most this often (recipes nest two levels)
# Until the outbox lands (T15), questions and replies go straight to the Messenger, bounded by this.
NOTICE_SEND_TIMEOUT_S: Final = 5.0


def retention_cutoff(now: datetime) -> datetime:
    """Finished decisions created strictly before this may be deleted."""
    return now - DECISION_KEEP


def fact_timeout_s(hook_timeout_s: float) -> float:
    """Each fact round's share of the hook budget (`gate.hook_timeout_s`), leaving one share to decide and write."""
    return hook_timeout_s / (FACT_ROUNDS + 1)


def decision_expires_at(now: datetime, ttl_min: int) -> datetime:
    """A new decision waits at most `gate.approval_ttl_min` (UC-13 F: "after 2 h … asked again")."""
    return now + timedelta(minutes=ttl_min)


def effective_status(decision: DecisionState, now: datetime) -> DecisionStatus:
    """`pending` past `expires_at`, or an unused grant past `grant_expires_at`, is `expired`."""
    if decision.status == "pending" and now >= decision.expires_at:
        return "expired"
    grant_over = decision.grant_expires_at is not None and now >= decision.grant_expires_at
    if decision.status == "approved" and decision.consumed_at is None and grant_over:
        return "expired"
    return decision.status


class ApprovalOutcome(StrEnum):
    APPROVED = "approved"  # set `grant_expires_at`; deliver "run exactly this, once"
    REASK = "reask"  # the decision expired: create a fresh one and ask again
    ALREADY_APPROVED = "already_approved"
    CLOSED = "closed"  # denied, consumed, superseded or cancelled: nothing to approve


@dataclass(frozen=True, slots=True)
class ApprovalResult:
    outcome: ApprovalOutcome
    grant_expires_at: datetime | None = None


def resolve_approval(decision: DecisionState, *, now: datetime, ttl_min: int) -> ApprovalResult:
    """An owner "ha" for `decision` (SPEC §3.2.3 steps 6 and 8)."""
    status = effective_status(decision, now)
    if status == "pending":
        return ApprovalResult(ApprovalOutcome.APPROVED, now + timedelta(minutes=ttl_min))
    if status == "expired":
        return ApprovalResult(ApprovalOutcome.REASK)
    if status == "approved":
        return ApprovalResult(ApprovalOutcome.ALREADY_APPROVED, decision.grant_expires_at)
    return ApprovalResult(ApprovalOutcome.CLOSED)


class GrantAction(StrEnum):
    USE_GRANT = "use_grant"  # consume it with `UPDATE … WHERE consumed_at IS NULL`, then allow
    STILL_PENDING = "still_pending"  # deny again; no new question
    ASK_NEW = "ask_new"  # insert a pending decision and ask


@dataclass(frozen=True, slots=True)
class GrantStep:
    action: GrantAction
    decision_id: int | None = None
    expired_ids: tuple[int, ...] = ()  # to mark `expired` in the same unit of work


def grant_step(same_key: Sequence[DecisionState], *, now: datetime) -> GrantStep:
    """An `ask` verdict, given the decisions with the call's exact `(project_id, tool_name, input_sha256)`."""
    expired = tuple(
        item.id
        for item in same_key
        if item.status in ("pending", "approved") and effective_status(item, now) == "expired"
    )
    grants = [item for item in same_key if effective_status(item, now) == "approved" and item.consumed_at is None]
    if grants:
        oldest = min(grants, key=lambda item: item.created_at)
        return GrantStep(GrantAction.USE_GRANT, oldest.id, expired)
    waiting = [item for item in same_key if effective_status(item, now) == "pending"]
    if waiting:
        newest = max(waiting, key=lambda item: item.created_at)
        return GrantStep(GrantAction.STILL_PENDING, newest.id, expired)
    return GrantStep(GrantAction.ASK_NEW, None, expired)


# --- Layer 2: the ssh/scp/sftp/rsync shims (SPEC §5.7) -------------------------------------------------------------
SHIM_TOOLS: Final = frozenset({"ssh", "scp", "sftp", "rsync"})


@dataclass(frozen=True, slots=True)
class ConsumedGrant:
    session_id: str
    consumed_at: datetime
    covers: tuple[tuple[str | None, ...], ...]  # `remote_invocations()` of the approved call


def _invocation_matches(pattern: Sequence[str | None], actual: Sequence[str]) -> bool:
    return len(pattern) == len(actual) and all(
        expected is None or expected == got for expected, got in zip(pattern, actual, strict=True)
    )


def shim_verdict(
    tool: str,
    argv: Sequence[str],
    *,
    session_id: str,
    rules: GateRules,
    ctx: GateContext,
    grants: Sequence[ConsumedGrant],
    now: datetime,
    window_s: float,
) -> GateVerdict:
    """A shim runs the real binary only on `allow`: a read-only (or git-host) command, or one a grant consumed in
    this session within `window_s` covers. The shim cannot ask, so anything else is a deny (exit 126)."""
    if tool not in SHIM_TOOLS:
        return GateVerdict(Action.FORBID, Reason.OPAQUE_COMMAND, tool)
    call = ToolCall("Bash", {"command": shlex.join([tool, *argv])}, "shim", None, session_id)
    verdict = classify(call, rules, ctx)
    if verdict.action is not Action.ASK:
        return verdict
    actual = (tool, *argv)
    window = timedelta(seconds=window_s)
    for grant in grants:
        recent = timedelta(0) <= now - grant.consumed_at <= window
        if grant.session_id == session_id and recent and any(_invocation_matches(c, actual) for c in grant.covers):
            return GateVerdict(Action.ALLOW, Reason.FREE, findings=verdict.findings)
    return verdict


# --- fact parsers (the service reads the files; SPEC §5.7 "Parsing") ------------------------------------------------
# A lookup answers `(known, text)`: `(True, None)` = nothing there to run (no such target, so the tool fails), and
# `(False, None)` = cannot tell (an include, a computed target name): the recipe stays unfetched and the call asks.
type RecipeLookup = tuple[bool, str | None]
MAKEFILE_NAMES: Final = ("GNUmakefile", "makefile", "Makefile")  # GNU make's search order
JUSTFILE_NAMES: Final = ("justfile", "Justfile", ".justfile", "JUSTFILE")
SCRIPT_READ_BYTES: Final = 256 * 1024  # a script, Makefile, justfile or package.json larger than this is not read
DB_CONFIG_FILES: Final = (".env", ".env.local", ".env.development", ".env.development.local", "alembic.ini")
_RECIPE_DEPTH: Final = 8
_RECIPE_TARGETS: Final = 64
_MAKE_RULE: Final = re.compile(r"(?P<targets>[^\t#=:][^#=:]*?)\s*::?(?!=)(?P<rest>.*)")
_MAKE_DEFAULT_GOAL: Final = re.compile(r"\.DEFAULT_GOAL\s*[:?]?=\s*(\S+)")
_MAKE_OPAQUE: Final = re.compile(r"^\s*(-?include|sinclude)\s|\$\(eval\b|\.RECIPEPREFIX")
_JUST_RECIPE: Final = re.compile(r"@?(?P<name>[A-Za-z_][A-Za-z0-9_-]*)(?P<params>[^:]*?):(?!=)(?P<deps>.*)")
_JUST_ALIAS: Final = re.compile(r"alias\s+(?P<alias>[A-Za-z_][A-Za-z0-9_-]*)\s*:=\s*(?P<name>[A-Za-z_][A-Za-z0-9_-]*)")
_JUST_OPAQUE: Final = re.compile(r"(import|mod)\b")
_JUST_NAME: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*")
_DB_URL_KEY: Final = re.compile(r"(?i)(?:^|_)(?:DATABASE|DB)_?(?:URL|URI|DSN)$")


def script_text(data: bytes | None) -> RecipeLookup:
    """A script file's text (`data` None = no such file): unknown if too large to read, nothing to run if binary."""
    if data is None:
        return True, None
    if len(data) > SCRIPT_READ_BYTES:
        return False, None
    if b"\x00" in data:
        return True, None
    try:
        return True, data.decode("utf-8")
    except UnicodeDecodeError:
        return True, None


def package_script(document: object, name: str) -> RecipeLookup:
    """`scripts[name]` of a parsed `package.json`; unknown when the document is not the expected shape."""
    if not isinstance(document, dict):
        return False, None
    scripts = document.get("scripts", {})
    if not isinstance(scripts, dict):
        return False, None
    script = scripts.get(name)
    if script is None:
        return True, None
    return (True, script) if isinstance(script, str) else (False, None)


def _logical_lines(text: str) -> list[str]:
    """Lines, with a backslash-newline continuation kept inside its line (recipes pass it to the shell as it is)."""
    lines: list[str] = []
    for line in text.replace("\r\n", "\n").split("\n"):
        if lines and lines[-1].endswith("\\"):
            lines[-1] = f"{lines[-1]}\n{line}"
        else:
            lines.append(line)
    return lines


@dataclass(slots=True)
class _Rules:
    """Targets (make) or recipes (just): their own lines, their prerequisites, the default, and whether the file can
    define more than its text shows (an include, a computed name)."""

    recipes: dict[str, list[str]] = field(default_factory=dict)
    prerequisites: dict[str, list[str]] = field(default_factory=dict)
    first: str | None = None
    opaque: bool = False


def _make_rule(rules: _Rules, match: re.Match[str]) -> list[str]:
    """Register one `targets: prerequisites [; recipe]` line; returns the targets later recipe lines belong to."""
    names = match["targets"].split()
    prerequisites, _, inline = match["rest"].partition(";")
    if any("$" in name for name in names) or "$" in prerequisites:
        rules.opaque = True
    current = [name for name in names if not name.startswith(".") and "%" not in name and "$" not in name]
    for name in current:
        rules.recipes.setdefault(name, [])
        rules.prerequisites.setdefault(name, []).extend(
            item for item in prerequisites.split() if "=" not in item and "%" not in item and "$" not in item
        )
        if inline.strip():
            rules.recipes[name].append(inline.strip())
        rules.first = rules.first or name
    return current


def _make_rules(text: str) -> _Rules:
    rules = _Rules()
    current: list[str] = []
    in_define = False
    for line in _logical_lines(text):
        if in_define:
            in_define = line.strip() != "endef"
            continue
        if line.startswith("\t"):
            for target in current:
                rules.recipes[target].append(line[1:])
            continue
        code = line.split("#", 1)[0].replace("\\\n", " ").strip()
        if not code:
            continue
        if code.split()[0] == "define":
            in_define, current = True, []
            continue
        rules.opaque = rules.opaque or _MAKE_OPAQUE.search(code) is not None
        goal = _MAKE_DEFAULT_GOAL.match(code)
        if goal is not None:
            rules.first = goal.group(1)
        match = _MAKE_RULE.fullmatch(code)
        current = [] if match is None else _make_rule(rules, match)
    return rules


def _ordered_recipe(rules: _Rules, target: str) -> RecipeLookup:
    """What runs for `target`: each prerequisite's lines first (depth first, each target once), then its own."""
    if target not in rules.recipes:
        return (False, None) if rules.opaque else (True, None)
    lines: list[str] = []
    seen: set[str] = set()

    def visit(name: str, depth: int) -> None:
        if name in seen or len(seen) >= _RECIPE_TARGETS or depth > _RECIPE_DEPTH:
            return
        seen.add(name)
        for item in rules.prerequisites.get(name, []):
            if item in rules.recipes:
                visit(item, depth + 1)
        lines.extend(rules.recipes.get(name, []))

    visit(target, 0)
    return True, "\n".join(lines)


def makefile_recipe(text: str, target: str) -> RecipeLookup:
    """What `make <target>` runs from this Makefile (`""` = the default goal), prerequisites included, as recipe
    lines (`ScriptRef("make", …)`). Variables stay unexpanded; `_make_recipe` turns them into unknown values."""
    rules = _make_rules(text)
    name = target or rules.first
    if name is None:
        return (False, None) if rules.opaque else (True, None)
    return _ordered_recipe(rules, name)


def _just_rules(text: str) -> tuple[_Rules, dict[str, str]]:
    rules = _Rules()
    aliases: dict[str, str] = {}
    current: str | None = None
    for line in _logical_lines(text):
        if line[:1] in (" ", "\t"):
            if current is not None:
                rules.recipes[current].append(line.strip())
            continue
        current = None
        code = line.split("#", 1)[0].rstrip()
        if not code or code.startswith("["):  # blank, a comment or an attribute line
            continue
        if _JUST_OPAQUE.match(code):
            rules.opaque = True
            continue
        alias = _JUST_ALIAS.fullmatch(code)
        if alias is not None:
            aliases[alias["alias"]] = alias["name"]
            continue
        match = _JUST_RECIPE.fullmatch(code)
        if match is None:
            continue
        current = match["name"]
        rules.recipes.setdefault(current, [])
        dependencies = re.sub(r"\(\s*(\S+)[^)]*\)", r"\1", match["deps"].replace("&&", " "))
        rules.prerequisites[current] = [item for item in dependencies.split() if _JUST_NAME.fullmatch(item)]
        rules.first = rules.first or current
    return rules, aliases


def justfile_recipe(text: str, name: str) -> RecipeLookup:
    """What `just <name>` runs (`""` = the first recipe), dependencies first (`ScriptRef("just", …)`)."""
    rules, aliases = _just_rules(text)
    recipe = aliases.get(name, name) or rules.first
    if recipe is None:
        return (False, None) if rules.opaque else (True, None)
    return _ordered_recipe(rules, recipe)


def db_hosts_from(files: Mapping[str, str]) -> tuple[str, ...]:
    """The DB hosts a project's own config points its tools at (`DB_CONFIG_FILES` name → text): `*DATABASE_URL`,
    `DB_URL`, `*_DSN`… in the `.env` files, `sqlalchemy.url` in `alembic.ini`. `""` = a SQLite file or a URL with no
    host (the local default); `"?"` = a value set elsewhere (not provably local); `()` = nothing configured."""
    hosts: list[str] = []
    for name, text in files.items():
        for raw in text.splitlines():
            line = raw.strip().removeprefix("export ").strip()
            key, sep, value = line.partition("=")
            key, value = key.strip(), value.strip().strip("\"'")
            wanted = key == "sqlalchemy.url" if name == "alembic.ini" else _DB_URL_KEY.search(key) is not None
            if not sep or not wanted or line.startswith(("#", ";")):
                continue
            if "://" in value:
                hosts.append(_parse_db_url(value)[0])
            elif value.startswith("$") or "%(" in value:
                hosts.append("?")
    return tuple(dict.fromkeys(hosts))
