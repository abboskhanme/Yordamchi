"""core/config: env-file bootstrap (SPEC §2.2, §2.5 step 2; CONVENTIONS §12). Tokens come from the Keychain through a
`FakeSecurity`; a plaintext secret in an env file is refused (owner requirement 2026-10-24)."""

from pathlib import Path

import pytest

from tests.factories import DEV_BOT_TOKEN, DEV_GROUP_ID, OWNER_ID, PERSONAL_BOT_TOKEN, make_paths
from tests.fakes.security import FakeSecurity
from yordamchi.core import config, keychain, redact
from yordamchi.core import strings as S
from yordamchi.core.errors import (
    ConfigInvalid,
    KeychainItemForbidden,
    KeychainItemMissing,
    KeychainUnavailable,
    SecretInPlaintext,
)
from yordamchi.core.paths import BotName

DEV_ENV = f"""
# dev bot
DEV_BOT_TOKEN={DEV_BOT_TOKEN}
export OWNER_ID="{OWNER_ID}"
DEV_GROUP_ID='{DEV_GROUP_ID}'
FUTURE_KEY=ignored
"""
DEV_ENV_REF = f"""
# dev bot
DEV_BOT_TOKEN={keychain.DEV_BOT_TOKEN.reference}
OWNER_ID={OWNER_ID}
DEV_GROUP_ID={DEV_GROUP_ID}
"""
PERSONAL_ENV_REF = f"PERSONAL_BOT_TOKEN={keychain.PERSONAL_BOT_TOKEN.reference}\nOWNER_ID={OWNER_ID}\n"


def keychain_with_tokens() -> FakeSecurity:
    security = FakeSecurity()
    security.put(keychain.DEV_BOT_TOKEN, DEV_BOT_TOKEN)
    security.put(keychain.PERSONAL_BOT_TOKEN, PERSONAL_BOT_TOKEN)
    return security


def env_at(tmp_path: Path, name: str, text: str) -> dict[str, str]:
    env_file = tmp_path / name
    env_file.write_text(text, encoding="utf-8")
    return {config.ENV_FILE_VAR: str(env_file), config.DATA_DIR_VAR: str(tmp_path / "data")}


def test_parse_handles_comments_export_and_quotes() -> None:
    values = config.parse_env_file(DEV_ENV)
    assert values == {
        "DEV_BOT_TOKEN": DEV_BOT_TOKEN,
        "OWNER_ID": str(OWNER_ID),
        "DEV_GROUP_ID": str(DEV_GROUP_ID),
        "FUTURE_KEY": "ignored",
    }


@pytest.mark.parametrize(("text", "problem"), [("JUST TEXT", "malformed"), ("A=1\nA=2", "duplicate")])
def test_parse_rejects_bad_lines(text: str, problem: str) -> None:
    with pytest.raises(ConfigInvalid) as caught:
        config.parse_env_file(text)
    assert caught.value.params["problem"] == problem


def test_dev_config_reads_ids_and_reports_unknown_keys(tmp_path: Path) -> None:
    cfg = config.dev_config_from(config.parse_env_file(DEV_ENV), make_paths(tmp_path))
    assert (cfg.owner_id, cfg.dev_group_id, cfg.ignored_keys) == (OWNER_ID, DEV_GROUP_ID, ("FUTURE_KEY",))
    assert cfg.bot is BotName.DEV
    assert DEV_BOT_TOKEN not in repr(cfg)


@pytest.mark.parametrize(
    ("change", "key", "problem"),
    [
        ({"DEV_BOT_TOKEN": ""}, "DEV_BOT_TOKEN", "missing"),
        ({"DEV_BOT_TOKEN": "not-a-token"}, "DEV_BOT_TOKEN", "not_a_bot_token"),
        ({"OWNER_ID": "abc"}, "OWNER_ID", "not_an_integer"),
        ({"OWNER_ID": "-5"}, "OWNER_ID", "not_a_user_id"),
        ({"DEV_GROUP_ID": "12345"}, "DEV_GROUP_ID", "not_a_group_id"),
    ],
)
def test_dev_config_refuses_bad_values(tmp_path: Path, change: dict[str, str], key: str, problem: str) -> None:
    values = {**config.parse_env_file(DEV_ENV), **change}
    with pytest.raises(ConfigInvalid) as caught:
        config.dev_config_from(values, make_paths(tmp_path))
    assert caught.value.params == {"key": key, "problem": problem}


def test_personal_group_is_optional(tmp_path: Path) -> None:
    values = {"PERSONAL_BOT_TOKEN": PERSONAL_BOT_TOKEN, "OWNER_ID": str(OWNER_ID), "PERSONAL_GROUP_ID": ""}
    assert config.personal_config_from(values, make_paths(tmp_path)).personal_group_id is None


def test_load_reads_the_named_file_and_masks_the_token_at_once(tmp_path: Path) -> None:
    environ = env_at(tmp_path, "dev.env", DEV_ENV_REF)
    security = keychain_with_tokens()
    cfg = config.load_dev_config(environ, home=tmp_path, keychain_runner=security)
    assert cfg.paths.data == tmp_path / "data"
    assert cfg.bot_token == DEV_BOT_TOKEN  # resolved from the Keychain item the file names
    assert DEV_BOT_TOKEN not in redact.scrubbed(f"token={DEV_BOT_TOKEN}")
    assert "DEV_BOT_TOKEN" not in environ  # parsed, never exported
    assert security.calls == [tuple(keychain.find_argv(keychain.DEV_BOT_TOKEN, reveal=True))]


def test_the_personal_config_resolves_its_own_token(tmp_path: Path) -> None:
    environ = env_at(tmp_path, "personal.env", PERSONAL_ENV_REF)
    cfg = config.load_personal_config(environ, home=tmp_path, keychain_runner=keychain_with_tokens())
    assert (cfg.bot_token, cfg.owner_id, cfg.personal_group_id) == (PERSONAL_BOT_TOKEN, OWNER_ID, None)


def test_a_plaintext_token_is_refused_at_startup_with_the_rotation_hint(tmp_path: Path) -> None:
    security = keychain_with_tokens()
    with pytest.raises(SecretInPlaintext) as caught:
        config.load_dev_config(env_at(tmp_path, "dev.env", DEV_ENV), home=tmp_path, keychain_runner=security)
    assert caught.value.params == {"key": "DEV_BOT_TOKEN", "problem": "plaintext_in_dev.env"}
    message = S.ERRORS[caught.value.code](**caught.value.params)
    assert "--rotate-tokens" in message
    assert "Keychain" in message
    assert DEV_BOT_TOKEN not in message
    assert security.calls == []  # refused before anything is resolved


@pytest.mark.parametrize(
    ("line", "key"),
    [
        ("GOOGLE_API_KEY=AIza" + "SyntheticKeyForOfflineTests_0123456", "GOOGLE_API_KEY"),
        ("NOTES=sk-or-v1-" + "0123456789abcdef" * 4, "NOTES"),
        ("OLD_DEV_BOT_TOKEN=whatever-it-is", "OLD_DEV_BOT_TOKEN"),
        ("SMTP_PASSWORD=hunter2", "SMTP_PASSWORD"),
    ],
)
def test_any_plaintext_secret_in_the_file_is_refused_even_under_an_unknown_key(
    tmp_path: Path, line: str, key: str
) -> None:
    environ = env_at(tmp_path, "dev.env", DEV_ENV_REF + line + "\n")
    with pytest.raises(SecretInPlaintext) as caught:
        config.load_dev_config(environ, home=tmp_path, keychain_runner=keychain_with_tokens())
    assert caught.value.params["key"] == key


def test_plaintext_secrets_names_keys_never_values() -> None:
    values = {
        "DEV_BOT_TOKEN": keychain.DEV_BOT_TOKEN.reference,
        "OWNER_ID": str(OWNER_ID),
        "DEV_GROUP_ID": str(DEV_GROUP_ID),
        "PERSONAL_GROUP_ID": "",
        "FUTURE_KEY": "ignored",  # a key name, not a secret name
        "B_TOKEN": "x",
        "A_API_KEY": "y",
    }
    assert config.plaintext_secrets(values) == ("A_API_KEY", "B_TOKEN")
    assert config.plaintext_secrets({"DEV_BOT_TOKEN": ""}) == ()


def test_the_personal_process_can_never_resolve_the_dev_token(tmp_path: Path) -> None:
    """UC-36: a personal.env pointing at the dev token is refused before `security` is asked."""
    text = f"PERSONAL_BOT_TOKEN={keychain.DEV_BOT_TOKEN.reference}\nOWNER_ID={OWNER_ID}\n"
    security = keychain_with_tokens()
    with pytest.raises(KeychainItemForbidden) as caught:
        config.load_personal_config(env_at(tmp_path, "personal.env", text), home=tmp_path, keychain_runner=security)
    assert caught.value.params == {"key": "uz.yordamchi/dev-bot-token", "problem": "not_for_the_personal_bot"}
    assert security.calls == []


def test_the_dev_process_cannot_resolve_the_personal_token_or_another_service(tmp_path: Path) -> None:
    for reference in (keychain.PERSONAL_BOT_TOKEN.reference, "@keychain:com.example/dev-bot-token"):
        text = f"DEV_BOT_TOKEN={reference}\nOWNER_ID={OWNER_ID}\nDEV_GROUP_ID={DEV_GROUP_ID}\n"
        security = keychain_with_tokens()
        with pytest.raises(KeychainItemForbidden):
            config.load_dev_config(env_at(tmp_path, "dev.env", text), home=tmp_path, keychain_runner=security)
        assert security.calls == []


def test_a_missing_item_or_a_locked_keychain_stops_the_start(tmp_path: Path) -> None:
    environ = env_at(tmp_path, "dev.env", DEV_ENV_REF)
    with pytest.raises(KeychainItemMissing) as missing:
        config.load_dev_config(environ, home=tmp_path, keychain_runner=FakeSecurity())
    assert "--rotate-tokens" in S.ERRORS[missing.value.code](**missing.value.params)
    locked = keychain_with_tokens()
    locked.locked = True
    with pytest.raises(KeychainUnavailable) as unavailable:
        config.load_dev_config(environ, home=tmp_path, keychain_runner=locked)
    assert "login" in S.ERRORS[unavailable.value.code](**unavailable.value.params)


def test_a_token_key_naming_the_master_key_is_never_resolved(tmp_path: Path) -> None:
    text = f"DEV_BOT_TOKEN={keychain.MASTER_KEY.reference}\nOWNER_ID={OWNER_ID}\nDEV_GROUP_ID={DEV_GROUP_ID}\n"
    security = keychain_with_tokens()
    security.put(keychain.MASTER_KEY, "a2V5" * 11)
    with pytest.raises(ConfigInvalid) as caught:
        config.load_dev_config(env_at(tmp_path, "dev.env", text), home=tmp_path, keychain_runner=security)
    assert caught.value.params == {"key": "DEV_BOT_TOKEN", "problem": "wrong_keychain_item"}
    assert security.calls == []


def test_a_malformed_reference_is_a_config_error(tmp_path: Path) -> None:
    text = f"DEV_BOT_TOKEN=@keychain:uz.yordamchi\nOWNER_ID={OWNER_ID}\nDEV_GROUP_ID={DEV_GROUP_ID}\n"
    with pytest.raises(ConfigInvalid) as caught:
        config.load_dev_config(env_at(tmp_path, "dev.env", text), home=tmp_path, keychain_runner=FakeSecurity())
    assert caught.value.params == {"key": "DEV_BOT_TOKEN", "problem": "bad_keychain_reference"}


def test_a_reference_under_an_unknown_key_is_never_resolved(tmp_path: Path) -> None:
    environ = env_at(tmp_path, "dev.env", DEV_ENV_REF + f"FUTURE_TOKEN={keychain.PERSONAL_BOT_TOKEN.reference}\n")
    security = keychain_with_tokens()
    cfg = config.load_dev_config(environ, home=tmp_path, keychain_runner=security)
    assert cfg.ignored_keys == ("FUTURE_TOKEN",)
    assert len(security.calls) == 1  # the dev token only


@pytest.mark.parametrize(
    "error",
    [
        SecretInPlaintext(source="dev.env", keys="DEV_BOT_TOKEN"),
        KeychainItemMissing(item="uz.yordamchi/dev-bot-token"),
        KeychainUnavailable(item="uz.yordamchi/dev-bot-token"),
        KeychainItemForbidden(item="uz.yordamchi/dev-bot-token", bot="personal"),
    ],
    ids=lambda error: type(error).__name__,
)
def test_every_keychain_refusal_is_a_config_error_with_its_own_uzbek_message(error: ConfigInvalid) -> None:
    """A startup that refuses on `ConfigInvalid` (bots/dev/app.py writes `key: problem` to run/dev.lasterror) refuses
    on these too, and the owner's message for each says what to do."""
    assert isinstance(error, ConfigInvalid)
    assert error.code is not ConfigInvalid.code_of
    assert set(error.params) == {"key", "problem"}
    message = S.ERRORS[error.code](**error.params)
    assert "Keychain" in message
    assert any(fix in message for fix in ("--rotate-tokens", "login", "havolani"))


def test_a_missing_env_file_is_a_clear_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigInvalid) as caught:
        config.load_dev_config({config.DATA_DIR_VAR: str(tmp_path)}, home=tmp_path)
    assert caught.value.params == {"key": config.ENV_FILE_VAR, "problem": "file_missing"}


def test_load_paths_defaults_and_overrides(tmp_path: Path) -> None:
    defaults = config.load_paths({}, home=tmp_path)
    assert defaults.data == tmp_path / "Library" / "Application Support" / "Yordamchi"
    assert defaults.master_key == tmp_path / ".config" / "yordamchi" / "master.key"
    moved = config.load_paths({config.CONFIG_DIR_VAR: str(tmp_path / "k")}, home=tmp_path)
    assert moved.master_key == tmp_path / "k" / "master.key"
