"""integrations.domain, pure: names, URLs, assignment, the paste-back parser, refresh timing, session injection and
the sealed grant document (SPEC §3.2.10; UC-08; NFR-SEC-07; R-43)."""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from yordamchi.core import redact
from yordamchi.core.errors import (
    ErrorCode,
    IntegrationAssignRefused,
    IntegrationNameInvalid,
    IntegrationUrlInvalid,
)
from yordamchi.core.paths import BotName
from yordamchi.ports.mcp import McpTool, OAuthCallback, OAuthGrant
from yordamchi.shared.integrations import domain
from yordamchi.shared.integrations.domain import (
    AuthKind,
    IntegrationKind,
    IntegrationStatus,
    IntegrationView,
    ToolEntry,
    ToolPolicy,
)

NOW = datetime(2026, 10, 24, 9, 0, tzinfo=UTC)
RELEASE = "/Users/owner/Library/Application Support/Yordamchi/releases/current"


def view(slug: str = "bonvi_hr", **changes: object) -> IntegrationView:
    base = IntegrationView(
        id=1,
        slug=slug,
        display_name="Bonvi HR",
        kind=IntegrationKind.MCP_HTTP,
        url=f"https://{slug}.example.uz/mcp",
        auth=AuthKind.BEARER,
        bots=(BotName.DEV,),
        status=IntegrationStatus.OK,
        tools=(ToolEntry("employees_list", read_only=True), ToolEntry("salary_set", read_only=False)),
    )
    return replace(base, **changes)  # type: ignore[arg-type]


# --- names, URLs, assignment ----------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("name", "slug"),
    [
        ("Bonvi HR", "bonvi_hr"),
        ("Wunderkind Maktab", "wunderkind_maktab"),
        ("O'quv markazi", "oquv_markazi"),
        ("Oʻquv  markazi (LC)", "oquv_markazi_lc"),
        ("Café-Ñandú", "cafe_nandu"),
        ("x" * 50, "x" * 31),
    ],
)
def test_a_name_becomes_a_slug(name: str, slug: str) -> None:
    assert domain.slugify(name) == slug


@pytest.mark.parametrize("name", ["", "  ", "1C", "?", "Мактаб", "a"])
def test_a_name_without_a_usable_slug_is_refused(name: str) -> None:
    with pytest.raises(IntegrationNameInvalid):
        domain.slugify(name)


@pytest.mark.parametrize(
    "url", ["https://hr.bonvi.uz/mcp", "https://lc.wunderkind.uz:8443/mcp/", "http://localhost:8001/mcp"]
)
def test_https_and_local_http_urls_are_accepted(url: str) -> None:
    assert domain.check_url(f"  {url} ") == url


@pytest.mark.parametrize(
    "url",
    [
        "http://hr.bonvi.uz/mcp",
        "ftp://hr.bonvi.uz/mcp",
        "https://user:pass@hr.bonvi.uz/mcp",
        "https://token@hr.bonvi.uz/mcp",
        "https:///mcp",
        "https://hr.bonvi.uz/mcp#frag",
        "https://hr.bonvi.uz/m cp",
        "https://hr.bonvi.uz:99999/mcp",
        "hr.bonvi.uz/mcp",
    ],
)
def test_insecure_or_credential_bearing_urls_are_refused(url: str) -> None:
    with pytest.raises(IntegrationUrlInvalid):
        domain.check_url(url)


def test_assignment_defaults_to_the_bot_it_was_said_in() -> None:
    assert domain.assigned_bots(BotName.DEV, both=False) == (BotName.DEV,)
    assert domain.assigned_bots(BotName.PERSONAL, both=False) == (BotName.PERSONAL,)
    assert domain.assigned_bots(BotName.DEV, both=True) == (BotName.DEV, BotName.PERSONAL)


def test_the_personal_bot_adds_for_itself_only() -> None:
    with pytest.raises(IntegrationAssignRefused):
        domain.assigned_bots(BotName.PERSONAL, both=True)
    with pytest.raises(IntegrationAssignRefused):
        domain.check_assignment(BotName.PERSONAL, [BotName.DEV])
    with pytest.raises(IntegrationAssignRefused):
        domain.check_assignment(BotName.DEV, [])
    assert domain.check_assignment(BotName.DEV, [BotName.PERSONAL, BotName.DEV]) == (BotName.DEV, BotName.PERSONAL)
    assert domain.check_assignment(BotName.DEV, [BotName.PERSONAL]) == (BotName.PERSONAL,)


def test_bots_round_trip_through_json_and_ignore_unknown_entries() -> None:
    assert domain.bots_from_json(domain.bots_json([BotName.PERSONAL, BotName.DEV])) == (BotName.DEV, BotName.PERSONAL)
    assert domain.bots_from_json(["dev", "tablet", 3]) == (BotName.DEV,)
    assert domain.bots_from_json("dev") == ()


def test_find_matches_slug_or_display_name_case_insensitively() -> None:
    views = [view(), view("markaz", id=2, display_name="Markaz")]
    assert domain.find(views, "bonvi hr") == views[0]
    assert domain.find(views, "BONVI_HR") == views[0]
    assert domain.find(views, "  Markaz ") == views[1]
    assert domain.find(views, "Мактаб") is None
    assert domain.find(views, "maktab") is None


def test_a_removal_can_be_reverted_for_thirty_days() -> None:
    assert domain.restorable(NOW - timedelta(days=30), NOW)
    assert not domain.restorable(NOW - timedelta(days=30, seconds=1), NOW)
    assert not domain.restorable(None, NOW)


# --- tools ----------------------------------------------------------------------------------------------------------
def test_the_tools_cache_keeps_names_and_read_only_only() -> None:
    tools = [McpTool("a", "secret description", read_only=True, input_schema={"type": "object"})]
    cache = domain.tools_cache(tools)
    assert cache == [{"name": "a", "read_only": True}]
    assert domain.tools_from_cache(cache) == (ToolEntry("a", read_only=True),)
    assert domain.tools_from_cache([{"name": 3}, "x", {"name": "b"}]) == (ToolEntry("b", read_only=False),)
    assert domain.tools_from_cache(None) == ()


def test_the_tool_list_marks_read_only_tools() -> None:
    text = domain.tools_text(view().tools)
    assert text == "employees_list (faqat o'qish), salary_set"
    assert domain.tools_text(()) == "vosita yo'q"


def test_allowed_tools_are_read_only_or_allowed_at_add_time() -> None:
    views = [
        view(),
        view("markaz", id=2, policy=ToolPolicy(dev=("note_add",)), tools=(ToolEntry("note_add", read_only=False),)),
        view("maktab", id=3, policy=ToolPolicy(dev_all=True), tools=(ToolEntry("grade_set", read_only=False),)),
        view("old", id=4, removed_at=NOW),
    ]
    assert domain.allowed_tools(views, BotName.DEV) == {
        "mcp__bonvi_hr__employees_list",
        "mcp__markaz__note_add",
        "mcp__maktab__grade_set",
    }
    assert domain.allowed_tools(views, BotName.PERSONAL) == frozenset()


def test_the_policy_json_is_read_defensively() -> None:
    assert domain.policy_from_json(domain.default_policy_json()) == ToolPolicy()
    assert domain.policy_from_json({"dev": "all", "personal": ["x", 1]}) == ToolPolicy(personal=("x",), dev_all=True)
    assert domain.policy_from_json("all") == ToolPolicy()


# --- statuses -------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("code", "status", "reason"),
    [
        (ErrorCode.AUTH_FAILED, IntegrationStatus.AUTH_FAILED, "auth_failed"),
        (ErrorCode.OAUTH_REAUTH_REQUIRED, IntegrationStatus.NEEDS_REAUTH, "oauth_reauth_required"),
        (ErrorCode.PROVIDER_UNAVAILABLE, IntegrationStatus.UNREACHABLE, "unreachable"),
        (ErrorCode.CALL_TIMED_OUT, IntegrationStatus.UNREACHABLE, "timed_out"),
        (ErrorCode.RATE_LIMITED, IntegrationStatus.UNREACHABLE, "rate_limited"),
        (ErrorCode.VAULT_DECRYPT_FAILED, IntegrationStatus.UNREACHABLE, "secret_unreadable"),
        (ErrorCode.INTERNAL, IntegrationStatus.UNREACHABLE, "internal"),
    ],
)
def test_a_failure_maps_to_a_status_and_a_reason_with_a_fix(
    code: ErrorCode, status: IntegrationStatus, reason: str
) -> None:
    assert domain.status_after(code) is status
    assert domain.reason_of(code) == reason
    why, fix = domain.failure_parts(reason)
    assert why
    assert fix


def test_every_reason_has_a_fix() -> None:
    from yordamchi.core import strings as S

    assert set(S.INTEGRATION_REASONS) == set(S.INTEGRATION_FIXES)


def test_the_list_shows_kind_bots_status_and_the_last_check() -> None:
    views = [
        view(last_validated_at=NOW, bots=(BotName.DEV, BotName.PERSONAL)),
        view(
            "markaz", id=2, display_name="Markaz", kind=IntegrationKind.API_HTTP, status=IntegrationStatus.UNREACHABLE
        ),
    ]
    text = domain.list_text(views, timezone="Asia/Tashkent")
    assert text.splitlines() == [
        "Integratsiyalar:",
        "• Bonvi HR — MCP, ikkala bot: ishlayapti (tekshirildi: 2026-10-24 14:00)",
        "• Markaz — API kalit, dev bot: javob bermayapti (tekshirildi: hali yo'q)",
    ]
    assert "integratsiya yo'q" in domain.list_text([], timezone="Asia/Tashkent")


# --- session injection ----------------------------------------------------------------------------------------------
def test_the_helper_command_is_quoted_for_the_shell() -> None:
    assert domain.helper_command(RELEASE) == f"'{RELEASE}/bin/yordamchi-helper' headers"


def test_dev_executor_sessions_get_the_active_assigned_mcp_servers_only() -> None:
    helper = domain.helper_command(RELEASE)
    views = [
        view(),
        view("unreachable", id=2, status=IntegrationStatus.UNREACHABLE),
        view("expired", id=3, status=IntegrationStatus.NEEDS_REAUTH),
        view("refused", id=4, status=IntegrationStatus.AUTH_FAILED),
        view("removed", id=5, removed_at=NOW),
        view("personal_only", id=6, bots=(BotName.PERSONAL,)),
        view("api_key", id=7, kind=IntegrationKind.API_HTTP),
    ]
    servers = domain.session_servers(views, bot=BotName.DEV, session_kind="coding", helper=helper)
    assert list(servers) == ["bonvi_hr", "unreachable"]
    assert servers["bonvi_hr"].url == "https://bonvi_hr.example.uz/mcp"
    assert servers["bonvi_hr"].headers_helper == helper
    assert domain.session_servers(views, bot=BotName.DEV, session_kind="investigation", helper=helper)
    assert domain.session_servers(views, bot=BotName.DEV, session_kind="smoke", helper=helper) == {}
    assert domain.session_servers(views, bot=BotName.PERSONAL, session_kind="coding", helper=helper) == {}


@pytest.mark.parametrize(
    ("auth", "header_name", "expected"),
    [
        (AuthKind.BEARER, None, {"Authorization": "Bearer v"}),
        (AuthKind.HEADER, "X-API-Key", {"X-API-Key": "v"}),
        (AuthKind.OAUTH, None, {"Authorization": "Bearer v"}),
        (AuthKind.NONE, None, {}),
    ],
)
def test_a_secret_becomes_its_auth_header(auth: AuthKind, header_name: str | None, expected: dict[str, str]) -> None:
    assert domain.auth_headers(auth, value="v", header_name=header_name) == expected


# --- OAuth ----------------------------------------------------------------------------------------------------------
def test_the_redirect_uri_is_localhost_on_the_configured_port() -> None:
    assert domain.redirect_uri(33418) == "http://localhost:33418/callback"


@pytest.mark.parametrize(
    ("pasted", "expected"),
    [
        ("http://localhost:33418/callback?code=abc123&state=st-1", OAuthCallback("abc123", "st-1")),
        (
            "  http://localhost:33418/callback?state=st-1&code=abc123&iss=https%3A%2F%2Fa.uz \n",
            OAuthCallback("abc123", "st-1", iss="https://a.uz"),
        ),
        ("code=abc123&state=st-1", OAuthCallback("abc123", "st-1")),
        ("http://localhost:33418/callback?code=abc123", OAuthCallback("abc123", None)),
        ("4/0AVMBsJh-8xY_z.Q", OAuthCallback("4/0AVMBsJh-8xY_z.Q", None)),
        ("http://localhost:33418/callback?error=access_denied&state=st-1", None),
        ("http://localhost:33418/callback?code=a&code=b", None),
        ("men ruxsat berdim", None),
        ("https://example.uz/page", None),
        ("", None),
        ("abc", None),
    ],
)
def test_the_paste_is_a_redirect_url_its_query_or_a_bare_code(pasted: str, expected: OAuthCallback | None) -> None:
    assert domain.parse_callback(pasted) == expected


def test_a_paste_finds_its_flow_by_state_or_as_the_only_one() -> None:
    flows = {"bonvi_hr": "st-1", "markaz": "st-2"}
    assert domain.match_flow(flows, OAuthCallback("c", "st-2")) == "markaz"
    assert domain.match_flow(flows, OAuthCallback("c", "st-9")) is None
    assert domain.match_flow(flows, OAuthCallback("c")) is None  # two wait: a bare code is ambiguous
    assert domain.match_flow({"bonvi_hr": "st-1"}, OAuthCallback("c")) == "bonvi_hr"
    assert domain.match_flow({}, OAuthCallback("c")) is None


def test_the_state_is_read_from_the_authorization_url() -> None:
    assert domain.state_of("https://a.uz/authorize?client_id=x&state=st-1&code_challenge=y") == "st-1"
    assert domain.state_of("https://a.uz/authorize?client_id=x") is None


def test_refresh_is_due_inside_the_lead_time() -> None:
    expires = domain.expires_at(NOW, 3600)
    assert expires == NOW + timedelta(hours=1)
    assert not domain.refresh_due(expires, NOW, lead_min=10)
    assert not domain.refresh_due(expires, NOW + timedelta(minutes=50), lead_min=10)
    assert domain.refresh_due(expires, NOW + timedelta(minutes=50, seconds=1), lead_min=10)
    assert domain.refresh_due(expires, NOW + timedelta(hours=2), lead_min=10)
    assert domain.expires_at(NOW, None) is None
    assert not domain.refresh_due(None, NOW, lead_min=10)


def test_two_refused_refreshes_need_a_new_sign_in() -> None:
    first = domain.after_refused_refresh(0, limit=2)
    assert (first.failures, first.needs_reauth) == (1, False)
    second = domain.after_refused_refresh(first.failures, limit=2)
    assert (second.failures, second.needs_reauth) == (2, True)


def test_the_grant_document_round_trips_and_names_its_secrets() -> None:
    grant = OAuthGrant(
        state={"tokens": {"access_token": "at-1", "refresh_token": "rt-1"}},
        access_token="at-1",
        expires_in=3600,
        issuer="https://a.uz",
        client_id="client-1",
        can_refresh=True,
        secret_values=("at-1", "rt-1"),
    )
    document = json.loads(json.dumps(domain.grant_document(grant)))
    assert domain.grant_from_document(document, issuer="https://a.uz", client_id="client-1") == grant
    assert domain.grant_from_document({"v": 2}, issuer=None, client_id="c") is None
    assert domain.grant_from_document({"v": 1, "state": {}, "access_token": 3}, issuer=None, client_id="c") is None
    assert domain.grant_aad(7) == b"oauth_clients|7"


def test_the_oauth_link_survives_the_outbound_scrubber() -> None:
    url = (
        "https://hr.bonvi.uz/authorize?response_type=code&client_id=47f2a0e5-6a93-4b84-a6db-b7d1aa101e66"
        "&redirect_uri=http%3A%2F%2Flocalhost%3A33418%2Fcallback&state=aEYrnRjheqcTkQTW9TeZ8ZTpyJKoxCKBhVCttGLTgkM"
        "&code_challenge=RBfEhe5ft4jSb07rA2W5ubUAM0lJyqbh_a7Phsb6zn0&code_challenge_method=S256"
    )
    from yordamchi.core import strings as S

    text = S.OAUTH_LINK(name="Bonvi HR", url=url, port=33418, minutes=15)
    assert url in redact.scrubbed(text)
