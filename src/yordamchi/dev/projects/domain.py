"""The project registry's rules, pure (SPEC §3.1 `dev/projects`, §3.2.6, §4.2 `projects`, §5.7; UC-10, UC-13, UC-18).

A project is a top-level folder of `projects.root`; its id is the folder name, spelled exactly, so look-alikes stay
apart (`HRMentor` ≠ `HRMentor-wt-agent-api`; six `Wunderkind*` folders). The service scans the disk and asks git;
this module decides:

1. which entries are projects (`skip_reason`): folders only — not files such as `BONVI_SERVER.md`, not hidden
   entries, not symlinks;
2. the flags: `is_worktree` (a `.git` file, or `-wt-` in the name) and `is_archive_name` (`… Archive`, `backup`,
   `… old`, `… copy`). Both make a project answer only to its exact name, until the owner unarchives it;
3. the description: the owner's text, else the first prose paragraph of README, CLAUDE.md or the SPEC;
4. the deploy branches (`deploy_branches`, `platform_deploy`): which pushes start a CI workflow that deploys, plus the
   default branch of a project wired to a platform's Git integration (Vercel, Netlify, Render, Railway, App Engine),
   which deploys with no workflow file at all. An unreadable workflow makes every branch and tag count (fail closed:
   an extra question is acceptable, a miss is not);
5. the registry view (`project_view`): the stored row plus the owner's settings — aliases, archived and unarchived
   names, descriptions, the executor — read at use time, so a chat change applies at once (UC-18 AC);
6. name lookup, the owner's edits as new setting values, and the `loyihalar` text.
"""

import hashlib
import re
import unicodedata
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from functools import cache
from pathlib import PurePosixPath
from typing import Final

from yordamchi.core import strings as S
from yordamchi.core.errors import (
    ProjectAliasInvalid,
    ProjectAliasTaken,
    ProjectAmbiguous,
    ProjectDescriptionInvalid,
    ProjectNotFound,
)
from yordamchi.core.json_value import JsonObject, JsonValue
from yordamchi.core.text import BotText, join

# UC-18 AC: a new folder is listed within 5 min. The scheduler (T34) runs `sync_projects` at this pace.
SCAN_INTERVAL_S: Final = 120.0
# Bound on each git call of a scan or a gate fact. SPEC §4.3 has no key for it yet (`timeouts.git_s` is proposed).
GIT_TIMEOUT_S: Final = 10.0
GIT_CONCURRENCY: Final = 8  # projects asked at once during a scan (NFR-CAP-01: ≥ 40 projects)
ID_MAX: Final = 64  # SPEC §4.2: `id` S64
DOC_READ_BYTES: Final = 64 * 1024  # of each README / CLAUDE.md / SPEC
DESCRIPTION_MAX: Final = 200  # a description built from a file
OWNER_DESCRIPTION_MAX: Final = 300
LIST_DESCRIPTION_MAX: Final = 80  # per line of `loyihalar`
ALIAS_MIN: Final = 2
ALIAS_MAX: Final = 40
EXECUTORS: Final = ("claude", "codex")


# --- folders -------------------------------------------------------------------------------------------------------
class SkipReason(StrEnum):
    HIDDEN = "hidden"
    NOT_A_FOLDER = "not_a_folder"  # BONVI_SERVER.md, WunderkindLC.zip
    SYMLINK = "symlink"  # a project must really live under the root: the gate reasons about its path
    NAME_TOO_LONG = "name_too_long"


class GitKind(StrEnum):
    NONE = "none"
    REPO = "repo"  # `.git` is a directory
    WORKTREE = "worktree"  # `.git` is a file: a linked worktree of another repo


_ARCHIVE_NAME: Final = re.compile(r"archiv|arxiv|backup|deprecated|(?:^|[\s_.-])old$|(?:^|\s)copy(?:\s\d+)?$", re.I)
_WORKTREE_NAME: Final = "-wt-"


def projects_root(raw: str, *, home: PurePosixPath) -> PurePosixPath:
    """`projects.root` as a path: `~` is the owner's home (from `Paths`, so tests move it); relative means in it."""
    text = raw.strip()
    if text == "~" or text.startswith("~/"):
        return home / text[2:]
    path = PurePosixPath(text)
    return path if path.is_absolute() else home / path


def project_id(folder_name: str) -> str:
    """The id is the folder name, exactly (NFC, so a decomposed accent from the file system compares equal)."""
    return unicodedata.normalize("NFC", folder_name)


def skip_reason(name: str, *, is_dir: bool, is_symlink: bool) -> SkipReason | None:
    """Why a top-level entry is not a project, or None when it is one."""
    if name.startswith("."):
        return SkipReason.HIDDEN
    if is_symlink:
        return SkipReason.SYMLINK
    if not is_dir:
        return SkipReason.NOT_A_FOLDER
    if len(project_id(name)) > ID_MAX:
        return SkipReason.NAME_TOO_LONG
    return None


def is_archive_name(name: str) -> bool:
    """`BonviCall Clone Archive`, `X backup`, `X old`, `X copy 2`."""
    return _ARCHIVE_NAME.search(name) is not None


def is_worktree(name: str, git: GitKind) -> bool:
    return git is GitKind.WORKTREE or _WORKTREE_NAME in name


# --- descriptions (UC-18: "built from README, CLAUDE.md or SPEC") --------------------------------------------------
class DescriptionSource(StrEnum):
    README = "readme"
    CLAUDE_MD = "claude_md"
    SPEC = "spec"
    OWNER = "owner"
    NONE = "none"


# Where each source is looked for, in priority order; names compare case-insensitively.
DOC_FILES: Final[tuple[tuple[DescriptionSource, tuple[str, ...]], ...]] = (
    (DescriptionSource.README, ("README.md", "README.markdown", "README.rst", "README.txt", "README")),
    (DescriptionSource.CLAUDE_MD, ("CLAUDE.md",)),
    (DescriptionSource.SPEC, ("docs/SPEC.md", "SPEC.md")),
)


@dataclass(frozen=True, slots=True)
class DocText:
    source: DescriptionSource
    text: str = field(repr=False)


_FRONT_MATTER: Final = re.compile(r"\A---\n.*?\n---\n", re.S)
_HTML_COMMENT: Final = re.compile(r"<!--.*?-->", re.S)
_FENCE: Final = re.compile(r"^\s*(?:```|~~~)")
_IMAGE: Final = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_LINK: Final = re.compile(r"\[([^\]]*)\]\([^)]*\)|\[([^\]]+)\]\[[^\]]*\]")
_TAG: Final = re.compile(r"</?[A-Za-z][^>]*>")
_EMPHASIS: Final = re.compile(r"\*\*|__|`|\*")
_LIST_ITEM: Final = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s")
_RULE: Final = re.compile(r"^\s*(?:[-=*_]\s*){3,}$")
_SENTENCE_END: Final = re.compile(r"[.!?](?=\s)")


def _clean_inline(text: str) -> str:
    text = _IMAGE.sub("", text)
    text = _LINK.sub(lambda match: match[1] or match[2] or "", text)
    text = _TAG.sub("", text)
    text = _EMPHASIS.sub("", text)
    return " ".join(text.split())


def shorten(text: str, limit: int) -> str:
    """`text` within `limit` characters: cut after a sentence when one ends in the second half, else at a word."""
    if len(text) <= limit:
        return text
    head = text[: limit + 1]
    ends = [match.end() for match in _SENTENCE_END.finditer(head)]
    if ends and ends[-1] >= limit // 2:
        return head[: ends[-1]]
    cut = head[:limit].rsplit(" ", 1)[0] if " " in head[:limit] else head[: limit - 1]
    return cut.rstrip(" ,;:—-") + "…"


def _paragraphs(markdown: str) -> list[list[str]]:
    text = markdown.replace("\r\n", "\n").replace("\r", "\n").lstrip("﻿")
    text = _HTML_COMMENT.sub("", _FRONT_MATTER.sub("", text, count=1))
    paragraphs: list[list[str]] = []
    current: list[str] = []
    fenced = False
    for line in text.split("\n"):
        if _FENCE.match(line):
            fenced = not fenced
            line = ""  # noqa: PLW2901  # a fence ends the paragraph before it
        if fenced:
            continue
        if line.strip():
            current.append(line.strip())
        elif current:
            paragraphs.append(current)
            current = []
    if current:
        paragraphs.append(current)
    return paragraphs


def _prose(lines: list[str]) -> str | None:
    """The paragraph as one line of plain text, or None when it is a heading, table, list, badge row or HTML."""
    first = lines[0]
    if first.startswith(("#", "|", "<", "[![", "![", "> [!")) or _LIST_ITEM.match(first) or _RULE.match(first):
        return None
    if len(lines) > 1 and _RULE.match(lines[-1]):  # a setext heading
        return None
    text = _clean_inline(" ".join(line.removeprefix(">").strip() for line in lines))
    return text if len(text.split()) >= 3 else None


def _heading(lines: list[str]) -> str | None:
    first = lines[0]
    if not first.startswith("#"):
        return None
    text = _clean_inline(first.lstrip("#"))
    return text or None


def summarize(markdown: str, *, name: str, limit: int = DESCRIPTION_MAX) -> str | None:
    """The first prose paragraph of a README-like text, as one line within `limit`; else its first heading when that
    says more than the project name; else None."""
    paragraphs = _paragraphs(markdown)
    for lines in paragraphs:
        if (text := _prose(lines)) is not None:
            return shorten(text, limit)
    for lines in paragraphs:
        heading = _heading(lines)
        if heading is not None and len(heading.split()) >= 2 and heading.casefold() != name.casefold():
            return shorten(heading, limit)
    return None


def describe(docs: Sequence[DocText], *, name: str, owner: str | None) -> tuple[str, DescriptionSource]:
    """The owner's description first, then README → CLAUDE.md → SPEC (`docs` in that order); "" when none says
    anything."""
    if owner:
        return owner, DescriptionSource.OWNER
    for doc in docs:
        if (text := summarize(doc.text, name=name)) is not None:
            return text, doc.source
    return "", DescriptionSource.NONE


# --- deploy branches (UC-13 #1; SPEC §5.7 `deploy_push`) -----------------------------------------------------------
WORKFLOWS_DIR: Final = ".github/workflows"
WORKFLOW_SUFFIXES: Final = (".yml", ".yaml")
WORKFLOW_MAX_BYTES: Final = 512 * 1024
# Part of the cache key: when these rules change in a release, every project is checked again. 2: platform configs.
DEPLOY_RULES_VERSION: Final = 2
# A platform's Git integration deploys the production branch on every push, with no workflow file (T17b). Any of these
# in the working tree makes the default branch count as deploying. `.vercel/project.json` is what `vercel link` writes;
# it is usually git-ignored, so only the working tree has it.
PLATFORM_FILES: Final = (
    "vercel.json",
    ".vercel/project.json",
    "netlify.toml",
    "render.yaml",
    "railway.json",
    "railway.toml",
    "app.yaml",  # Google App Engine
)


class Unparseable:
    """A workflow that could not be read or is not YAML: it may deploy on any push."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "UNPARSEABLE"


UNPARSEABLE: Final = Unparseable()


@dataclass(frozen=True, slots=True)
class WorkflowFile:
    """One `.github/workflows/*.yml`: its file name and its `yaml.safe_load` result (YAML 1.1, so the `on:` key
    arrives as `True`), or `UNPARSEABLE`."""

    name: str
    document: object = field(repr=False)


@dataclass(frozen=True, slots=True)
class WorkflowStat:
    """What the cache key looks at in the working tree: a change to any of these re-runs the check."""

    name: str
    size: int
    mtime_ns: int


@cache
def _glob(pattern: str) -> re.Pattern[str] | None:
    """A GitHub branch/tag filter pattern as a regex: `*` stays within one `/` segment, `**` crosses them, `?` and `+`
    repeat the character before, `[…]` is a class, `\\` escapes. None when it cannot be read (it then matches every
    name: fail closed)."""
    out: list[str] = []
    quantifiable = False  # a literal or a class just ended: `?`/`+` apply to it
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if char == "*":
            double = pattern.startswith("**", index)
            out.append(".*" if double else "[^/]*")
            index += 2 if double else 1
            quantifiable = False
            continue
        if char in "?+":
            if not out:
                return None
            if quantifiable:
                out.append(char)
            index += 1
            quantifiable = False  # `a?+` would be possessive in Python: drop the second
            continue
        if char == "[":
            end = pattern.find("]", index + 1)
            if end == -1:
                return None
            out.append(pattern[index : end + 1])
            index = end + 1
        elif char == "\\" and index + 1 < len(pattern):
            out.append(re.escape(pattern[index + 1]))
            index += 2
        else:
            out.append(re.escape(char))
            index += 1
        quantifiable = True
    try:
        # Case-insensitive: matching more names than GitHub would is the safe direction.
        return re.compile("".join(out), re.IGNORECASE)
    except re.error:
        return None


def glob_matches(pattern: str, name: str) -> bool:
    regex = _glob(pattern)
    return regex is None or regex.fullmatch(name) is not None


def _escape_glob(name: str) -> str:
    return re.sub(r"([*?+\[\]\\!])", r"\\\1", name)


@dataclass(frozen=True, slots=True)
class DeployBranches:
    """The pushes that deploy, through a CI workflow or a platform's Git integration. `branches` and `tags` are GitHub
    filter globs (`main`, `release/**`); `every_*` means any name. `workflows` lists the files that deploy (workflow
    files, and platform configs such as `vercel.json`), `unparseable` the files that forced `every_*`. Stored as
    `projects.deploy_branches_json`."""

    every_branch: bool = False
    branches: tuple[str, ...] = ()
    every_tag: bool = False
    tags: tuple[str, ...] = ()
    workflows: tuple[str, ...] = ()
    unparseable: tuple[str, ...] = ()

    def covers_branch(self, branch: str) -> bool:
        """A push to `branch` deploys (case-insensitive; negated patterns are ignored: both only over-ask)."""
        return self.every_branch or any(glob_matches(pattern, branch) for pattern in self.branches)

    def covers_tag(self, tag: str) -> bool:
        return self.every_tag or any(glob_matches(pattern, tag) for pattern in self.tags)

    @property
    def deploys(self) -> bool:
        return self.every_branch or self.every_tag or bool(self.branches or self.tags)

    def union(self, other: "DeployBranches") -> "DeployBranches":
        every_branch = self.every_branch or other.every_branch
        every_tag = self.every_tag or other.every_tag
        return DeployBranches(
            every_branch=every_branch,
            branches=() if every_branch else tuple(sorted({*self.branches, *other.branches})),
            every_tag=every_tag,
            tags=() if every_tag else tuple(sorted({*self.tags, *other.tags})),
            workflows=tuple(sorted({*self.workflows, *other.workflows})),
            unparseable=tuple(sorted({*self.unparseable, *other.unparseable})),
        )

    def to_json(self) -> JsonObject:
        return {
            "every_branch": self.every_branch,
            "branches": list[JsonValue](self.branches),
            "every_tag": self.every_tag,
            "tags": list[JsonValue](self.tags),
            "workflows": list[JsonValue](self.workflows),
            "unparseable": list[JsonValue](self.unparseable),
        }

    @classmethod
    def from_json(cls, raw: JsonValue) -> "DeployBranches":
        """The inverse of `to_json`. A malformed value reads as `FAIL_CLOSED`: every push deploys."""
        if not isinstance(raw, dict):
            return FAIL_CLOSED
        every_branch, every_tag = raw.get("every_branch"), raw.get("every_tag")
        lists: list[tuple[str, ...]] = []
        for key in ("branches", "tags", "workflows", "unparseable"):
            items = raw.get(key)
            if not isinstance(items, list) or not all(isinstance(item, str) for item in items):
                return FAIL_CLOSED
            lists.append(tuple(item for item in items if isinstance(item, str)))
        if not isinstance(every_branch, bool) or not isinstance(every_tag, bool):
            return FAIL_CLOSED
        branches, tags, workflows, unparseable = lists
        return cls(
            every_branch=every_branch,
            branches=branches,
            every_tag=every_tag,
            tags=tags,
            workflows=workflows,
            unparseable=unparseable,
        )


NO_DEPLOY: Final = DeployBranches()
FAIL_CLOSED: Final = DeployBranches(every_branch=True, every_tag=True)


@dataclass(frozen=True, slots=True)
class _Refs:
    """Ref names a trigger fires for: every one, or those matching `globs` (empty: none)."""

    every: bool = False
    globs: frozenset[str] = frozenset()

    def __or__(self, other: "_Refs") -> "_Refs":
        return _Refs(every=self.every or other.every, globs=self.globs | other.globs)

    def keep(self, names: frozenset[str]) -> "_Refs":
        """Only the literal `names` these refs include, as escaped globs."""
        kept = {name for name in names if self.every or any(glob_matches(glob, name) for glob in self.globs)}
        return _Refs(globs=frozenset(_escape_glob(name) for name in kept))


_NO_REFS: Final = _Refs()
_EVERY_REF: Final = _Refs(every=True)


@dataclass(frozen=True, slots=True)
class _Trigger:
    branches: _Refs = _NO_REFS
    tags: _Refs = _NO_REFS

    def __or__(self, other: "_Trigger") -> "_Trigger":
        return _Trigger(branches=self.branches | other.branches, tags=self.tags | other.tags)

    def restrict(self, only: "_RefFilter") -> "_Trigger":
        branches = self.branches if only.branches is None else self.branches.keep(only.branches)
        tags = self.tags if only.tags is None else self.tags.keep(only.tags)
        return _Trigger(branches=branches, tags=tags)


_NOTHING: Final = _Trigger()
_ANY_PUSH: Final = _Trigger(branches=_EVERY_REF, tags=_EVERY_REF)


@dataclass(frozen=True, slots=True)
class _RefFilter:
    """What a job's or step's `if:` lets through: `None` is any name, a set is just those names."""

    branches: frozenset[str] | None = None
    tags: frozenset[str] | None = None

    def __and__(self, other: "_RefFilter") -> "_RefFilter":
        return _RefFilter(branches=_meet(self.branches, other.branches), tags=_meet(self.tags, other.tags))

    def __or__(self, other: "_RefFilter") -> "_RefFilter":
        return _RefFilter(branches=_join(self.branches, other.branches), tags=_join(self.tags, other.tags))


def _meet(left: frozenset[str] | None, right: frozenset[str] | None) -> frozenset[str] | None:
    if left is None:
        return right
    return left if right is None else left & right


def _join(left: frozenset[str] | None, right: frozenset[str] | None) -> frozenset[str] | None:
    return None if left is None or right is None else left | right


_ANY_REF: Final = _RefFilter()
_NEVER: Final = _RefFilter(branches=frozenset(), tags=frozenset())

# --- triggers ---
# The events a push or a merge can start; a pull-request event counts only when it fires on `closed` (a merge).
_PUSH_STARTED: Final = frozenset({"push", "workflow_run", "pull_request", "pull_request_target"})


def _patterns(value: object) -> _Refs | None:
    """A `branches:`/`tags:` list. Negated (`!x`) patterns are dropped, so the result is a superset."""
    items = [value] if isinstance(value, str) else value
    if not isinstance(items, list) or not all(isinstance(item, str) for item in items):
        return None
    positive = frozenset(item for item in items if isinstance(item, str) and not item.startswith("!"))
    return _Refs(globs=positive) if positive else _EVERY_REF


def _side(config: Mapping[object, object], key: str) -> _Refs | None:
    if f"{key}-ignore" in config:  # everything except some names: count everything
        return _EVERY_REF if _patterns(config[f"{key}-ignore"]) is not None else None
    return _patterns(config.get(key))


def _push_trigger(config: Mapping[object, object]) -> _Trigger | None:
    has_branches = "branches" in config or "branches-ignore" in config
    has_tags = "tags" in config or "tags-ignore" in config
    if not has_branches and not has_tags:  # `paths` filters are ignored: they only narrow
        return _ANY_PUSH
    branches = _side(config, "branches") if has_branches else _NO_REFS
    tags = _side(config, "tags") if has_tags else _NO_REFS
    if branches is None or tags is None:
        return None
    return _Trigger(branches=branches, tags=tags)


def _base_branch_trigger(config: Mapping[object, object]) -> _Trigger | None:
    if "branches" not in config and "branches-ignore" not in config:
        return _ANY_PUSH
    branches = _side(config, "branches")
    return None if branches is None else _Trigger(branches=branches)


def _merge_trigger(config: Mapping[object, object]) -> _Trigger | None:
    """A pull-request trigger deploys on a merge (`gh pr merge`) only when it fires on `closed`."""
    types = config.get("types")
    kinds = [types] if isinstance(types, str) else types
    if kinds is None:
        return _NOTHING  # the default types (opened, synchronize, reopened) do not include `closed`
    if not isinstance(kinds, list):
        return None
    return _base_branch_trigger(config) if "closed" in kinds else _NOTHING


def _triggers(on: object) -> _Trigger | None:
    """The pushes an `on:` value reacts to; None when it cannot be read."""
    if isinstance(on, str):
        events: dict[object, object] = {on: None}
    elif isinstance(on, list) and all(isinstance(event, str) for event in on):
        events = dict.fromkeys(on)
    elif isinstance(on, dict):
        events = dict(on)
    else:
        return None
    trigger = _NOTHING
    for event, raw in events.items():
        if event not in _PUSH_STARTED:  # schedule (a list), workflow_dispatch, release, workflow_call, …
            continue
        if raw is not None and not isinstance(raw, dict):
            return None
        config: Mapping[object, object] = raw or {}
        if event == "push":
            part = _push_trigger(config)
        elif event == "workflow_run":  # runs after another workflow, on the branch that one ran for
            part = _base_branch_trigger(config)
        else:
            part = _merge_trigger(config)
        if part is None:
            return None
        trigger |= part
    return trigger


# --- `if:` conditions ---
_REF_PROPERTY: Final = r"github\.(?:ref_name|ref|base_ref|head_ref|event\.workflow_run\.head_branch|event\.ref)"
_REF_TERM: Final = re.compile(
    rf"(?P<p1>{_REF_PROPERTY})\s*==\s*'(?P<v1>[^']*)'|'(?P<v2>[^']*)'\s*==\s*(?P<p2>{_REF_PROPERTY})", re.I
)
_NEGATION: Final = re.compile(r"!\s*(?:\(|github\b)", re.I)  # `!(…)` or `!github.…`; `!=` and `!cancelled()` are fine
_FULL_REF_PROPERTIES: Final = frozenset({"github.ref", "github.event.ref"})


def _term_filter(prop: str, value: str) -> _RefFilter:
    if prop.lower() in _FULL_REF_PROPERTIES:
        if value.startswith("refs/heads/"):
            return _RefFilter(branches=frozenset({value.removeprefix("refs/heads/")}), tags=frozenset())
        if value.startswith("refs/tags/"):
            return _RefFilter(branches=frozenset(), tags=frozenset({value.removeprefix("refs/tags/")}))
        if value.startswith("refs/"):
            return _NEVER  # refs/pull/…: never a push to a branch or a tag
    return _RefFilter(branches=frozenset({value}), tags=frozenset({value}))


def condition_filter(condition: object) -> _RefFilter:
    """What an `if:` lets through. Only `github.ref == '…'`-style equalities restrict, inside alternatives joined by
    `||`; a negated group, or an alternative without such an equality, lets every name through (fail closed)."""
    if condition is False:
        return _NEVER
    if not isinstance(condition, str):
        return _ANY_REF
    text = condition.strip()
    if text.startswith("${{") and text.endswith("}}"):
        text = text[3:-2].strip()
    if text.lower() == "false":
        return _NEVER
    result: _RefFilter | None = None
    for alternative in text.split("||"):
        terms = list(_REF_TERM.finditer(alternative))
        if not terms or _NEGATION.search(alternative):
            return _ANY_REF
        filters = [_term_filter(term["p1"] or term["p2"], term["v1"] or term["v2"] or "") for term in terms]
        found = filters[0]
        for extra in filters[1:]:
            found |= extra  # a superset of what the conjunction allows
        result = found if result is None else result | found
    return result if result is not None else _ANY_REF


# --- deploy signals ---
_DEPLOY_WORDS: Final = re.compile(r"deploy|release|publish|rollout|production|\bprod\b|\bship\b|\bcd\b", re.I)
_DEPLOY_ACTION: Final = re.compile(
    r"deploy|release|publish|pages|ssh|scp|rsync|ftp|vercel|netlify|heroku|superfly|flyctl|railway|firebase|wrangler"
    r"|cloudflare|aws-actions/|google-github-actions/|azure/|digitalocean/|kubectl|helm|docker/login-action",
    re.I,
)
_DOCKER_BUILD_PUSH: Final = "docker/build-push-action"
_CHECKOUT: Final = "actions/checkout"  # a deploy key there only reads the repo
# SPEC §5.7 deploy and server_write triggers, as they appear in a CI `run:` script.
_DEPLOY_COMMAND: Final = re.compile(
    r"""deploy
    | \b(?:ssh|scp|sftp|rsync)\b
    | \bvercel\b | \bnetlify\b | \bheroku\b | \bfastlane\b | \bansible-playbook\b | \bsurge\b | \bdokku\b
    | \bdoctl\b | \bcaprover\b
    | \b(?:fly|flyctl)\s+(?:deploy|scale) | \brailway\s+up
    | \bgcloud\b | \baws\s+(?:ecs|lambda|s3\s+(?:sync|cp)|cloudformation)
    | \bkubectl\s+(?:apply|delete|rollout|scale|set|patch|edit|create|replace)
    | \bhelm\s+(?:install|upgrade|rollback|uninstall)
    | \b(?:terraform|tofu)\s+(?:apply|destroy) | \bpulumi\s+(?:up|destroy)
    | \bdocker\s+(?:push|buildx\b.*--push|compose\b.*\bpush) | \bdocker\s+(?:-H|--host|--context)\b
    | \beas\s+(?:submit|update|build)
    | \bgh\s+(?:workflow\s+run|release\s+create)
    | \b(?:make|just|npm\s+run|pnpm(?:\s+run)?|yarn(?:\s+run)?|bun\s+run)\s+(?:release|publish|prod)
    | \b(?:npm|pnpm|yarn|cargo|poetry|uv|flit|hatch)\s+publish\b | \btwine\s+upload\b | \bgem\s+push\b
    | \bfirebase\s+deploy | \bwrangler\s+(?:publish|pages)
    | \bgit\s+push\b
    """,
    re.I | re.X,
)
_SECRET_REF: Final = re.compile(r"secrets\.([A-Za-z0-9_]+)")
_DEPLOY_SECRET: Final = re.compile(
    r"SSH|DEPLOY|SERVER|HOST|VPS|PROD|KUBE|HEROKU|VERCEL|NETLIFY|FLY|RAILWAY|RENDER|CLOUDFLARE|FIREBASE|AWS|GCP|GCLOUD"
    r"|AZURE|DOCKER|REGISTRY|NPM_TOKEN|PYPI|DIGITALOCEAN|FTP",
    re.I,
)


def _strings(value: object) -> list[str]:
    """The string values one level down (a mapping's values or a list's items), or the value itself."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [item for item in value.values() if isinstance(item, str)]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str)]
    return []


def _mentions_deploy_secret(*values: object) -> bool:
    return any(
        _DEPLOY_SECRET.search(match[1])
        for value in values
        for text in _strings(value)
        for match in _SECRET_REF.finditer(text)
    )


def _truthy(value: object) -> bool:
    if isinstance(value, str):
        return value.strip().lower() not in {"", "false", "0", "no"}
    return bool(value)


def _named_deploy(*names: object) -> bool:
    return any(isinstance(name, str) and _DEPLOY_WORDS.search(name) for name in names)


def _step_deploys(step: Mapping[object, object]) -> bool:
    uses = step.get("uses")
    action = uses if isinstance(uses, str) else ""
    with_ = step.get("with")
    if _named_deploy(step.get("name"), step.get("id")):
        return True
    if action.startswith(_DOCKER_BUILD_PUSH):
        return isinstance(with_, dict) and _truthy(with_.get("push"))
    if action and _DEPLOY_ACTION.search(action):
        return True
    run = step.get("run")
    if isinstance(run, str) and _DEPLOY_COMMAND.search(run):
        return True
    return not action.startswith(_CHECKOUT) and _mentions_deploy_secret(run, with_, step.get("env"))


def _local_workflow(uses: str) -> str | None:
    """`./.github/workflows/deploy.yml` → `deploy.yml`; None for a workflow in another repository."""
    prefix = f"./{WORKFLOWS_DIR}/"
    return uses.removeprefix(prefix).split("@", 1)[0] if uses.startswith(prefix) else None


def _job_deploys(job_id: str, job: Mapping[object, object], deploys: Callable[[str], bool]) -> bool:
    """Signals on the job itself: its name, a GitHub environment, a reusable deploy workflow, a deploy secret."""
    if _named_deploy(job_id, job.get("name")) or job.get("environment") is not None:
        return True
    uses = job.get("uses")
    if isinstance(uses, str):
        local = _local_workflow(uses)
        return True if local is None else deploys(local)  # another repo's workflow cannot be read: fail closed
    return _mentions_deploy_secret(job.get("env"))


def _job_filter(
    job_id: str, job: Mapping[object, object], *, named: bool, deploys: Callable[[str], bool]
) -> _RefFilter | None:
    """What this job lets deploy, or None when it does not deploy. A deploy step under a job-level `if:` meets
    both conditions."""
    job_if = condition_filter(job.get("if"))
    if job_if == _NEVER:
        return None
    if named or _job_deploys(job_id, job, deploys):
        return job_if
    steps = job.get("steps")
    if steps is None:
        return None
    if not isinstance(steps, list) or not all(isinstance(step, dict) for step in steps):
        return job_if
    found: _RefFilter | None = None
    for step in steps:
        if isinstance(step, dict) and _step_deploys(step):
            step_filter = job_if & condition_filter(step.get("if"))
            found = step_filter if found is None else found | step_filter
    return found


def _deploy_filters(doc: Mapping[object, object], *, named: bool, deploys: Callable[[str], bool]) -> list[_RefFilter]:
    """One filter per deploying job. Jobs that cannot be read count as one unrestricted deploy job.

    A workflow named like a deploy (`deploy.yml`, `name: Release`) makes every job count only when no job shows a
    deploy signal of its own: the common `deploy.yml` with a test job on every branch and a deploy job under
    `if: github.ref == 'refs/heads/main'` must not make every push ask."""
    jobs = doc.get("jobs")
    if jobs is None:
        return []
    if not isinstance(jobs, dict) or not all(isinstance(job, dict) for job in jobs.values()):
        return [_ANY_REF]

    def collect(*, every_job: bool) -> list[_RefFilter]:
        return [
            found
            for job_id, job in jobs.items()
            if isinstance(job, dict)
            and (found := _job_filter(str(job_id), job, named=every_job, deploys=deploys)) is not None
        ]

    own = collect(every_job=False)
    return own if own or not named else collect(every_job=True)


def _on_values(doc: Mapping[object, object]) -> list[object]:
    """`on:` parses as the key `True` in YAML 1.1; a quoted `"on":` stays a string. Either may be present."""
    return [doc[key] for key in ("on", True) if key in doc]


class _Analysis:
    """One set of workflow files (the working tree, or one commit): reusable workflows resolve within the set."""

    def __init__(self, files: Sequence[WorkflowFile]) -> None:
        self._files = {file.name: file for file in files}
        self._deploys: dict[str, bool] = {}
        self._visiting: set[str] = set()

    def _is_named(self, file: WorkflowFile, doc: Mapping[object, object]) -> bool:
        return _named_deploy(file.name.rsplit(".", 1)[0], doc.get("name"))

    def deploys(self, name: str) -> bool:
        """Whether any job of the workflow `name` deploys, whatever starts it (used for `uses: ./…` callers)."""
        if name in self._deploys:
            return self._deploys[name]
        file = self._files.get(name)
        if file is None or name in self._visiting:  # missing or circular: cannot tell, so yes
            return True
        self._visiting.add(name)
        doc = file.document
        if doc is None:
            answer = False
        elif not isinstance(doc, dict):
            answer = True
        else:
            answer = bool(_deploy_filters(doc, named=self._is_named(file, doc), deploys=self.deploys))
        self._visiting.discard(name)
        self._deploys[name] = answer
        return answer

    def trigger(self, file: WorkflowFile) -> _Trigger:
        """The pushes that make this file deploy."""
        doc = file.document
        if doc is None:  # an empty file: GitHub never runs it
            return _NOTHING
        if not isinstance(doc, dict):
            return _ANY_PUSH
        on = _on_values(doc)
        if not on:
            return _NOTHING
        filters = _deploy_filters(doc, named=self._is_named(file, doc), deploys=self.deploys)
        if not filters:
            return _NOTHING
        trigger = _NOTHING
        for value in on:
            part = _triggers(value)
            trigger |= _ANY_PUSH if part is None else part
        result = _NOTHING
        for only in filters:
            result |= trigger.restrict(only)
        return result


def deploy_branches(files: Sequence[WorkflowFile]) -> DeployBranches:
    """Which pushes deploy, from one set of workflow files (UC-13 #1). Pass the working tree's files and the pushed
    commit's files separately and `union` the results: a reusable workflow resolves within its own set."""
    analysis = _Analysis(files)
    total = _NOTHING
    deploying: list[str] = []
    for file in sorted(files, key=lambda item: item.name):
        trigger = analysis.trigger(file)
        if trigger.branches.every or trigger.branches.globs or trigger.tags.every or trigger.tags.globs:
            deploying.append(file.name)
        total |= trigger
    unparseable = tuple(
        sorted(file.name for file in files if file.document is not None and not isinstance(file.document, dict))
    )
    return DeployBranches(
        every_branch=total.branches.every,
        branches=() if total.branches.every else tuple(sorted(total.branches.globs)),
        every_tag=total.tags.every,
        tags=() if total.tags.every else tuple(sorted(total.tags.globs)),
        workflows=tuple(deploying),
        unparseable=unparseable,
    )


def platform_deploy(found: Collection[str], *, default_branch: str | None) -> DeployBranches:
    """The pushes a platform's Git integration deploys (T17b): the default branch, when any of `PLATFORM_FILES` is in
    `found` (what the working tree has). An unknown default branch makes every branch count (fail closed)."""
    configs = tuple(name for name in PLATFORM_FILES if name in found)
    if not configs:
        return NO_DEPLOY
    if default_branch is None or not default_branch.strip():
        return DeployBranches(every_branch=True, workflows=configs)
    return DeployBranches(branches=(_escape_glob(default_branch),), workflows=configs)


def is_workflow_name(name: str) -> bool:
    return name.endswith(WORKFLOW_SUFFIXES) and not name.startswith(".")


def deploy_key(
    head: str | None,
    stats: Sequence[WorkflowStat] | None,
    *,
    platform: Collection[str] = (),
    default_branch: str | None = None,
) -> str:
    """The cache key of the stored deploy branches (`deploy_checked_sha`): the rules version, the HEAD commit, the
    working tree's workflow files, its platform configs and the default branch they deploy. Any change re-runs the
    check; `stats=None` means the folder could not be listed."""
    digest = hashlib.sha256(f"rules={DEPLOY_RULES_VERSION}\nhead={head or ''}\n".encode())
    if stats is None:
        digest.update(b"workflows=unreadable\n")
    else:
        for stat in sorted(stats, key=lambda item: item.name):
            digest.update(f"{stat.name}\0{stat.size}\0{stat.mtime_ns}\n".encode(errors="surrogateescape"))
    digest.update(f"platform={','.join(sorted(platform))}\nbranch={default_branch or ''}\n".encode())
    return digest.hexdigest()


# --- the registry ---------------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class FolderFacts:
    """What one scan saw in a project folder, before git."""

    name: str
    path: PurePosixPath
    git: GitKind
    docs: tuple[DocText, ...] = ()
    workflow_stats: tuple[WorkflowStat, ...] | None = ()  # None: `.github/workflows` exists but cannot be listed
    platform_files: tuple[str, ...] = ()  # the `PLATFORM_FILES` present; one that cannot be checked counts as present


@dataclass(frozen=True, slots=True)
class GitFacts:
    """What git said about a project during a scan."""

    head: str | None
    head_at: datetime | None
    remote_url: str | None
    default_branch: str | None


@dataclass(frozen=True, slots=True)
class ProjectRecord:
    """One `projects` row as the domain sees it."""

    id: str
    path: str
    description: str
    description_source: DescriptionSource
    is_worktree: bool
    is_archive_name: bool
    executor: str
    git_remote_url: str | None
    default_branch: str | None
    deploy: DeployBranches
    deploy_checked_sha: str | None
    discovered_at: datetime
    last_seen_at: datetime
    missing_since: datetime | None = None
    last_activity_at: datetime | None = None


def name_key(name: str) -> str:
    """How names compare: NFC, case-folded, inner whitespace collapsed. `HRMentor` and `hrmentor` are one key;
    `HRMentor` and `HRMentor-wt-agent-api` stay two."""
    return " ".join(unicodedata.normalize("NFC", name).casefold().split())


@dataclass(frozen=True, slots=True)
class ProjectPrefs:
    """The owner's project settings (§4.3 ROUTING / PROJECTS), as read from this turn's snapshot."""

    aliases: Mapping[str, str] = field(default_factory=dict)  # alias → project id
    descriptions: Mapping[str, str] = field(default_factory=dict)  # project id → text
    archived: Collection[str] = ()
    unarchived: Collection[str] = ()
    executor_default: str = "claude"
    executor_per_project: Mapping[str, str] = field(default_factory=dict)

    def _lookup(self, values: Mapping[str, str], project: str) -> str | None:
        key = name_key(project)
        return next((value for name, value in values.items() if name_key(name) == key), None)

    def aliases_of(self, project: str) -> tuple[str, ...]:
        key = name_key(project)
        return tuple(sorted(alias for alias, target in self.aliases.items() if name_key(target) == key))

    def description_of(self, project: str) -> str | None:
        text = self._lookup(self.descriptions, project)
        return " ".join(text.split()) if text and text.strip() else None

    def executor_of(self, project: str) -> str:
        chosen = self._lookup(self.executor_per_project, project)
        if chosen in EXECUTORS:
            return chosen
        return self.executor_default if self.executor_default in EXECUTORS else EXECUTORS[0]

    def is_listed(self, names: Collection[str], project: str) -> bool:
        key = name_key(project)
        return any(name_key(name) == key for name in names)


@dataclass(frozen=True, slots=True)
class Project:
    """A registered project as routing, the gate, turns and the digest see it: the row plus the owner's settings."""

    id: str
    path: PurePosixPath
    description: str
    description_source: DescriptionSource
    aliases: tuple[str, ...]
    executor: str
    is_worktree: bool
    is_archive_name: bool
    archived: bool  # by the owner, or by an archive-like name the owner did not unarchive
    exact_name_only: bool  # archived, or a worktree not unarchived: routing matches only its exact name (§3.2.6)
    missing: bool  # its folder is gone; the row stays
    git_remote_url: str | None
    default_branch: str | None
    deploy: DeployBranches
    discovered_at: datetime
    last_seen_at: datetime
    last_activity_at: datetime | None

    @property
    def routable(self) -> bool:
        """Content and fuzzy routing may pick it; otherwise only its exact name does (UC-18 AC)."""
        return not self.missing and not self.exact_name_only


def project_view(record: ProjectRecord, prefs: ProjectPrefs) -> Project:
    unarchived = prefs.is_listed(prefs.unarchived, record.id)
    archived = prefs.is_listed(prefs.archived, record.id) or (record.is_archive_name and not unarchived)
    owner_text = prefs.description_of(record.id)
    return Project(
        id=record.id,
        path=PurePosixPath(record.path),
        description=owner_text or record.description,
        description_source=DescriptionSource.OWNER if owner_text else record.description_source,
        aliases=prefs.aliases_of(record.id),
        executor=prefs.executor_of(record.id),
        is_worktree=record.is_worktree,
        is_archive_name=record.is_archive_name,
        archived=archived,
        exact_name_only=archived or (record.is_worktree and not unarchived),
        missing=record.missing_since is not None,
        git_remote_url=record.git_remote_url,
        default_branch=record.default_branch,
        deploy=record.deploy,
        discovered_at=record.discovered_at,
        last_seen_at=record.last_seen_at,
        last_activity_at=record.last_activity_at,
    )


def record_for(
    folder: FolderFacts,
    *,
    git: GitFacts | None,
    deploy: DeployBranches,
    deploy_checked_sha: str | None,
    previous: ProjectRecord | None,
    prefs: ProjectPrefs,
    now: datetime,
) -> ProjectRecord:
    """The row after this scan. `git=None` (no repository, or git failed this time) keeps the last git facts."""
    project = project_id(folder.name)
    description, source = describe(folder.docs, name=project, owner=prefs.description_of(project))
    remote = git.remote_url if git is not None else previous.git_remote_url if previous else None
    branch = git.default_branch if git is not None else previous.default_branch if previous else None
    activity = [at for at in (previous.last_activity_at if previous else None, git.head_at if git else None) if at]
    return ProjectRecord(
        id=project,
        path=str(folder.path),
        description=description,
        description_source=source,
        is_worktree=is_worktree(folder.name, folder.git),
        is_archive_name=is_archive_name(folder.name),
        executor=prefs.executor_of(project),
        git_remote_url=remote,
        default_branch=branch,
        deploy=deploy,
        deploy_checked_sha=deploy_checked_sha,
        discovered_at=previous.discovered_at if previous else now,
        last_seen_at=now,
        missing_since=None,
        last_activity_at=max(activity) if activity else None,
    )


@dataclass(frozen=True, slots=True)
class SyncReport:
    """What one scan changed. Names only; the scheduler logs the counts."""

    total: int
    added: tuple[str, ...] = ()
    returned: tuple[str, ...] = ()  # were missing, back now
    vanished: tuple[str, ...] = ()  # newly missing
    skipped: int = 0  # top-level entries that are not projects
    git_failed: tuple[str, ...] = ()
    root_readable: bool = True  # False: `projects.root` could not be listed, and nothing was changed


def vanished(known: Mapping[str, ProjectRecord], seen: Collection[str]) -> tuple[str, ...]:
    """Registered projects whose folder this scan did not see and that are not already marked missing."""
    return tuple(sorted(pid for pid, record in known.items() if pid not in seen and record.missing_since is None))


# --- names and the owner's edits (UC-18) ----------------------------------------------------------------------------
def find_projects(name: str, projects: Iterable[Project]) -> tuple[Project, ...]:
    """Projects whose folder name, else whose alias, is exactly `name` (case-insensitive). Archived, worktree and
    missing projects match too: an exact name is how the owner reaches them."""
    key = name_key(name)
    pool = list(projects)
    by_id = tuple(project for project in pool if name_key(project.id) == key)
    if by_id:
        return by_id
    return tuple(project for project in pool if any(name_key(alias) == key for alias in project.aliases))


def resolve_project(name: str, projects: Iterable[Project]) -> Project:
    """The one project `name` names, else `ProjectNotFound` / `ProjectAmbiguous`."""
    found = find_projects(name, projects)
    if not found:
        raise ProjectNotFound(name=name.strip())
    if len(found) > 1:
        raise ProjectAmbiguous(name=name.strip(), candidates=", ".join(project.id for project in found))
    return found[0]


def with_alias(
    aliases: Mapping[str, str], projects: Iterable[Project], *, project: Project, alias: str
) -> dict[str, str] | None:
    """The new `projects.aliases` value with `alias` → `project`, or None when nothing changes. An alias may not be
    another project's folder name or alias."""
    clean = " ".join(unicodedata.normalize("NFC", alias).split())
    if "\n" in alias or not ALIAS_MIN <= len(clean) <= ALIAS_MAX:
        raise ProjectAliasInvalid(alias=clean[:ALIAS_MAX], limit=ALIAS_MAX)
    key = name_key(clean)
    if key == name_key(project.id):
        return None
    for other in projects:
        if other.id != project.id and name_key(other.id) == key:
            raise ProjectAliasTaken(alias=clean, project=other.id)
    for existing, target in aliases.items():
        if name_key(existing) == key:
            if name_key(target) == name_key(project.id):
                return None
            raise ProjectAliasTaken(alias=clean, project=target)
    return {**aliases, clean: project.id}


def _without(names: Collection[str], project: str) -> list[str]:
    key = name_key(project)
    return [name for name in names if name_key(name) != key]


@dataclass(frozen=True, slots=True)
class ArchiveChange:
    """New values of `projects.archived` / `projects.unarchived`; None leaves that setting as it is."""

    archived: list[str] | None = None
    unarchived: list[str] | None = None


def with_archived(prefs: ProjectPrefs, project: Project, *, archived: bool) -> ArchiveChange:
    """Archive: list it in `projects.archived` (that list wins). Unarchive: drop it from there, and list it in
    `projects.unarchived` when its name alone would archive it (`… Archive`, `-wt-`)."""
    in_archived = prefs.is_listed(prefs.archived, project.id)
    in_unarchived = prefs.is_listed(prefs.unarchived, project.id)
    if archived:
        return ArchiveChange(
            archived=None if in_archived else [*prefs.archived, project.id],
            unarchived=_without(prefs.unarchived, project.id) if in_unarchived else None,
        )
    name_archives = project.is_archive_name or project.is_worktree
    return ArchiveChange(
        archived=_without(prefs.archived, project.id) if in_archived else None,
        unarchived=[*prefs.unarchived, project.id] if name_archives and not in_unarchived else None,
    )


def with_description(descriptions: Mapping[str, str], project: Project, text: str) -> dict[str, str]:
    """The new `projects.descriptions` value with the owner's one-line `text` for `project`."""
    clean = " ".join(text.split())
    if "\n" in text.strip() or not 1 <= len(clean) <= OWNER_DESCRIPTION_MAX:
        raise ProjectDescriptionInvalid(limit=OWNER_DESCRIPTION_MAX)
    kept = {name: value for name, value in descriptions.items() if name_key(name) != name_key(project.id)}
    return {**kept, project.id: clean}


# --- "loyihalar" ----------------------------------------------------------------------------------------------------
def _sorted(projects: Iterable[Project]) -> list[Project]:
    return sorted(projects, key=lambda project: (name_key(project.id), project.id))


def _line(project: Project) -> BotText:
    parts = [S.PROJECTS_ITEM(project=project.id)]
    if project.description:
        parts.append(S.PROJECTS_ITEM_DESCRIPTION(description=shorten(project.description, LIST_DESCRIPTION_MAX)))
    if project.aliases:
        parts.append(S.PROJECTS_ITEM_ALIASES(aliases=", ".join(project.aliases)))
    if project.exact_name_only and not project.archived:
        parts.append(S.PROJECTS_ITEM_WORKTREE())
    return join(parts, separator="")


def listing(projects: Iterable[Project], *, root: str) -> BotText:
    """`loyihalar` (UC-18, P1 basic): active projects one per line, then the archived and the vanished by name."""
    pool = _sorted(projects)
    if not pool:
        return S.PROJECTS_NONE(root=root)
    active = [project for project in pool if not project.missing and not project.archived]
    archived = [project for project in pool if not project.missing and project.archived]
    missing = [project for project in pool if project.missing]
    lines = [S.PROJECTS_SUMMARY(active=len(active), archived=len(archived))]
    lines.extend(_line(project) for project in active)
    if archived:
        lines.append(S.PROJECTS_ARCHIVED_LIST(projects=", ".join(project.id for project in archived)))
    if missing:
        lines.append(S.PROJECTS_MISSING_LIST(projects=", ".join(project.id for project in missing)))
    return join(lines)
