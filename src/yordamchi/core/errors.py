"""The one error hierarchy (CONVENTIONS §7).

`YordamchiError(code, **params)` → `DomainError` (an expected outcome the owner is told about) and `ExternalError`
(raised only by adapters, carries `retryable`). `code` is stable snake_case and keys the Uzbek message
`S.ERRORS[code]`; `params` are exactly that message's placeholders, so they must never hold secrets or message text.
`test_error_catalog` fails on a code without a message.

Block file (TASKS R2): each P1 feature adds its codes and classes only inside its own `# --- <feature> ---` block.
"""

from enum import StrEnum

type ErrorParam = str | int


class ErrorCode(StrEnum):
    # --- core ---
    INTERNAL = "internal"
    CONFIG_INVALID = "config_invalid"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    RATE_LIMITED = "rate_limited"
    QUOTA_EXHAUSTED = "quota_exhausted"
    AUTH_FAILED = "auth_failed"
    CALL_TIMED_OUT = "call_timed_out"
    NOT_INSTALLED = "not_installed"
    # --- inbox ---
    # --- audit ---
    # --- vault ---
    VAULT_KEY_MISSING = "vault_key_missing"
    VAULT_KEY_INSECURE = "vault_key_insecure"
    VAULT_DECRYPT_FAILED = "vault_decrypt_failed"
    VAULT_SECRET_NOT_FOUND = "vault_secret_not_found"  # noqa: S105  # an error code, not a password
    # --- cost ---
    # --- access ---
    # --- outbox ---
    # --- burst ---
    # --- projects ---
    # --- gate ---
    # --- voice ---
    # --- routing ---
    # --- settings ---
    SETTING_UNKNOWN = "setting_unknown"
    SETTING_INVALID = "setting_invalid"
    SETTING_PERSONAL_ONLY = "setting_personal_only"
    SETTING_DEV_ONLY = "setting_dev_only"
    # --- turns ---
    # --- questions ---
    # --- digest ---
    # --- servers ---
    # --- health ---
    # --- wiring (bots) ---


class YordamchiError(Exception):
    """Base of every error Yordamchi raises on purpose."""

    def __init__(self, code: ErrorCode, **params: ErrorParam) -> None:
        super().__init__(code.value)
        self.code = code
        self.params: dict[str, ErrorParam] = dict(params)

    def __str__(self) -> str:
        # Only the code: params go to the owner's message, never into a log line through str(exc).
        return self.code.value

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.code.value}, params={sorted(self.params)})"


class DomainError(YordamchiError):
    """An expected outcome (a refused value, an ambiguous name): a warning in the log, a message to the owner."""


class ExternalError(YordamchiError):
    """A failure of an external system, raised only by adapters. `retryable` says whether `core.retry` may retry."""

    def __init__(self, code: ErrorCode, *, retryable: bool, **params: ErrorParam) -> None:
        super().__init__(code, **params)
        self.retryable = retryable


# --- core ---
class ConfigInvalid(DomainError):
    """A bootstrap value (env file, environment) is missing or malformed. `problem` is a short snake_case code."""

    def __init__(self, *, key: str, problem: str) -> None:
        super().__init__(ErrorCode.CONFIG_INVALID, key=key, problem=problem)


class ProviderUnavailable(ExternalError):
    def __init__(self, *, provider: str, retryable: bool = True) -> None:
        super().__init__(ErrorCode.PROVIDER_UNAVAILABLE, retryable=retryable, provider=provider)


class RateLimited(ExternalError):
    """HTTP 429 or the vendor's equivalent. `retry_after` (seconds) is honoured by `core.retry`."""

    def __init__(self, *, provider: str, retry_after: float | None = None) -> None:
        super().__init__(ErrorCode.RATE_LIMITED, retryable=True, provider=provider)
        self.retry_after = retry_after


class QuotaExhausted(ExternalError):
    """Out of quota or credits (Google `RESOURCE_EXHAUSTED`, OpenRouter 402): waiting does not help."""

    def __init__(self, *, provider: str) -> None:
        super().__init__(ErrorCode.QUOTA_EXHAUSTED, retryable=False, provider=provider)


class AuthFailed(ExternalError):
    def __init__(self, *, provider: str) -> None:
        super().__init__(ErrorCode.AUTH_FAILED, retryable=False, provider=provider)


class CallTimedOut(ExternalError):
    """The call's own `timeout` ran out (A4). `operation` names the call, e.g. `stt`."""

    def __init__(self, *, operation: str) -> None:
        super().__init__(ErrorCode.CALL_TIMED_OUT, retryable=True, operation=operation)


class NotInstalled(ExternalError):
    def __init__(self, *, binary: str) -> None:
        super().__init__(ErrorCode.NOT_INSTALLED, retryable=False, binary=binary)


# --- inbox ---
# --- audit ---
# --- vault ---
class VaultKeyMissing(DomainError):
    """`master.key` is absent, unreadable or not 32 base64 bytes; the installer creates it, a bot never does (SPEC
    §4.5)."""

    def __init__(self) -> None:
        super().__init__(ErrorCode.VAULT_KEY_MISSING)


class VaultKeyInsecure(DomainError):
    """`master.key` is readable by the group or others (NFR-SEC-02 wants 0600); the vault refuses to use it."""

    def __init__(self, *, mode: str) -> None:
        super().__init__(ErrorCode.VAULT_KEY_INSECURE, mode=mode)


class VaultDecryptFailed(DomainError):
    """A ciphertext does not open with this key and AAD: tampered row, wrong row binding or a different key."""

    def __init__(self, *, name: str) -> None:
        super().__init__(ErrorCode.VAULT_DECRYPT_FAILED, name=name)


class VaultSecretNotFound(DomainError):
    """No active secret has this name (never stored, or removed)."""

    def __init__(self, *, name: str) -> None:
        super().__init__(ErrorCode.VAULT_SECRET_NOT_FOUND, name=name)


# --- cost ---
# --- access ---
# --- outbox ---
# --- burst ---
# --- projects ---
# --- gate ---
# --- voice ---
# --- routing ---
# --- settings ---
class SettingUnknown(DomainError):
    def __init__(self, *, key: str) -> None:
        super().__init__(ErrorCode.SETTING_UNKNOWN, key=key)


class SettingInvalid(DomainError):
    """A value fails its type or check. `reason` (snake_case) is for code and logs; the owner sees the options."""

    def __init__(self, *, key: str, reason: str, options: str) -> None:
        super().__init__(ErrorCode.SETTING_INVALID, key=key, options=options)
        self.reason = reason


class SettingPersonalOnly(DomainError):
    """The dev bot was asked to change a PERSONAL key: "buni shaxsiy botga ayting" (SPEC §2.3)."""

    def __init__(self, *, key: str) -> None:
        super().__init__(ErrorCode.SETTING_PERSONAL_ONLY, key=key)


class SettingDevOnly(DomainError):
    """The personal bot was asked to change a DEV key."""

    def __init__(self, *, key: str) -> None:
        super().__init__(ErrorCode.SETTING_DEV_ONLY, key=key)


# --- turns ---
# --- questions ---
# --- digest ---
# --- servers ---
# --- health ---
# --- wiring (bots) ---
