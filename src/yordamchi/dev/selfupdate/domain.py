"""Self-update by chat, pure (SPEC §3.2.12 bot side, §4.2 `self_updates`; UC-19, Q5, R-30…R-33).

- **Names.** `update_id(row_id)` (`u000007`) names `updates/<id>/`; `branch_name(row_id, text)` the work branch
  (`selfupdate/7-ovoz-sozlamasi`), always under `selfupdate/`, never `main`.
- **What the change is** (`plan_change`), from the files the turn changed against the base commit:
  - `supervisor/**` and anything else together → refused (R-32: never both in one update);
  - only `supervisor/**` → `kind=supervisor`; anything else → `kind=app`;
  - a file matching a `.protected-core` glob **of the running release** (never the branch's own copy, which the
    change itself could edit), or a safeguard setting whose declaration changed in `defaults.py`
    (`weakened_settings`) → the owner is asked once (Q5). Everything else applies after the tests pass.
- **The supervisor's answer** (`read_outcome`): `updates/<id>/status.json` as the supervisor writes it, and the
  owner's message for a final one (`outcome_text`).
- **Probation and drain flags** are the bots' side of the same protocol; they live in `shared/health`, because both
  bots read them.
"""

import ast
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Final

from yordamchi.core import strings as S
from yordamchi.core.json_value import JsonObject, JsonValue
from yordamchi.core.text import BotText

SELF_PROJECT: Final = "Yordamchi"  # the working copy `~/Documents/Projects/Yordamchi` (SPEC §2.2)
MAIN: Final = "main"
BRANCH_PREFIX: Final = "selfupdate/"
SUPERVISOR_PREFIX: Final = "supervisor/"
PROTECTED_CORE: Final = ".protected-core"
SETTINGS_DIR: Final = "src/yordamchi/shared/settings"
DEFAULTS_NAME: Final = "defaults.py"
DEFAULTS_MAX_BYTES: Final = 2 * 1024 * 1024
SLUG_MAX: Final = 32
SUMMARY_MAX: Final = 600
CHANGED_FILES_KEPT: Final = 200
CHANGES_SHOWN: Final = 5
GIT_TIMEOUT_S: Final = 20.0
_WORD: Final = re.compile(r"[a-z0-9]+")
_APOSTROPHES: Final = ("'", "’", "ʻ", "ʼ", "`")


class UpdateStatus(StrEnum):
    """`self_updates.status` (SPEC §4.2). The first three are the bot's; the rest mirror `status.json`."""

    WORKING = "working"  # the coding turn is on the branch
    AWAITING_CORE = "awaiting_core"  # one question to the owner (protected core, Q5)
    REQUESTED = "requested"  # `request.json` written; the supervisor has not answered yet
    BUILDING = "building"
    REFUSED = "refused"
    DRAINING = "draining"
    SWITCHING = "switching"
    PROBATION = "probation"
    HEALTHY = "healthy"
    MERGED = "merged"
    ROLLED_BACK = "rolled_back"
    FAILED = "failed"


FINAL: Final = frozenset(
    {UpdateStatus.REFUSED, UpdateStatus.HEALTHY, UpdateStatus.MERGED, UpdateStatus.ROLLED_BACK, UpdateStatus.FAILED}
)
OPEN: Final = frozenset(set(UpdateStatus) - FINAL)
WITH_SUPERVISOR: Final = frozenset(
    {
        UpdateStatus.REQUESTED,
        UpdateStatus.BUILDING,
        UpdateStatus.DRAINING,
        UpdateStatus.SWITCHING,
        UpdateStatus.PROBATION,
    }
)


class UpdateKind(StrEnum):
    APP = "app"
    SUPERVISOR = "supervisor"


# --- names ----------------------------------------------------------------------------------------------------------
def update_id(row_id: int) -> str:
    return f"u{row_id:06d}"


def slug(text: str) -> str:
    """A few ASCII words of the owner's request, for the branch name ("Ovoz sozlamasini qo'sh" → "ovoz-sozlamasini")."""
    for mark in _APOSTROPHES:  # Uzbek o' g' and the tutuq belgisi stay inside their word
        text = text.replace(mark, "")
    folded = unicodedata.normalize("NFKD", text.lower()).encode("ascii", "ignore").decode()
    words: list[str] = []
    for word in _WORD.findall(folded):
        if len("-".join([*words, word])) > SLUG_MAX:
            break
        words.append(word)
    return "-".join(words) or "ozgarish"


def branch_name(row_id: int, text: str) -> str:
    return f"{BRANCH_PREFIX}{row_id}-{slug(text)}"


# --- the protected core ----------------------------------------------------------------------------------------------
def protected_globs(text: str) -> tuple[str, ...]:
    """The globs of a `.protected-core` file: active lines, and `# pending:` ones too (a change that creates the
    first file a pending glob names is a change to the core)."""
    globs: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("# pending:"):
            line = line.removeprefix("# pending:").strip()
        elif line.startswith("#"):
            continue
        if line:
            globs.append(line)
    return tuple(globs)


def _glob_regex(glob: str) -> re.Pattern[str]:
    parts: list[str] = []
    index = 0
    while index < len(glob):
        if glob.startswith("**", index):
            parts.append(".*")
            index += 2
        elif glob[index] == "*":
            parts.append("[^/]*")
            index += 1
        elif glob[index] == "?":
            parts.append("[^/]")
            index += 1
        else:
            parts.append(re.escape(glob[index]))
            index += 1
    return re.compile("".join(parts))


def matches(path: str, glob: str) -> bool:
    """`**` crosses directories, `*` and `?` do not (the A8 rule's `Path.glob` semantics)."""
    return _glob_regex(glob).fullmatch(path) is not None


def core_files(paths: Iterable[str], globs: Sequence[str]) -> tuple[str, ...]:
    return tuple(sorted({path for path in paths for glob in globs if matches(path, glob)}))


# --- safeguard settings (CONVENTIONS §5) -----------------------------------------------------------------------------
def _settings_declared(source: str) -> dict[str, str]:
    """`key → ast dump of its Setting(...)` for every declaration that carries `weakens=` (a safeguard)."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {}
    found: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
        if name != "Setting" or not node.args or not isinstance(node.args[0], ast.Constant):
            continue
        key = node.args[0].value
        if isinstance(key, str) and any(keyword.arg == "weakens" for keyword in node.keywords):
            found[key] = ast.dump(node, include_attributes=False)
    return found


def weakened_settings(before: str | None, after: str | None) -> tuple[str, ...]:
    """Safeguard settings whose declaration changed between two versions of `defaults.py`: its default, its check
    or its `weakens` rule, or the declaration is gone. Unreadable sources count as changed (fail closed)."""
    if before == after:
        return ()
    old = _settings_declared(before or "")
    new = _settings_declared(after or "")
    if not old or not new:
        return ("defaults.py",)
    return tuple(sorted(key for key, dump in old.items() if new.get(key) != dump))


# --- the plan for one finished turn ----------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ChangePlan:
    kind: UpdateKind | None  # None: the supervisor and the app together (refused, R-32)
    files: tuple[str, ...]
    core: tuple[str, ...] = ()  # protected-core files touched
    weakened: tuple[str, ...] = ()  # safeguard settings whose declaration changed

    @property
    def split_needed(self) -> bool:
        return self.kind is None

    @property
    def asks_owner(self) -> bool:
        return bool(self.core or self.weakened)


def plan_change(
    files: Iterable[str], globs: Sequence[str], *, defaults_before: str | None, defaults_after: str | None
) -> ChangePlan:
    changed = tuple(sorted(set(files)))
    supervisor = any(path.startswith(SUPERVISOR_PREFIX) for path in changed)
    other = any(not path.startswith(SUPERVISOR_PREFIX) for path in changed)
    kind = None if supervisor and other else (UpdateKind.SUPERVISOR if supervisor else UpdateKind.APP)
    defaults_path = f"{SETTINGS_DIR}/{DEFAULTS_NAME}"
    weakened = weakened_settings(defaults_before, defaults_after) if defaults_path in changed else ()
    return ChangePlan(kind=kind, files=changed, core=core_files(changed, globs), weakened=weakened)


def core_summary(plan: ChangePlan) -> tuple[str, ...]:
    """What the owner's one question names: the core files, then the safeguard settings."""
    return (*plan.core, *(f"sozlama {key}" for key in plan.weakened))


def request_json(
    *,
    update: str,
    kind: UpdateKind,
    commit: str,
    branch: str,
    summary: str,
    plan: ChangePlan,
    decision_id: int | None,
    at: datetime,
    health_timeout_s: int,
    drain_max_min: int,
) -> JsonObject:
    """`updates/<id>/request.json` (SPEC §3.2.12 step 3); behaviour values come from the settings snapshot."""
    files: list[JsonValue] = list(plan.files[:CHANGED_FILES_KEPT])
    return {
        "id": update,
        "kind": kind.value,
        "commit": commit,
        "branch": branch,
        "summary": summary[:SUMMARY_MAX],
        "touches_core": plan.asks_owner,
        "decision_id": decision_id,
        "changed_files": files,
        "requested_at": at.isoformat(),
        "health_timeout_s": health_timeout_s,
        "drain_max_min": drain_max_min,
    }


# --- the supervisor's answer -----------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Outcome:
    """`updates/<id>/status.json`, as far as the bot needs it."""

    status: UpdateStatus
    final: bool
    version: str = ""
    previous: str = ""
    code: str = ""
    detail: str = ""
    tests: Mapping[str, int] = field(default_factory=dict)
    previous_tests: Mapping[str, int] = field(default_factory=dict)
    alerted: bool = False
    switched_at: datetime | None = None  # kind=supervisor while switching: when `supervisor/current` moved


def _counts(raw: JsonValue) -> dict[str, int]:
    if not isinstance(raw, dict):
        return {}
    return {key: value for key, value in raw.items() if isinstance(value, int) and not isinstance(value, bool)}


def read_outcome(raw: JsonValue) -> Outcome | None:
    if not isinstance(raw, dict):
        return None
    status = raw.get("status")
    if not isinstance(status, str) or status not in set(UpdateStatus):
        return None

    def text(key: str) -> str:
        value = raw.get(key)
        return value if isinstance(value, str) else ""

    stamp = text("switched_at")
    try:
        switched_at = datetime.fromisoformat(stamp) if stamp else None
    except ValueError:
        switched_at = None
    return Outcome(
        status=UpdateStatus(status),
        final=raw.get("final") is True,
        version=text("version"),
        previous=text("previous"),
        code=text("result_code"),
        detail=text("detail"),
        tests=_counts(raw.get("tests")),
        previous_tests=_counts(raw.get("previous_tests")),
        alerted=raw.get("alerted") is True,
        switched_at=switched_at if switched_at is None or switched_at.tzinfo is not None else None,
    )


def reason_text(code: str, detail: str = "") -> str:
    entry = S.SELFUPDATE_REASONS.get(code)
    reason = entry() if entry is not None else S.SELFUPDATE_REASON_OTHER(code=code or "?")
    return S.SELFUPDATE_DETAIL(reason=reason, detail=detail) if detail else reason


def tests_text(outcome: Outcome, kind: UpdateKind) -> str:
    name = "supervisor" if kind is UpdateKind.SUPERVISOR else "app"
    count = outcome.tests.get(name)
    if count is None:
        return "?"
    previous = outcome.previous_tests.get(name)
    return (
        S.SELFUPDATE_TESTS(count=count, previous=previous)
        if previous is not None
        else S.SELFUPDATE_TESTS_FIRST(count=count)
    )


def changes_text(files: Sequence[str]) -> str:
    shown = ", ".join(files[:CHANGES_SHOWN])
    return f"{shown} (+{len(files) - CHANGES_SHOWN})" if len(files) > CHANGES_SHOWN else shown or "—"


def outcome_text(  # noqa: PLR0911 - one message per outcome
    outcome: Outcome, *, kind: UpdateKind, branch: str, summary: str, files: Sequence[str]
) -> BotText | None:
    """The owner's one message about a final outcome; None when the supervisor already told them (a rollback)."""
    project = SELF_PROJECT
    tests = tests_text(outcome, kind)
    changes = summary or changes_text(files)
    if outcome.status is UpdateStatus.MERGED:
        if kind is UpdateKind.SUPERVISOR:
            return S.SELFUPDATE_SUPERVISOR_DONE(project=project, version=outcome.version, tests=tests)
        return S.SELFUPDATE_DONE(project=project, version=outcome.version, changes=changes, tests=tests)
    if outcome.status is UpdateStatus.HEALTHY:
        return S.SELFUPDATE_DONE_NOT_MERGED(
            project=project, version=outcome.version, tests=tests, branch=branch, reason=reason_text(outcome.code)
        )
    if outcome.status is UpdateStatus.REFUSED:
        return S.SELFUPDATE_REFUSED(project=project, reason=reason_text(outcome.code, outcome.detail))
    if outcome.status is UpdateStatus.FAILED:
        return S.SELFUPDATE_FAILED(project=project, reason=reason_text(outcome.code, outcome.detail))
    if outcome.alerted:
        return None
    return S.SELFUPDATE_ROLLED_BACK(
        project=project, version=outcome.version, reason=reason_text(outcome.code), previous=outcome.previous
    )


# --- the supervisor self-switch watch (kind=supervisor, SPEC §3.2.12) -----------------------------------------------
def supervisor_switch_failed(
    outcome: Outcome, *, beat_release: str | None, beat_at: datetime | None, now: datetime, wait_s: int
) -> bool:
    """The new supervisor had `wait_s` from the switch and no heartbeat of its release came: switch it back."""
    if outcome.status is not UpdateStatus.SWITCHING or outcome.switched_at is None or outcome.final:
        return False
    if beat_release == outcome.version and beat_at is not None and beat_at >= outcome.switched_at:
        return False
    return (now - outcome.switched_at).total_seconds() >= wait_s
