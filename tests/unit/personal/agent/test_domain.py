"""The personal agent's guard, pure (SPEC §3.2.18 item 2): the gate verdict → allow / forbid / ask, the owner's grant
used once and only inside its window, a refusal, and the one question that may wait."""

from datetime import UTC, datetime, timedelta

import pytest

from yordamchi.core.json_value import JsonValue
from yordamchi.personal.agent import domain
from yordamchi.personal.agent.domain import Grant, PendingAsk, Step
from yordamchi.ports.agent import ToolCall
from yordamchi.shared.settings import defaults
from yordamchi.shared.settings.registry import Setting

HOME = "/Users/owner"
WORKSPACE = f"{HOME}/Documents/Projects/_shaxsiy"
DATA = f"{HOME}/Library/Application Support/Yordamchi"
CONFIG = f"{HOME}/.config/yordamchi"
NOW = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)
TTL_MIN = 120


def _default(setting: Setting[list[str]]) -> list[str]:
    """The shipped default of a `gate.*` list setting (the personal guard runs on them: ASSUMPTIONS 2026-10-31)."""
    value = setting.default
    assert isinstance(value, list)
    return value


RULES = domain.gate_rules(
    ask_classes=_default(defaults.GATE_ASK_CLASSES),
    readonly_remote_commands=_default(defaults.GATE_READONLY_REMOTE_COMMANDS),
    jump_only_hosts=_default(defaults.GATE_JUMP_ONLY_HOSTS),
    git_hosts=_default(defaults.GATE_GIT_HOSTS),
    http_write_allow_hosts=_default(defaults.GATE_HTTP_WRITE_ALLOW_HOSTS),
    temp_roots=_default(defaults.GATE_TEMP_ROOTS),
)
CTX = domain.gate_context(home=HOME, workspace=WORKSPACE, cwd=None, data_dir=DATA, config_dir=CONFIG)


def bash(command: str, **extra: JsonValue) -> ToolCall:
    return ToolCall("Bash", {"command": command, **extra}, "toolu_1", None, "session-1")


def tool(tool_name: str, /, **tool_input: JsonValue) -> ToolCall:
    return ToolCall(tool_name, tool_input, "toolu_1", None, "session-1")


def step(
    call: ToolCall,
    *,
    grants: tuple[Grant, ...] = (),
    refusals: tuple[Grant, ...] = (),
    pending: PendingAsk | None = None,
    now: datetime = NOW,
) -> Step:
    verdict = domain.judge(call, RULES, CTX)
    return domain.step_of(verdict, key=domain.key_of(call), grants=grants, refusals=refusals, pending=pending, now=now)


def test_the_context_is_the_full_policy_rooted_in_the_workspace_with_git_facts_unknown() -> None:
    assert CTX.policy.value == "FULL"
    assert CTX.project_root == WORKSPACE
    assert CTX.cwd == WORKSPACE  # no `cd` yet: the workspace
    assert CTX.untracked is None
    assert CTX.deploy_branches is None
    assert (
        domain.gate_context(home=HOME, workspace=WORKSPACE, cwd="/tmp", data_dir=DATA, config_dir=CONFIG).cwd == "/tmp"
    )


@pytest.mark.parametrize(
    "call",
    [
        bash("ls -la"),
        bash("python3 -m venv .venv"),
        bash("mkdir -p sayt && cd sayt && touch index.html"),
        tool("Write", file_path=f"{WORKSPACE}/notes.md", content="salom"),
        tool("Read", file_path=f"{WORKSPACE}/notes.md"),
        tool("WebSearch", query="ob-havo Toshkent"),
    ],
)
def test_ordinary_work_runs(call: ToolCall) -> None:
    assert step(call) is Step.ALLOW


@pytest.mark.parametrize(
    "call",
    [
        bash("ssh prod 'rm -rf /srv'"),
        bash("vercel deploy --prod"),
        bash("aws ec2 run-instances --image-id ami-1 --count 3"),
        bash("git push origin main"),  # the workspace has no known deploy branches: an extra question, never a miss
    ],
)
def test_a_dangerous_call_asks(call: ToolCall) -> None:
    assert step(call) is Step.ASK


@pytest.mark.parametrize(
    "call",
    [
        bash("cat ~/.claude/.credentials.json"),
        tool("Read", file_path=f"{HOME}/.claude/.credentials.json"),
        tool("Read", file_path=f"{DATA}/personal.db"),
        tool("Edit", file_path=f"{HOME}/.claude/settings.json", old_string="a", new_string="b"),
        bash("launchctl kickstart -k gui/501/uz.yordamchi.personal"),
    ],
)
def test_a_forbidden_call_is_denied_and_never_asked(call: ToolCall) -> None:
    assert step(call) is Step.FORBID
    key = domain.key_of(call)
    grant = domain.grant_of(key, now=NOW, ttl_min=TTL_MIN)
    assert step(call, grants=(grant,)) is Step.FORBID  # not even a grant opens it


def test_a_grant_runs_that_exact_call_once() -> None:
    call = bash("vercel deploy --prod")
    grant = domain.grant_of(domain.key_of(call), now=NOW, ttl_min=TTL_MIN)
    assert step(call, grants=(grant,)) is Step.GRANTED
    left = domain.without((grant,), domain.key_of(call))
    assert left == ()
    assert step(call, grants=left) is Step.ASK  # used up: the next one asks again
    other = bash("vercel deploy --prod --force")
    assert step(other, grants=(grant,)) is Step.ASK  # a different command is a different key


def test_a_grant_expires_after_the_approval_window() -> None:
    call = bash("ssh prod 'rm -rf /srv'")
    grant = domain.grant_of(domain.key_of(call), now=NOW, ttl_min=TTL_MIN)
    assert grant.expires_at == NOW + timedelta(minutes=TTL_MIN)
    assert step(call, grants=(grant,), now=NOW + timedelta(minutes=TTL_MIN - 1)) is Step.GRANTED
    assert step(call, grants=(grant,), now=NOW + timedelta(minutes=TTL_MIN)) is Step.ASK
    assert domain.live((grant,), NOW + timedelta(minutes=TTL_MIN)) == ()


def test_without_uses_up_only_the_first_grant_of_a_key() -> None:
    first = Grant(key="a", expires_at=NOW)
    second = Grant(key="a", expires_at=NOW + timedelta(minutes=1))
    other = Grant(key="b", expires_at=NOW)
    assert domain.without((other, first, second), "a") == (other, second)
    assert domain.without((other,), "a") == (other,)


def test_a_refusal_denies_without_asking_again_until_it_expires() -> None:
    call = bash("vercel deploy --prod")
    refusal = domain.grant_of(domain.key_of(call), now=NOW, ttl_min=TTL_MIN)
    assert step(call, refusals=(refusal,)) is Step.REFUSED
    assert step(call, refusals=(refusal,), now=NOW + timedelta(minutes=TTL_MIN)) is Step.ASK


def test_one_question_waits_at_a_time() -> None:
    first = bash("vercel deploy --prod")
    pending = PendingAsk(number=1, key=domain.key_of(first), what="vercel deploy --prod", reason="deploy_cli")
    assert step(first, pending=pending) is Step.ASK  # the same call again: still the one question
    assert step(bash("ssh prod 'rm -rf /srv'"), pending=pending) is Step.WAITING


@pytest.mark.parametrize("value", [Step.ALLOW, Step.GRANTED])
def test_allowed_steps(value: Step) -> None:
    assert value.allowed


@pytest.mark.parametrize("value", [Step.FORBID, Step.REFUSED, Step.ASK, Step.WAITING])
def test_denied_steps(value: Step) -> None:
    assert not value.allowed


def test_the_key_ignores_cosmetic_fields_and_surrounding_space() -> None:
    plain = domain.key_of(bash("vercel deploy --prod"))
    assert domain.key_of(bash("  vercel deploy --prod\n", description="Deploy", timeout=60000)) == plain
    assert domain.key_of(bash("vercel deploy")) != plain


def test_describe_shows_the_command_or_the_tool_and_its_input() -> None:
    assert domain.describe(bash("  vercel deploy --prod ")) == "vercel deploy --prod"
    described = domain.describe(tool("WebFetch", url="https://example.com"))
    assert described.startswith("WebFetch ")
    assert "https://example.com" in described


def test_shortened_cuts_with_an_ellipsis() -> None:
    assert domain.shortened("abc", 5) == "abc"
    assert domain.shortened("abcdefgh", 5) == "abcd…"
    assert len(domain.shortened("x" * 1000)) == domain.WHAT_SHOWN_CHARS


def test_the_question_names_the_number_the_action_and_its_consequence() -> None:
    ask = PendingAsk(number=3, key="k", what="vercel deploy --prod", reason="deploy_cli")
    text = domain.ask_text(ask)
    assert "#3" in text
    assert "vercel deploy --prod" in text
    assert "prod" in text
    assert "«ha» yoki «yo'q»" in text
    unknown = domain.ask_text(PendingAsk(number=4, key="k", what="x", reason="no_such_reason"))
    assert "—" in unknown
