"""The dev bot token from the macOS login Keychain, standard library only (owner requirement 2026-10-24).

`dev.env` holds `DEV_BOT_TOKEN=@keychain:uz.yordamchi/dev-bot-token`, never the token. This is the supervisor's copy of
the app's `core.keychain` read path (A6 forbids importing it): it reads exactly that one item, with Apple-signed
`/usr/bin/security find-generic-password -s uz.yordamchi -a dev-bot-token -w`. argv names the item only; the token
comes back on stdout and never reaches argv, a log line or a file. The item was created by the installer with
`-T /usr/bin/security`, so this reads without a prompt from launchd.

Problems are short codes (`AlertConfigError`-style), logged by the alerter; never the value or the command output.
"""

from typing import Final, Protocol

from yordamchi_supervisor.proc import Runner

SECURITY: Final = "/usr/bin/security"
SERVICE: Final = "uz.yordamchi"
DEV_TOKEN_ACCOUNT: Final = "dev-bot-token"  # noqa: S105 - an item name, not a token
REFERENCE_PREFIX: Final = "@keychain:"
DEV_TOKEN_REFERENCE: Final = f"{REFERENCE_PREFIX}{SERVICE}/{DEV_TOKEN_ACCOUNT}"
ITEM_NOT_FOUND_EXIT: Final = 44  # errSecItemNotFound
TIMEOUT_S: Final = 10.0


class KeychainError(Exception):
    def __init__(self, problem: str) -> None:
        super().__init__(problem)
        self.problem = problem


class TokenSource(Protocol):
    def dev_token(self) -> str:
        """The dev bot token. Raises `KeychainError("keychain_item_missing" | "keychain_unavailable")`."""
        ...


def dev_token_argv() -> list[str]:
    return [SECURITY, "find-generic-password", "-s", SERVICE, "-a", DEV_TOKEN_ACCOUNT, "-w"]


class SecurityKeychain:
    """Reads the one item the supervisor may hold, through its `Runner` (minimal env, timeout, never a shell)."""

    def __init__(self, runner: Runner, *, timeout: float = TIMEOUT_S) -> None:
        self._runner = runner
        self._timeout = timeout

    def dev_token(self) -> str:
        result = self._runner.run(dev_token_argv(), timeout=self._timeout)
        if result.returncode == ITEM_NOT_FOUND_EXIT:
            raise KeychainError("keychain_item_missing")
        if not result.ok:
            raise KeychainError("keychain_unavailable")
        token = result.stdout.removesuffix("\n")
        if not token:
            raise KeychainError("keychain_item_missing")
        return token
