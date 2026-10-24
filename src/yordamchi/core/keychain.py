"""Secrets in the macOS login Keychain, through Apple-signed `/usr/bin/security` (owner requirement 2026-10-24).

No file Yordamchi writes holds a usable secret. The bot tokens and the vault master key are generic passwords of the
service `uz.yordamchi` in the owner's login Keychain, which is encrypted with the login password and is not part of a
plain file copy or of Time Machine. An env file only names an item:
`DEV_BOT_TOKEN=@keychain:uz.yordamchi/dev-bot-token`.

- **Read**: `security find-generic-password -s <service> -a <account> -w`. argv carries the item name only; the value
  comes back on stdout. It runs through `core.proc.run_sync` (argv list, own process group, timeout). The installer
  creates every item with `-T /usr/bin/security`, so a launchd job reads it without a prompt, and an update of the
  ad-hoc-signed uv Python changes nothing (the reason STACK §2.2 once chose a 0600 file no longer holds).
- **Write**: `security -i` with the command on **stdin** (`add_script`), so the value is never on argv or in `ps`.
  Values are restricted to a quote-free character set (bot tokens, base64 keys, API keys), so stdin parsing is exact.
- **Scope (UC-36)**: a bot reads and writes through `Keychain(bot)`, which refuses any item outside `ALLOWED[bot]`
  before `security` runs. The personal process can never read `dev-bot-token`, and no process can name another
  service (`Claude Code-credentials` is never Yordamchi's to read).
- **Tests** never touch the real Keychain: they pass a fake `Runner` (the test-only hook). The one exception is the
  opt-in `live` test, on a throwaway item it deletes afterwards.

Every value read is added to `core.redact`'s known set at once. Nothing here logs or prints a value.
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Protocol

from yordamchi.core import proc, redact
from yordamchi.core.errors import KeychainItemForbidden, KeychainItemMissing, KeychainUnavailable
from yordamchi.core.paths import BotName

SECURITY: Final = "/usr/bin/security"
SERVICE: Final = "uz.yordamchi"
REFERENCE_PREFIX: Final = "@keychain:"
TIMEOUT_S: Final = 5.0
# `security` exits 44 (errSecItemNotFound) for a missing item; any other failure (a locked Keychain refusing
# interaction, a crash) is reported as unavailable.
ITEM_NOT_FOUND_EXIT: Final = 44

_PART: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
# Bot tokens (digits:base64url), base64 master keys and the API key formats all fit; quotes, backslashes and
# whitespace never do, so `security -i` reads the value back byte for byte.
_VALUE: Final = re.compile(r"[A-Za-z0-9._~+/=:-]{1,4096}")


@dataclass(frozen=True, slots=True)
class KeychainItem:
    """One generic password: `-s service -a account`."""

    service: str
    account: str

    def __post_init__(self) -> None:
        if not (_PART.fullmatch(self.service) and _PART.fullmatch(self.account)):
            msg = "a Keychain service and account are 1-64 characters of [A-Za-z0-9._-]"
            raise ValueError(msg)

    def __str__(self) -> str:
        return f"{self.service}/{self.account}"

    @property
    def reference(self) -> str:
        """What an env file holds instead of the value."""
        return f"{REFERENCE_PREFIX}{self}"


DEV_BOT_TOKEN: Final = KeychainItem(SERVICE, "dev-bot-token")
PERSONAL_BOT_TOKEN: Final = KeychainItem(SERVICE, "personal-bot-token")
MASTER_KEY: Final = KeychainItem(SERVICE, "vault-master-key")
# Read only by the opt-in live tests (tests/integration/stt_harness.live_key), never by a bot.
GOOGLE_API_KEY: Final = KeychainItem(SERVICE, "google-api-key")
OPENROUTER_API_KEY: Final = KeychainItem(SERVICE, "openrouter-api-key")

# The env key that names each bot token, and the item the installer stores it in.
TOKEN_ITEMS: Final[Mapping[str, KeychainItem]] = {
    "DEV_BOT_TOKEN": DEV_BOT_TOKEN,
    "PERSONAL_BOT_TOKEN": PERSONAL_BOT_TOKEN,
}

# UC-36: what each bot process may read or write. Both bots open their own vault with the one master key; each
# holds only its own token.
ALLOWED: Final[Mapping[BotName, frozenset[KeychainItem]]] = {
    BotName.DEV: frozenset({DEV_BOT_TOKEN, MASTER_KEY}),
    BotName.PERSONAL: frozenset({PERSONAL_BOT_TOKEN, MASTER_KEY}),
}


# --- references ----------------------------------------------------------------------------------------------------
def is_reference(value: str) -> bool:
    return value.startswith(REFERENCE_PREFIX)


def parse_reference(value: str) -> KeychainItem:
    """`@keychain:<service>/<account>` → the item. Raises `ValueError` for anything else."""
    if not is_reference(value):
        msg = "not an @keychain: reference"
        raise ValueError(msg)
    service, sep, account = value.removeprefix(REFERENCE_PREFIX).partition("/")
    if not sep:
        msg = "an @keychain: reference is <service>/<account>"
        raise ValueError(msg)
    return KeychainItem(service, account)


# --- the `security` command lines (pure) ---------------------------------------------------------------------------
def find_argv(item: KeychainItem, *, reveal: bool) -> list[str]:
    """`reveal=True` prints the value (`-w`); without it the exit code alone says whether the item exists."""
    return [SECURITY, "find-generic-password", "-s", item.service, "-a", item.account, *(["-w"] if reveal else [])]


def delete_argv(item: KeychainItem) -> list[str]:
    return [SECURITY, "delete-generic-password", "-s", item.service, "-a", item.account]


def add_script(item: KeychainItem, value: str) -> bytes:
    """The `security -i` stdin that creates or updates (`-U`) the item, readable by `/usr/bin/security` without a
    prompt (`-T`). Raises `ValueError` (without the value) when the value has a character stdin could misread."""
    if not _VALUE.fullmatch(value):
        msg = "refusing to store a value with whitespace, quotes or other characters outside [A-Za-z0-9._~+/=:-]"
        raise ValueError(msg)
    line = f'add-generic-password -U -s "{item.service}" -a "{item.account}" -T {SECURITY} -w "{value}"\n'
    return line.encode("ascii")


def write_argv() -> list[str]:
    return [SECURITY, "-i"]


def password_from(result: proc.ProcResult, item: KeychainItem) -> str:
    """The value printed by `find_argv(item, reveal=True)`. Raises `KeychainItemMissing` or `KeychainUnavailable`."""
    if result.returncode == ITEM_NOT_FOUND_EXIT:
        raise KeychainItemMissing(item=str(item))
    if result.returncode != 0:
        raise KeychainUnavailable(item=str(item))
    try:
        value = result.stdout.decode("utf-8").removesuffix("\n")
    except UnicodeDecodeError:
        raise KeychainUnavailable(item=str(item)) from None
    if not value:
        raise KeychainItemMissing(item=str(item))
    return value


# --- running it ----------------------------------------------------------------------------------------------------
class Runner(Protocol):
    """Runs one `security` command. Production: `security_runner`; tests pass a fake (never the real Keychain)."""

    def __call__(self, argv: Sequence[str], *, stdin: bytes | None, timeout: float) -> proc.ProcResult: ...


def security_runner(argv: Sequence[str], *, stdin: bytes | None, timeout: float) -> proc.ProcResult:
    env = proc.clean_env(path=":".join(proc.SYSTEM_PATH))
    return proc.run_sync(argv, timeout=timeout, stdin=stdin, env=env)


class Keychain:
    """The Keychain items one bot process may use (`ALLOWED[bot]`, UC-36). Synchronous: one short subprocess."""

    def __init__(self, bot: BotName, *, runner: Runner | None = None, timeout: float = TIMEOUT_S) -> None:
        self._bot = bot
        self._run: Runner = runner or security_runner
        self._timeout = timeout

    def __repr__(self) -> str:
        return f"Keychain(bot={self._bot.value})"

    @property
    def bot(self) -> BotName:
        return self._bot

    def allows(self, item: KeychainItem) -> bool:
        return item in ALLOWED[self._bot]

    def _check(self, item: KeychainItem) -> None:
        if not self.allows(item):
            raise KeychainItemForbidden(item=str(item), bot=self._bot.value)

    def _call(self, argv: Sequence[str], item: KeychainItem, *, stdin: bytes | None = None) -> proc.ProcResult:
        try:
            return self._run(argv, stdin=stdin, timeout=self._timeout)
        except (TimeoutError, OSError):
            raise KeychainUnavailable(item=str(item)) from None

    def read(self, item: KeychainItem) -> str:
        """The value; it joins the masking set before it is returned. Never log or store it."""
        self._check(item)
        value = password_from(self._call(find_argv(item, reveal=True), item), item)
        redact.add_known_secrets([value])
        return value

    def write(self, item: KeychainItem, value: str) -> None:
        """Create or replace the item through `security -i` stdin. Raises `KeychainUnavailable` when it fails."""
        self._check(item)
        redact.add_known_secrets([value])
        result = self._call(write_argv(), item, stdin=add_script(item, value))
        if result.returncode != 0:
            raise KeychainUnavailable(item=str(item))
