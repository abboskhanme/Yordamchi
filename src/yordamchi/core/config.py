"""Bootstrap values, read once at startup — one class per process (CONVENTIONS §3, §12; SPEC §2.2, §2.5 step 2).

The env file named by `YORDAMCHI_ENV_FILE` (default `DATA/<bot>.env`, 0600) holds only the bot token, the owner id
and the chat ids. It is parsed here and **never exported into `os.environ`**, so no child process can inherit the
token. Everything else is a DB setting (`shared/settings`).

Process-environment overrides for the roots (tests, the supervisor's `make check`): `YORDAMCHI_DATA_DIR`,
`YORDAMCHI_CONFIG_DIR`, `YORDAMCHI_LOG_DIR`.
"""

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar, Final

from yordamchi.core import redact
from yordamchi.core.errors import ConfigInvalid
from yordamchi.core.paths import BotName, Paths

ENV_FILE_VAR: Final = "YORDAMCHI_ENV_FILE"
DATA_DIR_VAR: Final = "YORDAMCHI_DATA_DIR"
CONFIG_DIR_VAR: Final = "YORDAMCHI_CONFIG_DIR"
LOG_DIR_VAR: Final = "YORDAMCHI_LOG_DIR"

DEV_KEYS: Final = ("DEV_BOT_TOKEN", "OWNER_ID", "DEV_GROUP_ID")
PERSONAL_KEYS: Final = ("PERSONAL_BOT_TOKEN", "OWNER_ID", "PERSONAL_GROUP_ID")

_KEY = re.compile(r"[A-Z_][A-Z0-9_]*")
_BOT_TOKEN = re.compile(r"\d{8,10}:[A-Za-z0-9_-]{35}")


def load_paths(environ: Mapping[str, str] | None = None, *, home: Path | None = None) -> Paths:
    env = os.environ if environ is None else environ

    def override(var: str) -> Path | None:
        value = env.get(var)
        return Path(value).expanduser() if value else None

    return Paths.defaults(
        home or Path.home(), data=override(DATA_DIR_VAR), config=override(CONFIG_DIR_VAR), logs=override(LOG_DIR_VAR)
    )


def parse_env_file(text: str) -> dict[str, str]:
    """`KEY=VALUE` lines. Blank lines and `#` comments are skipped, `export ` is allowed, and a value may be wrapped
    in matching quotes. There is no escaping and no `$VAR` expansion."""
    values: dict[str, str] = {}
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        line = line.removeprefix("export ").strip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not _KEY.fullmatch(key):
            raise ConfigInvalid(key=f"line {number}", problem="malformed")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key in values:
            raise ConfigInvalid(key=key, problem="duplicate")
        values[key] = value
    return values


def read_env_file(path: Path) -> dict[str, str]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ConfigInvalid(key=ENV_FILE_VAR, problem="file_missing") from None
    except OSError:
        raise ConfigInvalid(key=ENV_FILE_VAR, problem="file_unreadable") from None
    return parse_env_file(text)


def _required(values: Mapping[str, str], key: str) -> str:
    value = values.get(key, "")
    if not value:
        raise ConfigInvalid(key=key, problem="missing")
    return value


def _token(values: Mapping[str, str], key: str) -> str:
    token = _required(values, key)
    if not _BOT_TOKEN.fullmatch(token):
        raise ConfigInvalid(key=key, problem="not_a_bot_token")
    return token


def _int(values: Mapping[str, str], key: str) -> int:
    raw = _required(values, key)
    try:
        return int(raw)
    except ValueError:
        raise ConfigInvalid(key=key, problem="not_an_integer") from None


def _owner_id(values: Mapping[str, str]) -> int:
    owner_id = _int(values, "OWNER_ID")
    if owner_id <= 0:
        raise ConfigInvalid(key="OWNER_ID", problem="not_a_user_id")
    return owner_id


def _group_id(values: Mapping[str, str], key: str) -> int:
    group_id = _int(values, key)
    if group_id >= 0:  # Telegram group and supergroup ids are negative; a positive one is a user id pasted by mistake.
        raise ConfigInvalid(key=key, problem="not_a_group_id")
    return group_id


def _ignored(values: Mapping[str, str], known: tuple[str, ...]) -> tuple[str, ...]:
    # Unknown keys are reported, not fatal: a rolled-back release must still start with a newer env file.
    return tuple(sorted(set(values) - set(known)))


@dataclass(frozen=True, slots=True)
class DevConfig:
    bot: ClassVar[BotName] = BotName.DEV
    bot_token: str = field(repr=False)
    owner_id: int
    dev_group_id: int
    paths: Paths
    ignored_keys: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PersonalConfig:
    bot: ClassVar[BotName] = BotName.PERSONAL
    bot_token: str = field(repr=False)
    owner_id: int
    personal_group_id: int | None
    paths: Paths
    ignored_keys: tuple[str, ...] = ()


def dev_config_from(values: Mapping[str, str], paths: Paths) -> DevConfig:
    return DevConfig(
        bot_token=_token(values, "DEV_BOT_TOKEN"),
        owner_id=_owner_id(values),
        dev_group_id=_group_id(values, "DEV_GROUP_ID"),
        paths=paths,
        ignored_keys=_ignored(values, DEV_KEYS),
    )


def personal_config_from(values: Mapping[str, str], paths: Paths) -> PersonalConfig:
    return PersonalConfig(
        bot_token=_token(values, "PERSONAL_BOT_TOKEN"),
        owner_id=_owner_id(values),
        personal_group_id=_group_id(values, "PERSONAL_GROUP_ID") if values.get("PERSONAL_GROUP_ID") else None,
        paths=paths,
        ignored_keys=_ignored(values, PERSONAL_KEYS),
    )


def _env_file(env: Mapping[str, str], paths: Paths, bot: BotName) -> Path:
    named = env.get(ENV_FILE_VAR)
    return Path(named).expanduser() if named else paths.env_file(bot)


def load_dev_config(environ: Mapping[str, str] | None = None, *, home: Path | None = None) -> DevConfig:
    """Read the dev env file. The token joins the redaction set at once, before anything can log it."""
    env = os.environ if environ is None else environ
    paths = load_paths(env, home=home)
    config = dev_config_from(read_env_file(_env_file(env, paths, BotName.DEV)), paths)
    redact.add_known_secrets([config.bot_token])
    return config


def load_personal_config(environ: Mapping[str, str] | None = None, *, home: Path | None = None) -> PersonalConfig:
    env = os.environ if environ is None else environ
    paths = load_paths(env, home=home)
    config = personal_config_from(read_env_file(_env_file(env, paths, BotName.PERSONAL)), paths)
    redact.add_known_secrets([config.bot_token])
    return config
