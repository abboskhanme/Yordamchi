"""`python -m yordamchi.check` (SPEC §9.3 step 8, §12; T28) against a fixture Mac: files, the Keychain, launchd,
heartbeats and a fake Bot API. The network stays closed: the Telegram calls go to an injected fake, and `UrllibBotApi`
is driven through an in-process urllib handler. `security` answers from a `FakeSecurity`, never the real Keychain."""

import base64
import email.message
import io
import os
import urllib.error
import urllib.request
import urllib.response
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from tests.factories import DEV_BOT_TOKEN, DEV_GROUP_ID, OWNER_ID, PERSONAL_BOT_TOKEN, make_paths, write_master_key
from tests.fakes.security import FakeSecurity
from yordamchi.bots.cli import check
from yordamchi.bots.cli.check import Context, Level, Line, Params, Report, RunResult, TelegramError
from yordamchi.core import keychain, redact
from yordamchi.core.paths import BotName, Paths, ProcessName

NOW = 1_800_000_000.0
BOT_ID = 7_000_000_001
AUTH_OK = '{"loggedIn": true, "authMethod": "claude.ai", "email": "owner@example.com", "orgName": "Owner org"}'
PMSET_OK = "Battery Power:\n sleep                1\nAC Power:\n displaysleep         10\n sleep                0\n"


# --- fakes -----------------------------------------------------------------------------------------------------------
MASTER_KEY_B64 = base64.b64encode(bytes(range(32))).decode()


def fixture_keychain() -> FakeSecurity:
    security = FakeSecurity()
    security.put(keychain.DEV_BOT_TOKEN, DEV_BOT_TOKEN)
    security.put(keychain.PERSONAL_BOT_TOKEN, PERSONAL_BOT_TOKEN)
    security.put(keychain.MASTER_KEY, MASTER_KEY_B64)
    return security


@dataclass
class FakeRunner:
    """`launchctl print` answers from `loaded`; `security` from `keychain`; every other command from `answers` (by
    argv[0] basename + argv[1]). The fixture Mac has all three plists installed (`--enable-personal`), so all three
    jobs are loaded by default."""

    loaded: set[str] = field(
        default_factory=lambda: {"uz.yordamchi.dev", "uz.yordamchi.supervisor", "uz.yordamchi.personal"}
    )
    answers: dict[str, RunResult] = field(default_factory=dict)
    calls: list[tuple[str, ...]] = field(default_factory=list)
    keychain: FakeSecurity = field(default_factory=fixture_keychain)

    async def __call__(self, argv: Sequence[str], timeout: float) -> RunResult:
        self.calls.append(tuple(argv))
        name = Path(argv[0]).name
        if name == "security":
            done = self.keychain(argv, stdin=None, timeout=timeout)
            return RunResult(done.returncode, done.stdout.decode())
        if name == "launchctl":
            label = argv[2].rsplit("/", 1)[-1]
            return RunResult(0 if label in self.loaded else 113, "")
        key = f"{name} {argv[1]}"
        defaults = {
            "claude auth": RunResult(0, AUTH_OK),
            "pmset -g": RunResult(0, PMSET_OK),
            "fdesetup status": RunResult(0, "FileVault is On.\n"),
        }
        return self.answers.get(key, defaults.get(key, RunResult(1, "")))


@dataclass
class FakeBotApi:
    results: dict[str, object]
    calls: list[tuple[str, dict[str, str | int]]] = field(default_factory=list)

    def call(self, method: str, params: Params | None = None) -> object:
        self.calls.append((method, dict(params or {})))
        result = self.results.get(method)
        if isinstance(result, TelegramError):
            raise result
        if callable(result):
            return result(dict(params or {}))
        return result


def good_dev_api() -> FakeBotApi:
    def member(params: dict[str, str | int]) -> object:
        if params["user_id"] == BOT_ID:
            return {"status": "administrator", "can_delete_messages": True, "can_pin_messages": True}
        return {"status": "creator", "is_anonymous": False}

    return FakeBotApi(
        {
            "getMe": {
                "id": BOT_ID,
                "is_bot": True,
                "username": "OrvionXBot",
                "can_join_groups": False,
                "can_read_all_group_messages": True,
            },
            "getMyCommands": [],
            "getChat": {"id": DEV_GROUP_ID, "type": "supergroup", "title": "Yordamchi dev"},
            "getChatMember": member,
        }
    )


def good_personal_api() -> FakeBotApi:
    return FakeBotApi(
        {
            "getMe": {"id": 7_000_000_002, "username": "OrvionX_AI_Bot", "can_join_groups": False},
            "getMyCommands": [],
        }
    )


@dataclass
class Mac:
    """A fixture Mac: the DATA tree with the right modes, env files, key, plists, heartbeats and binaries."""

    paths: Paths
    runner: FakeRunner
    apis: dict[str, FakeBotApi]
    bin_dir: Path
    environ: dict[str, str] = field(default_factory=dict)

    def context(self, *, telegram: bool = True) -> Context:
        return Context(
            paths=self.paths,
            environ=self.environ,
            uid=501,
            now=NOW,
            run=self.runner,
            bot_api=lambda token: self.apis[token],
            search_path=f"{self.paths.local_bin}:{self.bin_dir}:{self.paths.bin}",
            telegram=telegram,
        )

    async def report(self, *, telegram: bool = True) -> Report:
        return await check.run_checks(self.context(telegram=telegram))


def _exe(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(0o755)


def write_env(path: Path, values: Mapping[str, str | int]) -> None:
    path.write_text("".join(f"{key}={value}\n" for key, value in values.items()))
    path.chmod(0o600)


def heartbeat(paths: Paths, process: str, *, age_s: float) -> None:
    path = paths.run / f"{process}.heartbeat.json"
    path.write_text('{"pid": 1}')
    os.utime(path, (NOW - age_s, NOW - age_s))


@pytest.fixture
def mac(tmp_path: Path) -> Mac:
    paths = make_paths(tmp_path)
    for directory in (paths.data, paths.db_dir, paths.run, paths.config, paths.logs):
        directory.mkdir(parents=True, exist_ok=True)
        directory.chmod(0o700)
    write_env(
        paths.env_file(BotName.DEV),
        {"DEV_BOT_TOKEN": keychain.DEV_BOT_TOKEN.reference, "OWNER_ID": OWNER_ID, "DEV_GROUP_ID": DEV_GROUP_ID},
    )
    write_env(
        paths.env_file(BotName.PERSONAL),
        {"PERSONAL_BOT_TOKEN": keychain.PERSONAL_BOT_TOKEN.reference, "OWNER_ID": OWNER_ID, "PERSONAL_GROUP_ID": ""},
    )
    for bot in BotName:
        paths.db_file(bot).write_bytes(b"")
        paths.db_file(bot).chmod(0o600)
    for name in ("claude", "gh", "node"):
        _exe(paths.local_bin / name)
    bin_dir = tmp_path / "usr-bin"
    _exe(bin_dir / "git")
    _exe(paths.bin / "ffmpeg")
    processes: tuple[ProcessName, ...] = ("dev", "supervisor", "personal")
    for process in processes:
        plist = check.plist_path(paths, process)
        plist.parent.mkdir(parents=True, exist_ok=True)
        plist.write_text("<plist/>")
    heartbeat(paths, "supervisor", age_s=10)
    heartbeat(paths, "dev", age_s=20)
    return Mac(
        paths=paths,
        runner=FakeRunner(),
        apis={DEV_BOT_TOKEN: good_dev_api(), PERSONAL_BOT_TOKEN: good_personal_api()},
        bin_dir=bin_dir,
    )


def failures(report: Report) -> list[str]:
    return [line.text for line in report.lines if line.level is Level.FAIL]


def texts(report: Report, level: Level) -> list[str]:
    return [line.text for line in report.lines if line.level is level]


# --- the whole report ------------------------------------------------------------------------------------------------
async def test_a_fully_set_up_mac_is_all_green(mac: Mac) -> None:
    report = await mac.report()
    assert failures(report) == []
    rendered = check.render(report, colour=False)
    assert "Natija: hammasi joyida" in rendered
    assert 'authMethod "claude.ai"' in rendered
    assert any("anonim admin" in text for text in texts(report, Level.INFO))
    assert any("2FA" in text for text in texts(report, Level.INFO))


async def test_the_report_never_prints_a_token_or_the_claude_account(mac: Mac) -> None:
    mac.apis[DEV_BOT_TOKEN].results["getMe"] = TelegramError("unauthorized", 401)
    report = await mac.report()
    rendered = check.render(report, colour=True)
    for secret in (DEV_BOT_TOKEN, PERSONAL_BOT_TOKEN, "owner@example.com", "Owner org"):
        assert secret not in rendered
    assert "dev bot: getMe: Telegram xatosi (unauthorized)" in failures(report)


def test_a_line_that_carried_a_token_by_mistake_is_still_masked() -> None:
    redact.add_known_secrets([DEV_BOT_TOKEN])
    report = Report([("x", [Line(Level.FAIL, f"oops {DEV_BOT_TOKEN}")])])
    assert DEV_BOT_TOKEN not in check.render(report, colour=False)


async def test_exit_code_is_one_when_any_line_is_red(mac: Mac) -> None:
    mac.paths.env_file(BotName.DEV).chmod(0o644)
    report = await mac.report()
    assert report.failures >= 1


# --- files -----------------------------------------------------------------------------------------------------------
async def test_wrong_modes_are_red_with_the_fix(mac: Mac) -> None:
    mac.paths.env_file(BotName.DEV).chmod(0o644)
    mac.paths.data.chmod(0o755)
    mac.paths.config.chmod(0o750)
    report = await mac.report()
    fails = [line for line in report.lines if line.level is Level.FAIL]
    assert any("dev.env: ruxsat 0644, kerak 0600" in line.text for line in fails)
    assert any("ruxsat 0755, kerak 0700" in line.text for line in fails)
    assert any(line.text.endswith("config: ruxsat 0750, kerak 0700") for line in fails)
    assert any(line.hint.startswith("chmod 600") for line in fails)


# --- secrets: references only, values in the Keychain ----------------------------------------------------------------
async def test_the_keychain_lines_are_green_and_never_read_a_value_for_them(mac: Mac) -> None:
    report = await mac.report(telegram=False)
    oks = texts(report, Level.OK)
    assert "dev.env: ochiq maxfiy qiymat yo'q (faqat Keychain havolalari)" in oks
    assert "uz.yordamchi/dev-bot-token: Keychain'da bor (dev.env DEV_BOT_TOKEN)" in oks
    assert "uz.yordamchi/personal-bot-token: Keychain'da bor (personal.env PERSONAL_BOT_TOKEN)" in oks
    assert "Ombor kaliti: Keychain'da (uz.yordamchi/vault-master-key)" in oks
    security_calls = [call for call in mac.runner.calls if call[0] == keychain.SECURITY]
    assert security_calls
    assert all("-w" not in call for call in security_calls)  # offline: existence only, no value is read


async def test_a_plaintext_token_in_an_env_file_is_red_and_never_used(mac: Mac) -> None:
    write_env(mac.paths.env_file(BotName.DEV), {"DEV_BOT_TOKEN": DEV_BOT_TOKEN, "OWNER_ID": OWNER_ID})
    report = await mac.report()
    [line] = [line for line in report.lines if "DEV_BOT_TOKEN ochiq holda yozilgan" in line.text]
    assert line.level is Level.FAIL
    assert line.hint == "python scripts/install.py --env-only --rotate-tokens"
    assert mac.apis[DEV_BOT_TOKEN].calls == []  # a plaintext token is never used, not even for getMe
    assert DEV_BOT_TOKEN not in check.render(report, colour=False)


async def test_item_names_survive_the_scrubber_in_the_rendered_report(mac: Mac) -> None:
    rendered = check.render(await mac.report(telegram=False), colour=False)
    assert "uz.yordamchi/dev-bot-token: Keychain'da bor" in rendered
    assert "Ombor kaliti: Keychain'da (uz.yordamchi/vault-master-key)" in rendered


async def test_any_plaintext_secret_in_an_env_file_is_red(mac: Mac) -> None:
    path = mac.paths.env_file(BotName.PERSONAL)
    path.write_text(path.read_text() + "GOOGLE_API_KEY=AIza" + "SyntheticKeyForOfflineTests_0123456\n")
    assert "personal.env: GOOGLE_API_KEY ochiq holda yozilgan (Keychain havolasi emas)" in failures(await mac.report())


async def test_a_reference_to_a_missing_item_is_red_with_the_rotation_hint(mac: Mac) -> None:
    mac.runner.keychain.items.pop((keychain.SERVICE, "dev-bot-token"))
    report = await mac.report()
    [line] = [line for line in report.lines if line.text == "uz.yordamchi/dev-bot-token: Keychain'da topilmadi"]
    assert line.hint == "python scripts/install.py --env-only --rotate-tokens"
    assert mac.apis[DEV_BOT_TOKEN].calls == []


async def test_a_locked_keychain_is_red(mac: Mac) -> None:
    mac.runner.keychain.locked = True
    fails = failures(await mac.report())
    assert "uz.yordamchi/dev-bot-token: Keychain'dan o'qilmadi (qulflanganmi?)" in fails
    assert "Ombor kaliti: Keychain'da uz.yordamchi/vault-master-key yo'q yoki o'qilmadi" in fails


async def test_the_personal_env_may_not_point_at_the_dev_token(mac: Mac) -> None:
    """UC-36: even a reference to another bot's item is red, and the check never reads it for that bot."""
    write_env(
        mac.paths.env_file(BotName.PERSONAL),
        {"PERSONAL_BOT_TOKEN": keychain.DEV_BOT_TOKEN.reference, "OWNER_ID": OWNER_ID},
    )
    report = await mac.report()
    expected = "personal.env: PERSONAL_BOT_TOKEN boshqa botning yozuvini ko'rsatadi"
    assert any(text.startswith(expected) for text in failures(report))
    assert mac.apis[PERSONAL_BOT_TOKEN].calls == []


async def test_a_token_key_naming_the_master_key_is_red_and_the_key_is_never_read(mac: Mac) -> None:
    write_env(
        mac.paths.env_file(BotName.DEV),
        {"DEV_BOT_TOKEN": keychain.MASTER_KEY.reference, "OWNER_ID": OWNER_ID, "DEV_GROUP_ID": DEV_GROUP_ID},
    )
    report = await mac.report()
    [line] = [line for line in report.lines if "DEV_BOT_TOKEN boshqa yozuvni" in line.text]
    assert line.level is Level.FAIL
    assert "uz.yordamchi/dev-bot-token" in line.hint
    reads = [call for call in mac.runner.calls if call[0] == keychain.SECURITY and "-w" in call]
    assert reads == [tuple(keychain.find_argv(keychain.PERSONAL_BOT_TOKEN, reveal=True))]  # never the master key
    assert MASTER_KEY_B64 not in check.render(report, colour=False)


async def test_a_missing_master_key_item_is_red(mac: Mac) -> None:
    mac.runner.keychain.items.pop((keychain.SERVICE, "vault-master-key"))
    fails = failures(await mac.report())
    assert "Ombor kaliti: Keychain'da uz.yordamchi/vault-master-key yo'q yoki o'qilmadi" in fails


async def test_a_leftover_master_key_file_is_red(mac: Mac) -> None:
    write_master_key(mac.paths.master_key)
    report = await mac.report()
    [line] = [line for line in report.lines if "master.key" in line.text]
    assert line.level is Level.FAIL
    assert line.text.endswith("master.key: kalit hali fayl sifatida turibdi")
    assert "Keychain'ga ko'chiriladi" in line.hint


async def test_personal_env_is_optional_before_p3(mac: Mac) -> None:
    mac.paths.env_file(BotName.PERSONAL).unlink()
    report = await mac.report()
    assert failures(report) == []
    assert "personal.env: hali yo'q (P3 gacha shart emas)" in texts(report, Level.INFO)


# --- claude and credentials ------------------------------------------------------------------------------------------
async def test_an_api_key_login_is_red(mac: Mac) -> None:
    mac.runner.answers["claude auth"] = RunResult(0, '{"loggedIn": true, "authMethod": "api_key"}')
    report = await mac.report()
    assert 'claude auth status: authMethod "api_key", kerak "claude.ai"' in failures(report)


async def test_logged_out_or_unreadable_claude_is_red(mac: Mac) -> None:
    mac.runner.answers["claude auth"] = RunResult(1, '{"loggedIn": false}')
    assert "claude auth status: tizimga kirilmagan" in failures(await mac.report())
    mac.runner.answers["claude auth"] = RunResult(None)
    assert "claude auth status: javob o'qilmadi" in failures(await mac.report())


async def test_a_refused_credential_source_is_named_never_valued(mac: Mac) -> None:
    mac.environ["ANTHROPIC_BASE_URL"] = "https://proxy.invalid/secret-path"
    settings = mac.paths.claude_user_settings
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text('{"apiKeyHelper": "/usr/local/bin/give-key", "cleanupPeriodDays": 30}')
    report = await mac.report()
    fails = failures(report)
    assert "Taqiqlangan kirish manbai: env:ANTHROPIC_BASE_URL" in fails
    assert "Taqiqlangan kirish manbai: settings.json:apiKeyHelper" in fails
    assert "secret-path" not in check.render(report, colour=False)


async def test_missing_binaries_are_red(mac: Mac) -> None:
    (mac.paths.bin / "ffmpeg").unlink()
    (mac.paths.local_bin / "gh").unlink()
    fails = failures(await mac.report())
    assert "ffmpeg: topilmadi" in fails
    assert "gh: topilmadi" in fails


async def test_a_missing_claude_binary_is_red(mac: Mac) -> None:
    (mac.paths.local_bin / "claude").unlink()
    assert "claude: ~/.local/bin/claude topilmadi" in failures(await mac.report())


# --- launchd and heartbeats ------------------------------------------------------------------------------------------
async def test_unloaded_jobs_are_red_but_a_personal_bot_not_enabled_is_not(mac: Mac) -> None:
    check.plist_path(mac.paths, "personal").unlink()  # the installer ran without --enable-personal
    mac.runner.loaded = {"uz.yordamchi.dev"}
    report = await mac.report()
    assert "uz.yordamchi.supervisor: yuklanmagan" in failures(report)
    assert "uz.yordamchi.personal: yoqilmagan" in texts(report, Level.INFO)
    assert ("/bin/launchctl", "print", "gui/501/uz.yordamchi.dev") in mac.runner.calls


async def test_an_enabled_personal_bot_must_be_loaded(mac: Mac) -> None:
    """T77: once `--enable-personal` installed its plist, an unloaded personal bot is red, like the dev bot."""
    mac.runner.loaded = {"uz.yordamchi.dev", "uz.yordamchi.supervisor"}
    report = await mac.report()
    assert "uz.yordamchi.personal: yuklanmagan" in failures(report)
    mac.runner.loaded = {"uz.yordamchi.dev", "uz.yordamchi.supervisor", "uz.yordamchi.personal"}
    assert "uz.yordamchi.personal: launchd'da yuklangan" in texts(await mac.report(), Level.OK)


async def test_an_enabled_personal_bot_s_heartbeat_is_judged_like_the_dev_bot_s(mac: Mac) -> None:
    heartbeat(mac.paths, "personal", age_s=200)
    assert "personal heartbeat: eskirgan (200 s oldin, chegara 120 s)" in failures(await mac.report())
    heartbeat(mac.paths, "personal", age_s=15)
    assert "personal heartbeat: yangi (15 s oldin)" in texts(await mac.report(), Level.OK)


async def test_a_personal_heartbeat_left_from_before_it_was_switched_off_is_not_judged(mac: Mac) -> None:
    check.plist_path(mac.paths, "personal").unlink()
    heartbeat(mac.paths, "personal", age_s=86_400)
    report = await mac.report()
    assert not [text for text in failures(report) if text.startswith("personal")]
    assert "personal bot heartbeat: bot yoqilmagan" in texts(report, Level.INFO)


async def test_a_missing_plist_is_red(mac: Mac) -> None:
    mac.runner.loaded = set()
    check.plist_path(mac.paths, "dev").unlink()
    assert any(text.startswith("uz.yordamchi.dev: plist yo'q") for text in failures(await mac.report()))


async def test_heartbeats_supervisor_must_be_fresh_bots_once_running(mac: Mac) -> None:
    heartbeat(mac.paths, "supervisor", age_s=300)
    heartbeat(mac.paths, "dev", age_s=200)
    report = await mac.report()
    fails = failures(report)
    assert "supervisor heartbeat: eskirgan (300 s oldin, chegara 60 s)" in fails
    assert "dev heartbeat: eskirgan (200 s oldin, chegara 120 s)" in fails
    assert "personal bot heartbeat: hali yo'q (bot ishga tushmagan)" in texts(report, Level.INFO)


async def test_no_supervisor_heartbeat_is_red_no_bot_heartbeat_is_not(mac: Mac) -> None:
    (mac.paths.run / "supervisor.heartbeat.json").unlink()
    (mac.paths.run / "dev.heartbeat.json").unlink()
    report = await mac.report()
    assert "supervisor heartbeat: fayl yo'q" in failures(report)
    assert "dev bot heartbeat: hali yo'q (bot ishga tushmagan)" in texts(report, Level.INFO)


# --- ids and Telegram ------------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ({"OWNER_ID": "", "DEV_GROUP_ID": "-1001234567890"}, "OWNER_ID: bo'sh"),
        ({"OWNER_ID": "42", "DEV_GROUP_ID": ""}, "DEV_GROUP_ID: bo'sh"),
        ({"OWNER_ID": "42", "DEV_GROUP_ID": "-123456"}, "DEV_GROUP_ID: -123456 supergroup id emas"),
        ({"OWNER_ID": "-5", "DEV_GROUP_ID": "-1001234567890"}, "OWNER_ID: foydalanuvchi id emas"),
    ],
)
def test_owner_and_dev_group_ids(values: dict[str, str], expected: str) -> None:
    lines = check.check_ids(values)
    assert any(line.level is Level.FAIL and line.text.startswith(expected) for line in lines)


def test_good_ids_are_green() -> None:
    lines = check.check_ids({"OWNER_ID": str(OWNER_ID), "DEV_GROUP_ID": str(DEV_GROUP_ID)})
    assert [line.level for line in lines] == [Level.OK, Level.OK]


async def test_privacy_on_join_groups_on_and_a_command_menu_are_red(mac: Mac) -> None:
    api = mac.apis[DEV_BOT_TOKEN]
    api.results["getMe"] = {
        "id": BOT_ID,
        "username": "b",
        "can_join_groups": True,
        "can_read_all_group_messages": False,
    }
    api.results["getMyCommands"] = [{"command": "start", "description": "x"}]
    fails = failures(await mac.report())
    assert "dev bot: privacy yoqilgan (can_read_all_group_messages = false)" in fails
    assert "dev bot: guruhlarga qo'shish yoqilgan (can_join_groups = true)" in fails
    assert "dev bot: buyruqlar menyusi bor" in fails


async def test_the_group_must_be_a_supergroup_where_the_bot_can_delete_and_pin(mac: Mac) -> None:
    api = mac.apis[DEV_BOT_TOKEN]
    api.results["getChat"] = {"id": DEV_GROUP_ID, "type": "group", "title": "old"}
    api.results["getChatMember"] = lambda params: (
        {"status": "administrator", "can_delete_messages": True, "can_pin_messages": False}
        if params["user_id"] == BOT_ID
        else {"status": "member"}
    )
    fails = failures(await mac.report())
    assert "dev guruh: turi group, kerak supergroup" in fails
    assert "dev guruh: bot admin emas yoki o'chirish/qadash huquqi yo'q" in fails
    assert ("getChat", {"chat_id": str(DEV_GROUP_ID)}) in api.calls


async def test_an_owner_posting_as_anonymous_admin_is_red(mac: Mac) -> None:
    api = mac.apis[DEV_BOT_TOKEN]
    api.results["getChatMember"] = lambda params: (
        {"status": "administrator", "can_delete_messages": True, "can_pin_messages": True}
        if params["user_id"] == BOT_ID
        else {"status": "creator", "is_anonymous": True}
    )
    fails = failures(await mac.report())
    assert "dev guruh: siz anonim admin sifatida yozasiz, xabarlaringiz tashlab yuboriladi" in fails


async def test_a_bot_outside_the_group_is_red(mac: Mac) -> None:
    mac.apis[DEV_BOT_TOKEN].results["getChat"] = TelegramError("bad_request", 400)
    assert "dev guruh: getChat: Telegram xatosi (bad_request)" in failures(await mac.report())


async def test_the_personal_bot_is_checked_when_its_token_is_there(mac: Mac) -> None:
    mac.apis[PERSONAL_BOT_TOKEN].results["getMe"] = {"id": 2, "username": "p", "can_join_groups": True}
    fails = failures(await mac.report())
    assert "shaxsiy bot: guruhlarga qo'shish yoqilgan (can_join_groups = true)" in fails
    assert not any("shaxsiy bot: privacy" in text for text in fails)  # privacy matters only for the dev group


async def test_offline_makes_no_bot_api_call(mac: Mac) -> None:
    report = await mac.report(telegram=False)
    assert mac.apis[DEV_BOT_TOKEN].calls == []
    assert mac.apis[PERSONAL_BOT_TOKEN].calls == []
    assert "Telegram tekshiruvlari o'tkazib yuborildi (--offline)" in texts(report, Level.INFO)


async def test_an_empty_dev_token_is_red_and_calls_nothing(mac: Mac) -> None:
    write_env(
        mac.paths.env_file(BotName.DEV), {"DEV_BOT_TOKEN": "", "OWNER_ID": OWNER_ID, "DEV_GROUP_ID": DEV_GROUP_ID}
    )
    report = await mac.report()
    assert "DEV_BOT_TOKEN: bo'sh" in failures(report)
    assert mac.apis[DEV_BOT_TOKEN].calls == []


# --- power -----------------------------------------------------------------------------------------------------------
def test_ac_sleep_is_read_from_the_ac_section_only() -> None:
    assert check.ac_sleep_minutes(PMSET_OK) == 0
    assert check.ac_sleep_minutes("AC Power:\n displaysleep 0\n sleep                1\n") == 1
    assert check.ac_sleep_minutes("Battery Power:\n sleep 0\n") is None


async def test_ac_sleep_not_zero_is_red(mac: Mac) -> None:
    mac.runner.answers["pmset -g"] = RunResult(0, "AC Power:\n sleep                1\n")
    assert "Quvvat: zaryadda Mac 1 daqiqada uxlaydi" in failures(await mac.report())


# --- UrllibBotApi through an in-process handler ----------------------------------------------------------------------
class FakeTelegramHandler(urllib.request.BaseHandler):
    handler_order = 100  # answers before the default HTTPSHandler, which would open a socket

    def __init__(self, status: int, body: bytes, *, error: OSError | None = None) -> None:
        self.status, self.body, self.error = status, body, error
        self.requests: list[urllib.request.Request] = []

    def https_open(self, request: urllib.request.Request) -> urllib.response.addinfourl:
        self.requests.append(request)
        if self.error is not None:
            raise urllib.error.URLError(self.error)
        headers = email.message.Message()
        headers["Content-Type"] = "application/json"
        return FakeResponse(io.BytesIO(self.body), headers, request.full_url, self.status)


class FakeResponse(urllib.response.addinfourl):
    msg = "fake"  # read by urllib's HTTPErrorProcessor, as on a real http.client response


def test_urllib_api_posts_to_the_bot_api_and_returns_the_result() -> None:
    handler = FakeTelegramHandler(200, b'{"ok": true, "result": {"id": 1, "can_join_groups": false}}')
    api = check.UrllibBotApi(DEV_BOT_TOKEN, opener=urllib.request.build_opener(handler))
    assert api.call("getChat", {"chat_id": DEV_GROUP_ID}) == {"id": 1, "can_join_groups": False}
    request = handler.requests[0]
    assert request.full_url == f"https://api.telegram.org/bot{DEV_BOT_TOKEN}/getChat"
    assert request.data == b"chat_id=-1001234567890"
    assert DEV_BOT_TOKEN not in repr(api)


@pytest.mark.parametrize(
    ("status", "body", "problem"),
    [
        (401, b'{"ok": false, "error_code": 401, "description": "Unauthorized"}', "unauthorized"),
        (409, b'{"ok": false, "error_code": 409, "description": "Conflict"}', "conflict"),
        (200, b'{"ok": false, "error_code": 400, "description": "Bad Request: chat not found"}', "bad_request"),
        (200, b"<html>", "bad_response"),
    ],
)
def test_urllib_api_errors_carry_a_code_never_the_token(status: int, body: bytes, problem: str) -> None:
    handler = FakeTelegramHandler(status, body)
    api = check.UrllibBotApi(DEV_BOT_TOKEN, opener=urllib.request.build_opener(handler))
    with pytest.raises(TelegramError) as caught:
        api.call("getMe")
    assert caught.value.problem == problem
    assert DEV_BOT_TOKEN not in str(caught.value)
    assert caught.value.__cause__ is None


def test_urllib_api_network_failure_hides_the_request() -> None:
    handler = FakeTelegramHandler(0, b"", error=ConnectionRefusedError(61, "refused"))
    api = check.UrllibBotApi(DEV_BOT_TOKEN, opener=urllib.request.build_opener(handler))
    with pytest.raises(TelegramError) as caught:
        api.call("getMe")
    assert caught.value.problem == "network"
    assert caught.value.__suppress_context__  # the urllib error (which knows the URL) is never chained into output


# --- env-file editing (shared with scripts/install.py) ---------------------------------------------------------------
def test_update_env_text_fills_only_empty_values_and_keeps_every_other_line() -> None:
    text = "# comment stays\nDEV_BOT_TOKEN=keep-me\nexport OWNER_ID=\nUNKNOWN=x\n"
    new, changed = check.update_env_text(text, {"DEV_BOT_TOKEN": "other", "OWNER_ID": "42", "DEV_GROUP_ID": "-1001"})
    assert new == "# comment stays\nDEV_BOT_TOKEN=keep-me\nOWNER_ID=42\nUNKNOWN=x\nDEV_GROUP_ID=-1001\n"
    assert changed == ("OWNER_ID", "DEV_GROUP_ID")


def test_update_env_text_replaces_only_the_named_keys() -> None:
    new, changed = check.update_env_text("A=1\nB=2\n", {"A": "9", "B": "8"}, replace={"A"})
    assert (new, changed) == ("A=9\nB=2\n", ("A",))


@pytest.mark.parametrize("value", ["a b", 'x"y', "x'y", "x#y", "x\ny"])
def test_update_env_text_refuses_values_the_parser_would_misread(value: str) -> None:
    with pytest.raises(ValueError, match="refusing to write"):
        check.update_env_text("", {"K": value})


def test_write_env_file_is_0600_and_atomic(tmp_path: Path) -> None:
    path = tmp_path / "data" / "dev.env"
    check.write_env_file(path, "A=1\n")
    assert path.read_text() == "A=1\n"
    assert path.stat().st_mode & 0o777 == 0o600
    assert sorted(p.name for p in path.parent.iterdir()) == ["dev.env"]


# --- --discover-ids --------------------------------------------------------------------------------------------------
UPDATES = [
    {
        "update_id": 1,
        "message": {"chat": {"id": OWNER_ID, "type": "private"}, "from": {"id": OWNER_ID, "username": "o"}},
    },
    {
        "update_id": 2,
        "message": {"chat": {"id": DEV_GROUP_ID, "type": "supergroup", "title": "dev"}, "from": {"id": OWNER_ID}},
    },
    {"update_id": 3, "message": {"chat": {"id": -555, "type": "group", "title": "old"}, "migrate_to_chat_id": -1009}},
]


def test_find_candidates_reads_owners_groups_and_migrations() -> None:
    found = check.find_candidates(UPDATES)
    assert found.owners == {OWNER_ID: "o"}
    assert found.groups == {DEV_GROUP_ID: ("supergroup", "dev"), -555: ("group", "old")}
    assert found.migrated == {-555: -1009}


DEV_REF = keychain.DEV_BOT_TOKEN.reference
PERSONAL_REF = keychain.PERSONAL_BOT_TOKEN.reference


async def test_discover_ids_writes_only_the_empty_keys(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    paths.data.mkdir(parents=True)
    write_env(paths.env_file(BotName.DEV), {"DEV_BOT_TOKEN": DEV_REF, "OWNER_ID": "", "DEV_GROUP_ID": ""})
    write_env(paths.env_file(BotName.PERSONAL), {"PERSONAL_BOT_TOKEN": PERSONAL_REF, "OWNER_ID": ""})
    tokens: list[str] = []
    api = FakeBotApi({"getUpdates": UPDATES})
    answers = iter(["ha", str(DEV_GROUP_ID)])
    out = io.StringIO()

    def bot_api(token: str) -> FakeBotApi:
        tokens.append(token)
        return api

    code = await check.discover_ids(paths, bot_api, run=FakeRunner(), ask=lambda _prompt: next(answers), out=out)
    assert code == 0
    assert tokens == [DEV_BOT_TOKEN]  # the token came from the Keychain item the file names
    assert api.calls == [("getUpdates", {"timeout": 0, "limit": 100})]  # no offset: nothing is confirmed
    dev = paths.env_file(BotName.DEV).read_text()
    assert f"OWNER_ID={OWNER_ID}\n" in dev
    assert f"DEV_GROUP_ID={DEV_GROUP_ID}\n" in dev
    assert f"DEV_BOT_TOKEN={DEV_REF}\n" in dev
    assert DEV_BOT_TOKEN not in dev
    assert f"OWNER_ID={OWNER_ID}\n" in paths.env_file(BotName.PERSONAL).read_text()
    assert paths.env_file(BotName.DEV).stat().st_mode & 0o777 == 0o600
    assert DEV_BOT_TOKEN not in out.getvalue()
    assert "-555  group  old  (supergroupga aylangan: yangi id -1009)" in out.getvalue()


async def test_discover_ids_never_replaces_an_existing_id(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    paths.data.mkdir(parents=True)
    original = {"DEV_BOT_TOKEN": DEV_REF, "OWNER_ID": "111", "DEV_GROUP_ID": "-100999999999"}
    write_env(paths.env_file(BotName.DEV), original)
    before = paths.env_file(BotName.DEV).read_text()

    def ask(prompt: str) -> str:
        raise AssertionError(f"nothing to ask, but asked: {prompt}")

    api = FakeBotApi({"getUpdates": UPDATES})
    assert await check.discover_ids(paths, lambda _token: api, run=FakeRunner(), ask=ask, out=io.StringIO()) == 0
    assert paths.env_file(BotName.DEV).read_text() == before


async def test_discover_ids_while_the_bot_polls_is_a_clear_error(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    paths.data.mkdir(parents=True)
    write_env(paths.env_file(BotName.DEV), {"DEV_BOT_TOKEN": DEV_REF, "OWNER_ID": "", "DEV_GROUP_ID": ""})
    api = FakeBotApi({"getUpdates": TelegramError("conflict", 409)})
    out = io.StringIO()
    assert await check.discover_ids(paths, lambda _token: api, run=FakeRunner(), ask=lambda _p: "", out=out) == 1
    assert "launchctl bootout" in out.getvalue()


@pytest.mark.parametrize("plaintext", [True, False], ids=["plaintext_token", "missing_item"])
async def test_discover_ids_uses_only_a_token_from_the_keychain(tmp_path: Path, plaintext: bool) -> None:
    paths = make_paths(tmp_path)
    paths.data.mkdir(parents=True)
    value = DEV_BOT_TOKEN if plaintext else DEV_REF
    write_env(paths.env_file(BotName.DEV), {"DEV_BOT_TOKEN": value, "OWNER_ID": "", "DEV_GROUP_ID": ""})
    api = FakeBotApi({"getUpdates": UPDATES})
    out = io.StringIO()
    runner = FakeRunner(keychain=FakeSecurity())
    assert await check.discover_ids(paths, lambda _token: api, run=runner, ask=lambda _p: "", out=out) == 1
    assert api.calls == []
    assert "--rotate-tokens" in out.getvalue()
    assert DEV_BOT_TOKEN not in out.getvalue()
