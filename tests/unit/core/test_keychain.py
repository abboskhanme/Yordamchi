"""core/keychain: Keychain references, the `security` command lines and the per-bot scope (UC-36; owner requirement
2026-10-24). Every test runs on `FakeSecurity`; nothing here touches the real Keychain."""

from collections.abc import Sequence

import pytest

from tests.factories import DEV_BOT_TOKEN, PERSONAL_BOT_TOKEN
from tests.fakes.security import LOCKED_EXIT, FakeSecurity
from yordamchi.core import keychain, proc, redact
from yordamchi.core import strings as S
from yordamchi.core.errors import KeychainItemForbidden, KeychainItemMissing, KeychainUnavailable
from yordamchi.core.keychain import DEV_BOT_TOKEN as DEV_ITEM
from yordamchi.core.keychain import MASTER_KEY, Keychain, KeychainItem
from yordamchi.core.keychain import PERSONAL_BOT_TOKEN as PERSONAL_ITEM
from yordamchi.core.paths import BotName

CLAUDE_CREDENTIALS = KeychainItem("Claude-Code-credentials", "owner")


def result(returncode: int, stdout: bytes = b"") -> proc.ProcResult:
    return proc.ProcResult(argv=(keychain.SECURITY,), returncode=returncode, stdout=stdout, stderr=b"", duration_ms=1)


# --- references ----------------------------------------------------------------------------------------------------
def test_a_reference_names_service_and_account() -> None:
    assert DEV_ITEM.reference == "@keychain:uz.yordamchi/dev-bot-token"
    assert keychain.parse_reference(DEV_ITEM.reference) == DEV_ITEM
    assert keychain.is_reference(PERSONAL_ITEM.reference)
    assert not keychain.is_reference(DEV_BOT_TOKEN)


@pytest.mark.parametrize(
    "value",
    [
        "@keychain:",
        "@keychain:uz.yordamchi",
        "@keychain:uz.yordamchi/",
        "@keychain:/dev-bot-token",
        "@keychain:uz yordamchi/dev-bot-token",
        '@keychain:uz.yordamchi/dev"bot',
        "@keychain:uz.yordamchi/dev/bot",
        "keychain:uz.yordamchi/dev-bot-token",
    ],
)
def test_a_malformed_reference_is_refused(value: str) -> None:
    with pytest.raises(ValueError, match=r"Keychain|keychain"):
        keychain.parse_reference(value)


# --- the command lines ---------------------------------------------------------------------------------------------
def test_reading_puts_only_the_item_name_on_argv() -> None:
    assert keychain.find_argv(DEV_ITEM, reveal=True) == [
        "/usr/bin/security",
        "find-generic-password",
        "-s",
        "uz.yordamchi",
        "-a",
        "dev-bot-token",
        "-w",
    ]
    assert keychain.find_argv(DEV_ITEM, reveal=False)[-1] == "dev-bot-token"
    assert keychain.delete_argv(DEV_ITEM) == [
        "/usr/bin/security",
        "delete-generic-password",
        "-s",
        "uz.yordamchi",
        "-a",
        "dev-bot-token",
    ]


def test_writing_sends_the_value_on_stdin_with_security_as_the_trusted_reader() -> None:
    assert keychain.write_argv() == ["/usr/bin/security", "-i"]
    script = keychain.add_script(DEV_ITEM, DEV_BOT_TOKEN)
    assert (
        script
        == (
            f'add-generic-password -U -s "uz.yordamchi" -a "dev-bot-token" -T /usr/bin/security -w "{DEV_BOT_TOKEN}"\n'
        ).encode()
    )


@pytest.mark.parametrize("value", ["", "has space", 'quo"te', "back\\slash", "new\nline", "x" * 4097, "naïve"])
def test_a_value_stdin_could_misread_is_refused_without_echoing_it(value: str) -> None:
    with pytest.raises(ValueError, match="refusing to store") as caught:
        keychain.add_script(DEV_ITEM, value)
    if value:
        assert value not in str(caught.value)


def test_password_from_maps_the_exit_codes() -> None:
    assert keychain.password_from(result(0, b"abc:DEF\n"), DEV_ITEM) == "abc:DEF"
    with pytest.raises(KeychainItemMissing) as missing:
        keychain.password_from(result(keychain.ITEM_NOT_FOUND_EXIT), DEV_ITEM)
    assert missing.value.params == {"key": "uz.yordamchi/dev-bot-token", "problem": "keychain_item_missing"}
    with pytest.raises(KeychainItemMissing):
        keychain.password_from(result(0, b"\n"), DEV_ITEM)
    for returncode, stdout in ((LOCKED_EXIT, b""), (1, b""), (0, b"\xff\xfe")):
        with pytest.raises(KeychainUnavailable):
            keychain.password_from(result(returncode, stdout), DEV_ITEM)


# --- the per-bot scope (UC-36) --------------------------------------------------------------------------------------
def test_each_bot_holds_only_its_own_token_and_the_master_key() -> None:
    assert keychain.ALLOWED[BotName.DEV] == {DEV_ITEM, MASTER_KEY}
    assert keychain.ALLOWED[BotName.PERSONAL] == {PERSONAL_ITEM, MASTER_KEY}
    assert DEV_ITEM not in keychain.ALLOWED[BotName.PERSONAL]


def test_the_personal_process_can_never_read_the_dev_token() -> None:
    security = FakeSecurity()
    security.put(DEV_ITEM, DEV_BOT_TOKEN)
    chain = Keychain(BotName.PERSONAL, runner=security)
    with pytest.raises(KeychainItemForbidden) as caught:
        chain.read(DEV_ITEM)
    assert caught.value.params == {"key": "uz.yordamchi/dev-bot-token", "problem": "not_for_the_personal_bot"}
    assert security.calls == []  # refused before `security` runs
    assert S.ERRORS[caught.value.code](**caught.value.params)


@pytest.mark.parametrize("bot", list(BotName))
def test_no_bot_may_name_another_service(bot: BotName) -> None:
    security = FakeSecurity()
    security.put(CLAUDE_CREDENTIALS, "x" * 30)
    with pytest.raises(KeychainItemForbidden):
        Keychain(bot, runner=security).read(CLAUDE_CREDENTIALS)
    with pytest.raises(KeychainItemForbidden):
        Keychain(bot, runner=security).write(CLAUDE_CREDENTIALS, "y" * 30)
    assert security.calls == []


def test_the_personal_process_cannot_write_the_dev_token_either() -> None:
    security = FakeSecurity()
    with pytest.raises(KeychainItemForbidden):
        Keychain(BotName.PERSONAL, runner=security).write(DEV_ITEM, DEV_BOT_TOKEN)
    assert security.items == {}


# --- reading and writing ---------------------------------------------------------------------------------------------
def test_read_returns_the_value_and_masks_it_at_once() -> None:
    security = FakeSecurity()
    security.put(PERSONAL_ITEM, PERSONAL_BOT_TOKEN)
    assert Keychain(BotName.PERSONAL, runner=security).read(PERSONAL_ITEM) == PERSONAL_BOT_TOKEN
    assert PERSONAL_BOT_TOKEN not in redact.scrubbed(f"token {PERSONAL_BOT_TOKEN}")
    assert security.calls == [tuple(keychain.find_argv(PERSONAL_ITEM, reveal=True))]


def test_a_missing_item_and_a_locked_keychain_are_told_apart() -> None:
    security = FakeSecurity()
    chain = Keychain(BotName.DEV, runner=security)
    with pytest.raises(KeychainItemMissing):
        chain.read(DEV_ITEM)
    security.put(DEV_ITEM, DEV_BOT_TOKEN)
    security.locked = True
    with pytest.raises(KeychainUnavailable) as caught:
        chain.read(DEV_ITEM)
    assert caught.value.params == {"key": "uz.yordamchi/dev-bot-token", "problem": "keychain_unavailable"}


@pytest.mark.parametrize("failure", [TimeoutError("security"), FileNotFoundError("/usr/bin/security")])
def test_a_hung_or_absent_security_is_unavailable(failure: Exception) -> None:
    def runner(argv: Sequence[str], *, stdin: bytes | None, timeout: float) -> proc.ProcResult:
        raise failure

    with pytest.raises(KeychainUnavailable):
        Keychain(BotName.DEV, runner=runner).read(MASTER_KEY)


def test_write_keeps_the_value_off_argv() -> None:
    security = FakeSecurity()
    Keychain(BotName.DEV, runner=security).write(MASTER_KEY, "a2V5" * 11)
    assert security.get(MASTER_KEY) == "a2V5" * 11
    assert security.trusted[(MASTER_KEY.service, MASTER_KEY.account)] == "/usr/bin/security"
    assert security.calls == [("/usr/bin/security", "-i")]
    assert all("a2V5" * 11 not in part for call in security.calls for part in call)


def test_a_failed_write_is_unavailable() -> None:
    security = FakeSecurity(fail_writes=True)
    with pytest.raises(KeychainUnavailable):
        Keychain(BotName.DEV, runner=security).write(MASTER_KEY, "a2V5" * 11)


def test_the_default_runner_is_core_proc_with_a_clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def fake_run_sync(argv: Sequence[str], **kwargs: object) -> proc.ProcResult:
        seen["argv"] = list(argv)
        seen.update(kwargs)
        return result(0, b"v\n")

    monkeypatch.setattr(proc, "run_sync", fake_run_sync)
    keychain.security_runner(["/usr/bin/security", "-i"], stdin=b"x\n", timeout=2.0)
    assert seen["argv"] == ["/usr/bin/security", "-i"]
    assert (seen["stdin"], seen["timeout"]) == (b"x\n", 2.0)
    env = seen["env"]
    assert isinstance(env, dict)
    assert env["PATH"] == "/usr/bin:/bin:/usr/sbin:/sbin"
    assert not any(name.startswith(("YORDAMCHI_", "ANTHROPIC_", "CLAUDE_CODE_")) for name in env)


def test_repr_names_the_scope_only() -> None:
    assert repr(Keychain(BotName.PERSONAL, runner=FakeSecurity())) == "Keychain(bot=personal)"
