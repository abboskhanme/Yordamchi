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
    GIT_FAILED = "git_failed"  # adapters/git (T17): a local git command failed
    PROJECT_NOT_FOUND = "project_not_found"
    PROJECT_AMBIGUOUS = "project_ambiguous"
    PROJECT_ALIAS_TAKEN = "project_alias_taken"
    PROJECT_ALIAS_INVALID = "project_alias_invalid"
    PROJECT_DESCRIPTION_INVALID = "project_description_invalid"
    # --- gate ---
    DECISION_NOT_FOUND = "decision_not_found"
    # --- voice ---
    STT_TERM_INVALID = "stt_term_invalid"
    # --- routing ---
    # --- settings ---
    SETTING_UNKNOWN = "setting_unknown"
    SETTING_INVALID = "setting_invalid"
    SETTING_PERSONAL_ONLY = "setting_personal_only"
    SETTING_DEV_ONLY = "setting_dev_only"
    # --- turns ---
    TURN_NOT_FOUND = "turn_not_found"
    # --- questions ---
    # --- digest ---
    # --- servers ---
    SERVER_JUMP_ONLY = "server_jump_only"
    SERVER_NOT_WATCHED = "server_not_watched"
    # --- health ---
    # --- wiring (bots) ---
    # --- web ---
    WEB_UNREACHABLE = "web_unreachable"
    WEB_NOT_FOUND = "web_not_found"
    WEB_ACCESS_DENIED = "web_access_denied"
    WEB_LOGIN_REQUIRED = "web_login_required"
    WEB_PAYWALLED = "web_paywalled"
    WEB_GEO_BLOCKED = "web_geo_blocked"
    WEB_BOT_CHECK = "web_bot_check"
    WEB_CONTENT_UNAVAILABLE = "web_content_unavailable"
    WEB_NO_MEDIA = "web_no_media"
    WEB_NOT_A_PAGE = "web_not_a_page"
    WEB_CONTENT_TOO_LARGE = "web_content_too_large"
    VIDEO_NOT_FOUND = "video_not_found"
    VIDEO_IS_LIVE = "video_is_live"
    VIDEO_TOO_LONG = "video_too_long"
    VIDEO_ENCODE_FAILED = "video_encode_failed"
    # --- artefacts ---
    WEB_URL_INVALID = "web_url_invalid"
    VIDEO_CLIP_INVALID = "video_clip_invalid"
    VIDEO_TOO_LARGE = "video_too_large"

    # --- integrations ---
    OAUTH_REQUIRED = "oauth_required"
    OAUTH_REAUTH_REQUIRED = "oauth_reauth_required"
    OAUTH_CLIENT_METADATA_NEEDED = "oauth_client_metadata_needed"
    OAUTH_PASTE_TIMED_OUT = "oauth_paste_timed_out"
    OAUTH_NOT_PENDING = "oauth_not_pending"
    OAUTH_CALLBACK_INVALID = "oauth_callback_invalid"
    INTEGRATION_FAILED = "integration_failed"
    INTEGRATION_NOT_FOUND = "integration_not_found"
    INTEGRATION_EXISTS = "integration_exists"
    INTEGRATION_NAME_INVALID = "integration_name_invalid"
    INTEGRATION_URL_INVALID = "integration_url_invalid"
    INTEGRATION_ASSIGN_REFUSED = "integration_assign_refused"


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
class GitFailed(ExternalError):
    """A local git command exited non-zero (adapters/git). `reason` is for code and logs only: `not_a_repo`,
    `bad_ref`, `conflict`, `locked` (another git holds the index lock: retryable) or `failed`. git's own output is
    never kept: it may quote file content."""

    def __init__(self, *, operation: str, reason: str) -> None:
        super().__init__(ErrorCode.GIT_FAILED, retryable=reason == "locked", operation=operation)
        self.reason = reason


class ProjectNotFound(DomainError):
    """No registered project has this folder name or alias (exact, case-insensitive)."""

    def __init__(self, *, name: str) -> None:
        super().__init__(ErrorCode.PROJECT_NOT_FOUND, name=name)


class ProjectAmbiguous(DomainError):
    """More than one project answers to this name. `candidates` is the comma-separated list the owner picks from."""

    def __init__(self, *, name: str, candidates: str) -> None:
        super().__init__(ErrorCode.PROJECT_AMBIGUOUS, name=name, candidates=candidates)


class ProjectAliasTaken(DomainError):
    """The alias is already another project's folder name or alias."""

    def __init__(self, *, alias: str, project: str) -> None:
        super().__init__(ErrorCode.PROJECT_ALIAS_TAKEN, alias=alias, project=project)


class ProjectAliasInvalid(DomainError):
    def __init__(self, *, alias: str, limit: int) -> None:
        super().__init__(ErrorCode.PROJECT_ALIAS_INVALID, alias=alias, limit=limit)


class ProjectDescriptionInvalid(DomainError):
    def __init__(self, *, limit: int) -> None:
        super().__init__(ErrorCode.PROJECT_DESCRIPTION_INVALID, limit=limit)


# --- gate ---
class DecisionNotFound(DomainError):
    """The owner answered a decision number (`#17 ha`) that no decision has."""

    def __init__(self, *, decision_id: int) -> None:
        super().__init__(ErrorCode.DECISION_NOT_FOUND, decision_id=decision_id)


# --- voice ---
class SttTermInvalid(DomainError):
    """A term for the STT vocabulary ("bu so'zni to'g'ri yoz: …") is empty or longer than `limit` characters."""

    def __init__(self, *, limit: int) -> None:
        super().__init__(ErrorCode.STT_TERM_INVALID, limit=limit)


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
class TurnNotFound(DomainError):
    """An answer, an approval or a revert names a turn that is not (or no longer) there."""

    def __init__(self, *, turn_id: int) -> None:
        super().__init__(ErrorCode.TURN_NOT_FOUND, turn_id=turn_id)


# --- questions ---
# --- digest ---
# --- servers ---
class ServerJumpOnly(DomainError):
    """The host is in `gate.jump_only_hosts` (the Windows host with production 1C): a jump, never a target, so it is
    neither watched nor checked."""

    def __init__(self, *, host: str) -> None:
        super().__init__(ErrorCode.SERVER_JUMP_ONLY, host=host)


class ServerNotWatched(DomainError):
    """The owner named a server that is not in `servers.hosts`. `hosts` is the comma-separated list that is."""

    def __init__(self, *, host: str, hosts: str) -> None:
        super().__init__(ErrorCode.SERVER_NOT_WATCHED, host=host, hosts=hosts)


# --- health ---
# --- wiring (bots) ---
# --- web ---
# Raised by adapters/web (T74). `site` is the link's host name (never the full URL: it may carry a token).
class WebUnreachable(ExternalError):
    """The site does not resolve, refuses the connection or fails TLS: a retry does not help."""

    def __init__(self, *, site: str) -> None:
        super().__init__(ErrorCode.WEB_UNREACHABLE, retryable=False, site=site)


class WebNotFound(ExternalError):
    """HTTP 404/410: the page or file is not there."""

    def __init__(self, *, site: str) -> None:
        super().__init__(ErrorCode.WEB_NOT_FOUND, retryable=False, site=site)


class WebAccessDenied(ExternalError):
    """HTTP 403 without a login hint: the site blocks this client (or wants a login it does not say so about)."""

    def __init__(self, *, site: str) -> None:
        super().__init__(ErrorCode.WEB_ACCESS_DENIED, retryable=False, site=site)


class WebLoginRequired(ExternalError):
    """Only a logged-in account sees this (HTTP 401, a private or age-gated video). Logged-in browsing is out of
    scope (ASSUMPTIONS 2026-10-23), so this is final."""

    def __init__(self, *, site: str) -> None:
        super().__init__(ErrorCode.WEB_LOGIN_REQUIRED, retryable=False, site=site)


class WebPaywalled(ExternalError):
    """Paid content: HTTP 402, members-only, premium, rental or purchase."""

    def __init__(self, *, site: str) -> None:
        super().__init__(ErrorCode.WEB_PAYWALLED, retryable=False, site=site)


class WebGeoBlocked(ExternalError):
    def __init__(self, *, site: str) -> None:
        super().__init__(ErrorCode.WEB_GEO_BLOCKED, retryable=False, site=site)


class WebBotCheck(ExternalError):
    """The site asked to prove this is not a bot (YouTube "Sign in to confirm you're not a bot"). Retrying at once
    makes it worse, so it is not retryable here; the owner may ask again later."""

    def __init__(self, *, site: str) -> None:
        super().__init__(ErrorCode.WEB_BOT_CHECK, retryable=False, site=site)


class WebContentUnavailable(ExternalError):
    """Removed, private-by-owner or otherwise gone (yt-dlp "Video unavailable")."""

    def __init__(self, *, site: str) -> None:
        super().__init__(ErrorCode.WEB_CONTENT_UNAVAILABLE, retryable=False, site=site)


class WebNoMedia(ExternalError):
    """The page holds no file or image to fetch (no `og:image`, no `<img>`)."""

    def __init__(self, *, site: str) -> None:
        super().__init__(ErrorCode.WEB_NO_MEDIA, retryable=False, site=site)


class WebNotAPage(ExternalError):
    """A screenshot was asked of a link that downloads a file instead of showing a page."""

    def __init__(self, *, site: str) -> None:
        super().__init__(ErrorCode.WEB_NOT_A_PAGE, retryable=False, site=site)


class WebContentTooLarge(ExternalError):
    """The file is over the cap before or while downloading; the partial file is deleted."""

    def __init__(self, *, limit_mb: int) -> None:
        super().__init__(ErrorCode.WEB_CONTENT_TOO_LARGE, retryable=False, limit_mb=limit_mb)


class VideoNotFound(ExternalError):
    """yt-dlp finds no downloadable video at the link (unsupported site, no formats)."""

    def __init__(self, *, site: str) -> None:
        super().__init__(ErrorCode.VIDEO_NOT_FOUND, retryable=False, site=site)


class VideoIsLive(ExternalError):
    def __init__(self, *, site: str) -> None:
        super().__init__(ErrorCode.VIDEO_IS_LIVE, retryable=False, site=site)


class VideoTooLong(ExternalError):
    """The video is longer than the caller's `max_duration_s`: refused before any byte is fetched. `link` is the
    video's own page (not a param: it never reaches a log line), for the "direct link" alternative."""

    def __init__(self, *, minutes: int, link: str | None = None) -> None:
        super().__init__(ErrorCode.VIDEO_TOO_LONG, retryable=False, minutes=minutes)
        self.link = link


class VideoEncodeFailed(ExternalError):
    """ffmpeg exited non-zero or wrote no file. Its stderr is never kept (it may quote the source URL)."""

    def __init__(self) -> None:
        super().__init__(ErrorCode.VIDEO_ENCODE_FAILED, retryable=False)


# --- artefacts ---
class WebUrlInvalid(DomainError):
    """Only absolute http(s) links with a host are opened (no file:, data:, javascript: …)."""

    def __init__(self) -> None:
        super().__init__(ErrorCode.WEB_URL_INVALID)


class VideoClipInvalid(DomainError):
    """A clip that starts before 0, ends before it starts, or is longer than `max_minutes` (what still fits)."""

    def __init__(self, *, max_minutes: int) -> None:
        super().__init__(ErrorCode.VIDEO_CLIP_INVALID, max_minutes=max_minutes)


class VideoTooLarge(DomainError):
    """The video cannot fit the send cap even at the lowest allowed bitrate (UC-32 F). The reply's clip offer is
    computed by `personal/artefacts/domain.fit_minutes`."""

    def __init__(self, *, limit_mb: int) -> None:
        super().__init__(ErrorCode.VIDEO_TOO_LARGE, limit_mb=limit_mb)


# --- integrations ---
class OAuthRequired(ExternalError):
    """The MCP server answered 401 and advertises OAuth (`WWW-Authenticate` with resource metadata): run the flow."""

    def __init__(self, *, provider: str) -> None:
        super().__init__(ErrorCode.OAUTH_REQUIRED, retryable=False, provider=provider)


class OAuthReauthRequired(ExternalError):
    """The authorization server refused the refresh token (expired, revoked, or none was issued)."""

    def __init__(self, *, provider: str) -> None:
        super().__init__(ErrorCode.OAUTH_REAUTH_REQUIRED, retryable=False, provider=provider)


class OAuthClientMetadataNeeded(ExternalError):
    """The server registers no clients dynamically and `integrations.oauth_client_metadata_url` is empty (CIMD)."""

    def __init__(self, *, provider: str) -> None:
        super().__init__(ErrorCode.OAUTH_CLIENT_METADATA_NEEDED, retryable=False, provider=provider)


class OAuthPasteTimedOut(DomainError):
    """The owner did not paste the redirect URL back within `integrations.oauth_paste_timeout_min`."""

    def __init__(self, *, name: str, minutes: int) -> None:
        super().__init__(ErrorCode.OAUTH_PASTE_TIMED_OUT, name=name, minutes=minutes)


class OAuthNotPending(DomainError):
    """A pasted redirect URL arrived while no OAuth flow waits for one (or none with its `state`)."""

    def __init__(self) -> None:
        super().__init__(ErrorCode.OAUTH_NOT_PENDING)


class OAuthCallbackInvalid(DomainError):
    """The pasted text holds no authorization code (an `error=` redirect, or something else entirely)."""

    def __init__(self) -> None:
        super().__init__(ErrorCode.OAUTH_CALLBACK_INVALID)


class IntegrationFailed(DomainError):
    """Validation or a call failed (UC-08 F). `reason` and `fix` are already-rendered Uzbek fragments of the catalog
    (`S.INTEGRATION_REASONS`, `S.INTEGRATION_FIXES`); `reason_code` stays for code and logs."""

    def __init__(self, *, name: str, reason: str, fix: str, reason_code: str) -> None:
        super().__init__(ErrorCode.INTEGRATION_FAILED, name=name, reason=reason, fix=fix)
        self.reason_code = reason_code


class IntegrationNotFound(DomainError):
    def __init__(self, *, name: str) -> None:
        super().__init__(ErrorCode.INTEGRATION_NOT_FOUND, name=name)


class IntegrationExists(DomainError):
    """An active integration already answers to this name and works; re-adding is for a broken one only."""

    def __init__(self, *, name: str) -> None:
        super().__init__(ErrorCode.INTEGRATION_EXISTS, name=name)


class IntegrationNameInvalid(DomainError):
    """The name gives no usable slug (`^[a-z][a-z0-9_]{1,30}$` after transliteration)."""

    def __init__(self, *, name: str) -> None:
        super().__init__(ErrorCode.INTEGRATION_NAME_INVALID, name=name)


class IntegrationUrlInvalid(DomainError):
    """Not an https URL (plain http only for localhost), or it carries credentials in the userinfo part. The URL is
    not echoed back: it may hold something secret."""

    def __init__(self) -> None:
        super().__init__(ErrorCode.INTEGRATION_URL_INVALID)


class IntegrationAssignRefused(DomainError):
    """The personal bot adds integrations for itself only (SPEC §2.3)."""

    def __init__(self) -> None:
        super().__init__(ErrorCode.INTEGRATION_ASSIGN_REFUSED)
