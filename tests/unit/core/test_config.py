"""core/config: env-file bootstrap (SPEC §2.2, §2.5 step 2; CONVENTIONS §12)."""

from pathlib import Path

import pytest

from tests.factories import DEV_BOT_TOKEN, DEV_GROUP_ID, OWNER_ID, PERSONAL_BOT_TOKEN, make_paths
from yordamchi.core import config, redact
from yordamchi.core.errors import ConfigInvalid
from yordamchi.core.paths import BotName

DEV_ENV = f"""
# dev bot
DEV_BOT_TOKEN={DEV_BOT_TOKEN}
export OWNER_ID="{OWNER_ID}"
DEV_GROUP_ID='{DEV_GROUP_ID}'
FUTURE_KEY=ignored
"""


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
    env_file = tmp_path / "dev.env"
    env_file.write_text(DEV_ENV, encoding="utf-8")
    environ = {config.ENV_FILE_VAR: str(env_file), config.DATA_DIR_VAR: str(tmp_path / "data")}
    cfg = config.load_dev_config(environ, home=tmp_path)
    assert cfg.paths.data == tmp_path / "data"
    assert DEV_BOT_TOKEN not in redact.scrubbed(f"token={DEV_BOT_TOKEN}")
    assert "DEV_BOT_TOKEN" not in environ  # parsed, never exported


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
