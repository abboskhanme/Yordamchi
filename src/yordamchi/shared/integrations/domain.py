"""Integration rules, pure (SPEC §3.2.10, §4.2 `integrations`; UC-08, UC-35; NFR-SEC-07; D11).

- An integration belongs to **Yordamchi**: its row in this bot's DB plus a vault secret or a sealed OAuth grant. Nothing
  here reads a Claude account, so a session's MCP config is the same whichever account ClaudeManager has active
  (the UC-08 rotation AC holds by construction).
- The **slug** (`^[a-z][a-z0-9_]{1,30}$`) is the MCP server name in a session (`mcp__<slug>__<tool>`) and the key the
  `headers` helper asks for. It is derived from the owner's name for it ("Bonvi HR" → `bonvi_hr`).
- **Assignment**: the bot it was said in; the dev bot also accepts "ikkala bot uchun". The personal bot adds for
  itself only (SPEC §2.3).
- **Session injection**: an HTTP MCP server carries only `url` and a `headersHelper` command. The helper fetches the
  auth header over the control socket at connect time, so no secret is ever on the `claude` command line (STACK §4.2).
- **Status**: `ok`, `auth_failed`, `unreachable`, `needs_reauth`, `disabled` (SPEC §4.2) plus `last_validated_at`.
  The task names them ok / auth expired / failing: `needs_reauth` is "auth expired", the other two are "failing".
"""

import hmac
import re
import shlex
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Final
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from yordamchi.core import strings as S
from yordamchi.core import text
from yordamchi.core.errors import (
    ErrorCode,
    IntegrationAssignRefused,
    IntegrationNameInvalid,
    IntegrationUrlInvalid,
)
from yordamchi.core.json_value import JsonObject, JsonValue
from yordamchi.core.paths import BotName
from yordamchi.core.text import BotText
from yordamchi.ports.agent import McpHttpServer
from yordamchi.ports.mcp import McpTool, OAuthCallback, OAuthGrant

SLUG_PATTERN: Final = re.compile(r"[a-z][a-z0-9_]{1,30}")
SLUG_MAX: Final = 31
RESTORE_WINDOW: Final = timedelta(days=30)  # SPEC §3.2.10 step 8: "Reverting a removal works for 30 days"
HELPER_RELATIVE: Final = PurePosixPath("bin/yordamchi-helper")
HELPER_METHOD: Final = "headers"
CLIENT_NAME: Final = "Yordamchi"
CALLBACK_PATH: Final = "/callback"
# Dev executor sessions get the remote servers; smoke sessions stay clean (`strict_mcp`), and the personal bot's
# sessions reach integrations only through the in-process gateway `yordamchi_int` (SPEC §3.2.10 step 7, T76).
INJECTED_SESSION_KINDS: Final = frozenset({"coding", "investigation"})
MAX_CODE_CHARS: Final = 2048
_LOCAL_HOSTS: Final = frozenset({"localhost", "127.0.0.1", "::1"})
_APOSTROPHES: Final = "'`’‘ʻʼ"
_BARE_CODE: Final = re.compile(r"[A-Za-z0-9._~+/=%-]{4,2048}")
_NON_SLUG: Final = re.compile(r"[^a-z0-9]+")


class IntegrationKind(StrEnum):
    MCP_HTTP = "mcp_http"
    API_HTTP = "api_http"


class AuthKind(StrEnum):
    NONE = "none"
    BEARER = "bearer"  # Authorization: Bearer <secret>
    HEADER = "header"  # <header_name>: <secret>
    OAUTH = "oauth"  # Authorization: Bearer <access token of the sealed grant>
    QUERY = "query"  # ?<header_name>=<secret> (an API key many REST APIs take as a query parameter; T76)


class IntegrationStatus(StrEnum):
    OK = "ok"
    AUTH_FAILED = "auth_failed"
    UNREACHABLE = "unreachable"
    NEEDS_REAUTH = "needs_reauth"
    DISABLED = "disabled"


# A server that may come back stays in sessions; a refused credential or an expired grant would only fail there.
INJECTED_STATUSES: Final = frozenset({IntegrationStatus.OK, IntegrationStatus.UNREACHABLE})


@dataclass(frozen=True, slots=True)
class ToolEntry:
    name: str
    read_only: bool


@dataclass(frozen=True, slots=True)
class ToolPolicy:
    """`tool_policy_json`: which non-read-only tools the owner allowed when adding (UC-35). `dev_all` = every tool."""

    dev: tuple[str, ...] = ()
    personal: tuple[str, ...] = ()
    dev_all: bool = False


@dataclass(frozen=True, slots=True)
class IntegrationView:
    """One `integrations` row as the rest of the code sees it: never a secret."""

    id: int
    slug: str
    display_name: str
    kind: IntegrationKind
    url: str
    auth: AuthKind
    bots: tuple[BotName, ...]
    status: IntegrationStatus
    tools: tuple[ToolEntry, ...] = ()
    policy: ToolPolicy = field(default_factory=ToolPolicy)
    description: str = ""
    last_validated_at: datetime | None = None
    removed_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class AddPlan:
    """A checked "X'ga ulan" request: what the service validates and stores."""

    slug: str
    display_name: str
    url: str
    bots: tuple[BotName, ...]


@dataclass(frozen=True, slots=True)
class RefreshOutcome:
    failures: int
    needs_reauth: bool


# --- names, URLs, assignment ----------------------------------------------------------------------------------------
def slugify(name: str) -> str:
    """ "Bonvi HR" → `bonvi_hr`, "Wunderkind Maktab" → `wunderkind_maktab`, "O'quv markazi" → `oquv_markazi`.
    Raises `IntegrationNameInvalid` when nothing usable is left."""
    folded = unicodedata.normalize("NFKD", name)
    ascii_only = "".join(char for char in folded if not unicodedata.combining(char) and char not in _APOSTROPHES)
    slug = _NON_SLUG.sub("_", ascii_only.lower()).strip("_")[:SLUG_MAX].rstrip("_")
    if not SLUG_PATTERN.fullmatch(slug):
        raise IntegrationNameInvalid(name=name.strip()[:64] or "?")
    return slug


def check_url(url: str) -> str:
    """An MCP or API base URL: https (plain http only for localhost), a host, no userinfo, no fragment. The URL is
    put on the `claude` command line (`--mcp-config`), so credentials in it are refused."""
    candidate = url.strip()
    try:
        parts = urlsplit(candidate)
        host = parts.hostname
        has_userinfo = parts.username is not None or parts.password is not None
        _ = parts.port  # a malformed port raises ValueError
    except ValueError:
        raise IntegrationUrlInvalid from None
    secure = parts.scheme == "https" or (parts.scheme == "http" and host in _LOCAL_HOSTS)
    if not secure or not host or has_userinfo or parts.fragment or any(char.isspace() for char in candidate):
        raise IntegrationUrlInvalid
    return candidate


def assigned_bots(said_in: BotName, *, both: bool) -> tuple[BotName, ...]:
    """SPEC §3.2.10 step 1: the bot it was said in; "ikkala bot uchun" adds the other one (dev bot only)."""
    if not both:
        return (said_in,)
    if said_in is not BotName.DEV:
        raise IntegrationAssignRefused
    return (BotName.DEV, BotName.PERSONAL)


def check_assignment(said_in: BotName, bots: Sequence[BotName]) -> tuple[BotName, ...]:
    """A reassignment: the personal bot may only keep an integration for itself."""
    ordered = tuple(bot for bot in BotName if bot in set(bots))
    if not ordered or (said_in is BotName.PERSONAL and ordered != (BotName.PERSONAL,)):
        raise IntegrationAssignRefused
    return ordered


def plan_add(*, name: str, url: str, said_in: BotName, both: bool) -> AddPlan:
    return AddPlan(
        slug=slugify(name),
        display_name=" ".join(name.split())[:64],
        url=check_url(url),
        bots=assigned_bots(said_in, both=both),
    )


def bots_json(bots: Iterable[BotName]) -> JsonValue:
    return [bot.value for bot in BotName if bot in set(bots)]


def bots_from_json(raw: JsonValue) -> tuple[BotName, ...]:
    """Unknown entries are ignored (a newer release may add a bot)."""
    if not isinstance(raw, list):
        return ()
    known = {bot.value: bot for bot in BotName}
    return tuple(known[item] for item in raw if isinstance(item, str) and item in known)


def bots_label(bots: Sequence[BotName]) -> BotText:
    key = "both" if set(bots) >= {BotName.DEV, BotName.PERSONAL} else (bots[0].value if bots else "dev")
    return S.INTEGRATION_BOTS[key]()


# --- lookup ---------------------------------------------------------------------------------------------------------
def find(views: Iterable[IntegrationView], name: str) -> IntegrationView | None:
    """By slug or display name, case-insensitive. Only the slug form is tried when the name gives one."""
    wanted = " ".join(name.split()).casefold()
    try:
        slug = slugify(name)
    except IntegrationNameInvalid:
        slug = None
    for view in views:
        if view.slug == slug or view.display_name.casefold() == wanted:
            return view
    return None


def restorable(removed_at: datetime | None, now: datetime) -> bool:
    return removed_at is not None and now - removed_at <= RESTORE_WINDOW


def retention_cutoff(now: datetime) -> datetime:
    """SPEC §4.2: a removed integration (and its sealed grant) is deleted once removed strictly before this."""
    return now - RESTORE_WINDOW


# --- tools ----------------------------------------------------------------------------------------------------------
def tools_cache(tools: Sequence[McpTool]) -> JsonValue:
    """`tools_cache_json`: names and `readOnlyHint` only (descriptions are server content and stay out of the DB)."""
    entries: list[JsonValue] = [{"name": tool.name, "read_only": tool.read_only} for tool in tools]
    return entries


def tools_from_cache(raw: JsonValue) -> tuple[ToolEntry, ...]:
    if not isinstance(raw, list):
        return ()
    return tuple(
        ToolEntry(name=name, read_only=item.get("read_only") is True)
        for item in raw
        if isinstance(item, dict) and isinstance(name := item.get("name"), str)
    )


def tools_text(tools: Sequence[ToolEntry]) -> BotText:
    """The confirmation's tool list, read-only tools marked (SPEC §3.2.10 step 4)."""
    if not tools:
        return S.INTEGRATION_NO_TOOLS()
    parts = (
        S.INTEGRATION_TOOL_READ_ONLY(tool=tool.name) if tool.read_only else S.INTEGRATION_TOOL(tool=tool.name)
        for tool in tools
    )
    return text.join(parts, ", ")


def default_policy_json() -> JsonValue:
    return {"dev": [], "personal": []}


def policy_from_json(raw: JsonValue) -> ToolPolicy:
    if not isinstance(raw, dict):
        return ToolPolicy()
    dev, personal = raw.get("dev"), raw.get("personal")
    return ToolPolicy(
        dev=tuple(item for item in dev if isinstance(item, str)) if isinstance(dev, list) else (),
        personal=tuple(item for item in personal if isinstance(item, str)) if isinstance(personal, list) else (),
        dev_all=dev == "all",
    )


def tool_name(slug: str, tool: str) -> str:
    """The name Claude Code gives a remote MCP tool."""
    return f"mcp__{slug}__{tool}"


def allowed_tools(views: Iterable[IntegrationView], bot: BotName) -> frozenset[str]:
    """Tools of this bot's injected integrations that run without asking: `readOnlyHint`, or allowed at add time.
    Any other MCP tool is the gate's `third_party_send` (ask) on the dev bot, and absent on the personal bot."""
    names: set[str] = set()
    for view in views:
        if view.removed_at is not None or bot not in view.bots:
            continue
        explicit = set(view.policy.dev if bot is BotName.DEV else view.policy.personal)
        for tool in view.tools:
            dev_all = bot is BotName.DEV and view.policy.dev_all
            if tool.read_only or dev_all or tool.name in explicit:
                names.add(tool_name(view.slug, tool.name))
    return frozenset(names)


# --- statuses and failures ------------------------------------------------------------------------------------------
_STATUS_OF_ERROR: Final[Mapping[ErrorCode, IntegrationStatus]] = {
    ErrorCode.AUTH_FAILED: IntegrationStatus.AUTH_FAILED,
    ErrorCode.OAUTH_REQUIRED: IntegrationStatus.AUTH_FAILED,
    ErrorCode.OAUTH_REAUTH_REQUIRED: IntegrationStatus.NEEDS_REAUTH,
}
_REASON_OF_ERROR: Final[Mapping[ErrorCode, str]] = {
    ErrorCode.PROVIDER_UNAVAILABLE: "unreachable",
    ErrorCode.CALL_TIMED_OUT: "timed_out",
    ErrorCode.AUTH_FAILED: "auth_failed",
    ErrorCode.OAUTH_REQUIRED: "auth_failed",
    ErrorCode.RATE_LIMITED: "rate_limited",
    ErrorCode.QUOTA_EXHAUSTED: "rate_limited",
    ErrorCode.OAUTH_REAUTH_REQUIRED: "oauth_reauth_required",
    ErrorCode.OAUTH_CLIENT_METADATA_NEEDED: "oauth_client_metadata_needed",
    ErrorCode.OAUTH_PASTE_TIMED_OUT: "oauth_paste_timed_out",
    ErrorCode.VAULT_DECRYPT_FAILED: "secret_unreadable",
    ErrorCode.VAULT_SECRET_NOT_FOUND: "secret_unreadable",
    ErrorCode.VAULT_KEY_MISSING: "secret_unreadable",
    ErrorCode.VAULT_KEY_INSECURE: "secret_unreadable",
}


def status_after(code: ErrorCode) -> IntegrationStatus:
    """The status a failed check leaves: a refused credential, an expired grant, or (anything else) unreachable."""
    return _STATUS_OF_ERROR.get(code, IntegrationStatus.UNREACHABLE)


def reason_of(code: ErrorCode) -> str:
    """The `S.INTEGRATION_REASONS` / `S.INTEGRATION_FIXES` key for an error code."""
    return _REASON_OF_ERROR.get(code, "internal")


def failure_parts(reason: str) -> tuple[BotText, BotText]:
    return S.INTEGRATION_REASONS[reason](), S.INTEGRATION_FIXES[reason]()


def list_text(views: Sequence[IntegrationView], *, timezone: str) -> BotText:
    """ "integratsiyalar" (UC-08: list by chat): name, kind, bots, status and the last check, oldest first."""
    if not views:
        return S.INTEGRATION_LIST_EMPTY()
    lines = [S.INTEGRATION_LIST_HEAD()]
    for view in views:
        checked = (
            view.last_validated_at.astimezone(ZoneInfo(timezone)).strftime("%Y-%m-%d %H:%M")
            if view.last_validated_at is not None
            else S.INTEGRATION_NEVER_CHECKED()
        )
        lines.append(
            S.INTEGRATION_LIST_ITEM(
                name=view.display_name,
                kind=S.INTEGRATION_KINDS[view.kind.value](),
                bots=bots_label(view.bots),
                status=S.INTEGRATION_STATUSES[view.status.value](),
                checked=checked,
            )
        )
    return text.join(lines)


# --- session injection ----------------------------------------------------------------------------------------------
def helper_command(release: PurePosixPath | str) -> str:
    """`'<DATA>/releases/current/bin/yordamchi-helper' headers`: Claude Code runs it in a shell (the data dir holds a
    space), with `CLAUDE_CODE_MCP_SERVER_NAME` set and the session's `YORDAMCHI_SOCK` / `YORDAMCHI_SESSION_TOKEN`."""
    return f"{shlex.quote(str(PurePosixPath(release) / HELPER_RELATIVE))} {HELPER_METHOD}"


def injected(view: IntegrationView, *, bot: BotName, session_kind: str) -> bool:
    return (
        bot is BotName.DEV
        and session_kind in INJECTED_SESSION_KINDS
        and view.kind is IntegrationKind.MCP_HTTP
        and view.removed_at is None
        and bot in view.bots
        and view.status in INJECTED_STATUSES
    )


def session_servers(
    views: Iterable[IntegrationView], *, bot: BotName, session_kind: str, helper: str
) -> dict[str, McpHttpServer]:
    """`SessionSpec.mcp_servers` for one session: built from Yordamchi's registry alone, URL + helper only."""
    return {
        view.slug: McpHttpServer(url=view.url, headers_helper=helper)
        for view in sorted(views, key=lambda item: item.slug)
        if injected(view, bot=bot, session_kind=session_kind)
    }


def auth_headers(auth: AuthKind, *, value: str, header_name: str | None) -> dict[str, str]:
    """The header a stored secret becomes. `NONE` and `QUERY` send none."""
    if auth in {AuthKind.NONE, AuthKind.QUERY}:
        return {}
    if auth is AuthKind.HEADER:
        return {header_name or "Authorization": value}
    return {"Authorization": f"Bearer {value}"}


# --- OAuth ----------------------------------------------------------------------------------------------------------
def redirect_uri(port: int) -> str:
    return f"http://localhost:{port}{CALLBACK_PATH}"


def parse_callback(pasted: str) -> OAuthCallback | None:
    """The owner's paste: the final redirect URL (`http://localhost:33418/callback?code=…&state=…`), its query alone,
    or just the code. None when it holds no code (an `error=` redirect, prose)."""
    candidate = pasted.strip()
    if not candidate:
        return None
    if "code=" in candidate:
        query = candidate.split("?", 1)[1] if "?" in candidate else candidate
        params = parse_qs(query.split("#", 1)[0], keep_blank_values=False)
        codes, states, issuers = params.get("code", []), params.get("state", []), params.get("iss", [])
        if len(codes) != 1 or len(codes[0]) > MAX_CODE_CHARS:
            return None
        return OAuthCallback(
            code=codes[0], state=states[0] if len(states) == 1 else None, iss=issuers[0] if len(issuers) == 1 else None
        )
    if "://" in candidate or not _BARE_CODE.fullmatch(candidate):
        return None
    return OAuthCallback(code=candidate)


def state_of(authorization_url: str) -> str | None:
    """The `state` the SDK put into the authorization URL."""
    states = parse_qs(urlsplit(authorization_url).query).get("state", [])
    return states[0] if len(states) == 1 else None


def match_flow(flows: Mapping[str, str | None], callback: OAuthCallback) -> str | None:
    """Which waiting flow (slug → its `state`) a paste belongs to: the one whose state matches; a paste without a
    state (just the code) only when exactly one flow waits."""
    if callback.state is not None:
        for slug, state in flows.items():
            if state is not None and hmac.compare_digest(state, callback.state):
                return slug
        return None
    return next(iter(flows)) if len(flows) == 1 else None


def expires_at(now: datetime, expires_in: int | None) -> datetime | None:
    return now + timedelta(seconds=expires_in) if expires_in is not None else None


def refresh_due(expires: datetime | None, now: datetime, *, lead_min: int) -> bool:
    """SPEC §3.2.10 step 5: refresh when `expires_at - now < lead`. No stated expiry → never proactively."""
    return expires is not None and expires - now < timedelta(minutes=lead_min)


def after_refused_refresh(failures: int, *, limit: int) -> RefreshOutcome:
    """One more refused refresh; `limit` in a row (`integrations.refresh_failures_max`) → `needs_reauth`."""
    count = failures + 1
    return RefreshOutcome(failures=count, needs_reauth=count >= limit)


def grant_aad(integration_id: int) -> bytes:
    """Binds a sealed grant to its row, as the vault's AAD binds a secret (SPEC §4.5). The slug is left out: a
    rename must not orphan the grant."""
    return f"oauth_clients|{integration_id}".encode()


def grant_document(grant: OAuthGrant) -> JsonObject:
    """What is sealed: the adapter's opaque state plus what the service reads back, versioned for a later release."""
    secrets: list[JsonValue] = list(grant.secret_values)
    return {
        "v": 1,
        "state": grant.state,
        "access_token": grant.access_token,
        "expires_in": grant.expires_in,
        "can_refresh": grant.can_refresh,
        "secret_values": secrets,
    }


def grant_from_document(document: JsonValue, *, issuer: str | None, client_id: str) -> OAuthGrant | None:
    """The sealed document back as a grant; None when it is not one this release understands."""
    if not isinstance(document, dict) or document.get("v") != 1:
        return None
    state, access, expires_in, values = (
        document.get("state"),
        document.get("access_token"),
        document.get("expires_in"),
        document.get("secret_values"),
    )
    if not isinstance(state, dict) or not isinstance(access, str) or not isinstance(values, list):
        return None
    return OAuthGrant(
        state=state,
        access_token=access,
        expires_in=expires_in if isinstance(expires_in, int) and not isinstance(expires_in, bool) else None,
        issuer=issuer,
        client_id=client_id,
        can_refresh=document.get("can_refresh") is True,
        secret_values=tuple(value for value in values if isinstance(value, str)),
    )


# --- API-key calls (T76; SPEC §3.2.10 step 7: GET only, the base URL fixed, the key added in-process) --------------
AUTH_PARAM: Final = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,63}")
_PATH_REFUSED: Final = ("://", "\\", "#", "?")


@dataclass(frozen=True, slots=True)
class ApiCredentials:
    """What one API call carries for its key: a header, or a query parameter. Never logged, never in a repr."""

    headers: Mapping[str, str] = field(default_factory=dict, repr=False)
    query: Mapping[str, str] = field(default_factory=dict, repr=False)


def api_auth(*, header_name: str | None, query_param: str | None) -> tuple[AuthKind, str | None]:
    """How a new API key is sent: a named query parameter, a named header, else `Authorization: Bearer`. The name is
    checked (`AUTH_PARAM`); a bad one raises `IntegrationNameInvalid`."""
    for name, kind in ((query_param, AuthKind.QUERY), (header_name, AuthKind.HEADER)):
        if name is None or not name.strip():
            continue
        if not AUTH_PARAM.fullmatch(name.strip()):
            raise IntegrationNameInvalid(name=name.strip()[:64])
        return kind, name.strip()
    return AuthKind.BEARER, None


def api_credentials(auth: AuthKind, *, value: str, name: str | None) -> ApiCredentials:
    if auth is AuthKind.QUERY:
        return ApiCredentials(query={name or "key": value})
    return ApiCredentials(headers=auth_headers(auth, value=value, header_name=name))


def api_url(base: str, path: str, query: Mapping[str, str], credentials: ApiCredentials) -> str | None:
    """`path` under the integration's fixed base URL, with `query` and the key's parameter. None when `path` would
    leave the base (a scheme, `//`, `..`, a backslash, a fragment, or its own query string: use `query`)."""
    raw = path.strip()
    if raw.startswith("//") or any(token in raw for token in _PATH_REFUSED):
        return None
    if any(segment == ".." for segment in raw.split("/")):
        return None
    parts = urlsplit(base)
    joined = parts.path.rstrip("/") + ("/" + raw.lstrip("/") if raw.strip("/") else "")
    params = [*parse_qsl(parts.query, keep_blank_values=True)]
    params += [(key, value) for key, value in query.items() if key not in credentials.query]
    params += list(credentials.query.items())
    return urlunsplit((parts.scheme, parts.netloc, joined or "/", urlencode(params), ""))
