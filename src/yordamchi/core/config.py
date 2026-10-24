"""Bootstrap values, read once at startup — one class per process (CONVENTIONS §3, §12; SPEC §2.2, §2.5 step 2).

The env file named by `YORDAMCHI_ENV_FILE` (default `DATA/<bot>.env`, 0600) holds the owner id, the chat ids and a
**reference** to the bot token, never the token itself (owner requirement 2026-10-24, `core.keychain`):
`DEV_BOT_TOKEN=@keychain:uz.yordamchi/dev-bot-token`. At load time:
- a plaintext secret anywhere in the file (a `*_TOKEN`/`*_SECRET`/`*_PASSWORD`/`*_API_KEY` value that is not a
  reference, or any value `core.redact` recognises as a key) is refused: `SecretInPlaintext`, whose Uzbek message
  tells the owner to run `scripts/install.py --env-only --rotate-tokens`;
- the token reference is resolved through `/usr/bin/security`, scoped to this bot (`core.keychain.Keychain`): the
  personal process can never resolve `dev-bot-token` (UC-36). `keychain_runner` replaces `/usr/bin/security` for
  tests only; production passes nothing.
Values are parsed here and **never exported into `os.environ`**, so no child process can inherit the token.
Everything else is a DB setting (`shared/settings`).

Process-environment overrides for the roots (tests, the supervisor's `make check`): `YORDAMCHI_DATA_DIR`,
`YORDAMCHI_CONFIG_DIR`, `YORDAMCHI_LOG_DIR`.
"""

import os
import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar, Final

from yordamchi.core import keychain, redact
from yordamchi.core.errors import ConfigInvalid, SecretInPlaintext
from yordamchi.core.paths import SECRET_HASHES_FILE, BotName, Paths

ENV_FILE_VAR: Final = "YORDAMCHI_ENV_FILE"
DATA_DIR_VAR: Final = "YORDAMCHI_DATA_DIR"
CONFIG_DIR_VAR: Final = "YORDAMCHI_CONFIG_DIR"
LOG_DIR_VAR: Final = "YORDAMCHI_LOG_DIR"
# Passed into executor sessions (SPEC §5.3) for the shims and git hooks; the gate forbids the agent to touch them.
SOCK_VAR: Final = "YORDAMCHI_SOCK"
SESSION_TOKEN_VAR: Final = "YORDAMCHI_SESSION_TOKEN"  # noqa: S105  # a variable name, not a token
# Set by Claude Code for a `headersHelper` (code.claude.com/docs/en/mcp): which server the helper is asked about.
MCP_SERVER_NAME_VAR: Final = "CLAUDE_CODE_MCP_SERVER_NAME"

DEV_KEYS: Final = ("DEV_BOT_TOKEN", "OWNER_ID", "DEV_GROUP_ID")
PERSONAL_KEYS: Final = ("PERSONAL_BOT_TOKEN", "OWNER_ID", "PERSONAL_GROUP_ID")
# The keys whose value is an `@keychain:` reference that the loader resolves.
DEV_SECRET_KEYS: Final = ("DEV_BOT_TOKEN",)
PERSONAL_SECRET_KEYS: Final = ("PERSONAL_BOT_TOKEN",)

_KEY = re.compile(r"[A-Z_][A-Z0-9_]*")
_BOT_TOKEN = re.compile(r"\d{8,10}:[A-Za-z0-9_-]{35}")
_SECRET_NAME = re.compile(r"(?:TOKEN|SECRET|PASSWORD|PASSWD|API_KEY|ACCESS_KEY|PRIVATE_KEY)$")


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


def plaintext_secrets(values: Mapping[str, str]) -> tuple[str, ...]:
    """The keys (sorted, never their values) that hold a secret in the clear: a non-empty value under a secret name
    (`*_TOKEN`, `*_SECRET`, `*_PASSWORD`, `*_API_KEY`, …) that is not an `@keychain:` reference, or any value in which
    `core.redact` finds a key. The check script reports the same list."""
    found = {
        key
        for key, value in values.items()
        if value
        and not keychain.is_reference(value)
        and (_SECRET_NAME.search(key) is not None or bool(redact.find_secrets(f"{key}={value}")))
    }
    return tuple(sorted(found))


def resolve_secrets(
    values: Mapping[str, str], *, source: str, keys: Collection[str], chain: keychain.Keychain
) -> dict[str, str]:
    """Refuse a plaintext secret anywhere in `values` (`SecretInPlaintext`), then replace each `@keychain:` reference
    under `keys` with the item's value. `chain` is scoped to one bot, so a reference to another bot's item raises
    `KeychainItemForbidden` before `security` runs, and a token key naming this bot's other item (the master key) is
    `ConfigInvalid`; a missing item or a locked Keychain raises `KeychainItemMissing` / `KeychainUnavailable`.
    Unknown keys are never resolved."""
    plaintext = plaintext_secrets(values)
    if plaintext:
        raise SecretInPlaintext(source=source, keys=", ".join(plaintext))
    resolved = dict(values)
    for key in keys:
        value = values.get(key, "")
        if not keychain.is_reference(value):
            continue
        try:
            item = keychain.parse_reference(value)
        except ValueError:
            raise ConfigInvalid(key=key, problem="bad_keychain_reference") from None
        if chain.allows(item) and item != keychain.TOKEN_ITEMS.get(key, item):
            # This bot's other item (the master key) is never read as a token.
            raise ConfigInvalid(key=key, problem="wrong_keychain_item")
        resolved[key] = chain.read(item)  # an item outside this bot's scope raises KeychainItemForbidden here
    return resolved


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


def _load_values(
    env: Mapping[str, str],
    paths: Paths,
    bot: BotName,
    keys: Collection[str],
    runner: keychain.Runner | None,
) -> dict[str, str]:
    path = _env_file(env, paths, bot)
    chain = keychain.Keychain(bot, runner=runner)  # the scope comes from the process, never from the file
    return resolve_secrets(read_env_file(path), source=path.name, keys=keys, chain=chain)


def load_dev_config(
    environ: Mapping[str, str] | None = None,
    *,
    home: Path | None = None,
    keychain_runner: keychain.Runner | None = None,
) -> DevConfig:
    """Read the dev env file and resolve its token from the Keychain. The token joins the redaction set at once,
    before anything can log it. `keychain_runner`: tests only (a fake `/usr/bin/security`)."""
    env = os.environ if environ is None else environ
    paths = load_paths(env, home=home)
    config = dev_config_from(_load_values(env, paths, BotName.DEV, DEV_SECRET_KEYS, keychain_runner), paths)
    redact.add_known_secrets([config.bot_token])
    return config


@dataclass(frozen=True, slots=True)
class CliConfig:
    """What a gate shim or the pre-push hook (`bots/cli`) reads from the executor session's environment (SPEC §5.3):
    the dev bot's control socket, this session's token and `PATH`. The secret-hash file lives next to the socket.
    A missing value stays empty; the CLI then refuses (fail closed)."""

    socket: Path | None
    session_token: str = field(repr=False)
    search_path: str

    @property
    def secret_hashes(self) -> Path | None:
        return self.socket.parent / SECRET_HASHES_FILE if self.socket is not None else None


def load_cli_config(environ: Mapping[str, str] | None = None) -> CliConfig:
    env = os.environ if environ is None else environ
    socket = env.get(SOCK_VAR, "")
    return CliConfig(
        socket=Path(socket) if socket else None,
        session_token=env.get(SESSION_TOKEN_VAR, ""),
        search_path=env.get("PATH", ""),
    )


def load_mcp_server_name(environ: Mapping[str, str] | None = None) -> str:
    """The MCP server a `headersHelper` run is for (T50); empty when unset, and the helper then refuses."""
    env = os.environ if environ is None else environ
    return env.get(MCP_SERVER_NAME_VAR, "")


def load_personal_config(
    environ: Mapping[str, str] | None = None,
    *,
    home: Path | None = None,
    keychain_runner: keychain.Runner | None = None,
) -> PersonalConfig:
    """`load_dev_config` for the personal bot; its Keychain scope never includes the dev token (UC-36)."""
    env = os.environ if environ is None else environ
    paths = load_paths(env, home=home)
    values = _load_values(env, paths, BotName.PERSONAL, PERSONAL_SECRET_KEYS, keychain_runner)
    config = personal_config_from(values, paths)
    redact.add_known_secrets([config.bot_token])
    return config
