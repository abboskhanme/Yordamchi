"""personal/gateway rules, pure (SPEC §3.2.10 step 7; UC-35; T76): which key a connect tool may use, the arguments it
takes, which integrations and MCP tools are offered, and masking a key that comes back in a result."""

from datetime import UTC, datetime

from yordamchi.core.paths import BotName
from yordamchi.personal.gateway import domain
from yordamchi.personal.gateway.domain import ConnectApi
from yordamchi.shared.integrations import domain as integrations
from yordamchi.shared.integrations.domain import (
    AuthKind,
    IntegrationKind,
    IntegrationStatus,
    IntegrationView,
    ToolEntry,
    ToolPolicy,
)
from yordamchi.shared.vault.domain import SecretRef

KEY = SecretRef(id=7, name="generic_secret", kind="generic_secret", last4="abcd", bots=(BotName.PERSONAL,))
OTHER = SecretRef(id=8, name="google_api_key", kind="google_api_key", last4="wxyz", bots=(BotName.PERSONAL,))


def _view(slug: str, *, kind: IntegrationKind = IntegrationKind.API_HTTP, **changes: object) -> IntegrationView:
    fields: dict[str, object] = {
        "id": 1,
        "slug": slug,
        "display_name": slug.title(),
        "kind": kind,
        "url": "https://api.example.uz/v1",
        "auth": AuthKind.BEARER,
        "bots": (BotName.PERSONAL,),
        "status": IntegrationStatus.OK,
    }
    fields.update(changes)
    return IntegrationView(**fields)  # type: ignore[arg-type]


def test_a_connect_tool_takes_only_a_key_from_the_owners_own_words() -> None:
    assert domain.usable_key("generic_secret", (KEY,)) == KEY
    assert domain.usable_key("[kalit: generic_secret ••••abcd]", (KEY,)) == KEY  # the whole marker, as he wrote it
    assert domain.usable_key("google_api_key", (KEY,)) is None  # in the vault, but not in this message
    assert domain.usable_key("generic_secret", ()) is None


def test_connect_api_arguments_are_checked() -> None:
    parsed = domain.connect_api_of(
        {
            "name": "OpenWeather",
            "base_url": "https://api.openweathermap.org/data/2.5",
            "key": "generic_secret",
            "auth": "query",
            "param": "appid",
            "description": "ob-havo",
            "test_path": "/weather",
            "test_query": {"q": "Tashkent", "units": "metric", "cnt": 1, "full": True},
        }
    )
    assert isinstance(parsed, ConnectApi)
    assert (parsed.auth, parsed.param, parsed.test_path) == ("query", "appid", "/weather")
    assert parsed.test_query == {"q": "Tashkent", "units": "metric", "cnt": "1", "full": "true"}
    base = {"name": "X", "base_url": "https://x.uz", "key": "k", "description": "d"}
    assert domain.connect_api_of({**base, "auth": "basic"}) == "bad_auth"
    assert domain.connect_api_of({**base, "auth": "header"}) == "missing_param"
    assert domain.connect_api_of({**base, "auth": "bearer", "test_query": [1]}) == "bad_test_query"
    assert domain.connect_api_of({"name": "X", "auth": "bearer"}) == "missing_argument"
    bearer = domain.connect_api_of({**base, "auth": "bearer", "param": "ignored"})
    assert isinstance(bearer, ConnectApi)
    assert bearer.param is None


def test_offered_integrations_are_the_personal_bots_working_ones_by_slug() -> None:
    views = (
        _view("zeta"),
        _view("alpha"),
        _view("dev_only", bots=(BotName.DEV,)),
        _view("refused", status=IntegrationStatus.AUTH_FAILED),
        _view("gone", removed_at=datetime(2026, 10, 30, tzinfo=UTC)),
        _view("flaky", status=IntegrationStatus.UNREACHABLE),
    )
    assert [view.slug for view in domain.offered(views)] == ["alpha", "flaky", "zeta"]


def test_only_read_only_or_allowed_mcp_tools_are_callable() -> None:
    view = _view(
        "bonvi_hr",
        kind=IntegrationKind.MCP_HTTP,
        tools=(ToolEntry("employees_list", True), ToolEntry("salary_set", False), ToolEntry("leave_add", False)),
        policy=ToolPolicy(personal=("leave_add",), dev_all=True),
    )
    assert domain.allowed_tools(view) == frozenset({"employees_list", "leave_add"})


def test_a_key_that_comes_back_in_a_result_is_masked() -> None:
    assert domain.mask_secret('{"echo": "s3cr3t-value"}', ["s3cr3t-value", ""]) == '{"echo": "••••"}'


def test_api_calls_stay_under_the_fixed_base_url() -> None:
    query = integrations.api_credentials(AuthKind.QUERY, value="K3Y", name="appid")
    url = integrations.api_url("https://api.example.uz/v1", "/weather", {"q": "Tashkent", "appid": "evil"}, query)
    assert url == "https://api.example.uz/v1/weather?q=Tashkent&appid=K3Y"  # the model cannot override the key
    bearer = integrations.api_credentials(AuthKind.BEARER, value="K3Y", name=None)
    assert bearer.headers == {"Authorization": "Bearer K3Y"}
    assert bearer.query == {}
    assert integrations.api_url("https://api.example.uz/v1?lang=uz", "", {}, bearer) == (
        "https://api.example.uz/v1?lang=uz"
    )
    for escape in ("https://evil.example/x", "//evil.example/x", "../admin", "a/../../b", "x?appid=1", "x#y", "a\\b"):
        assert integrations.api_url("https://api.example.uz/v1", escape, {}, bearer) is None, escape


def test_how_a_new_api_key_is_sent() -> None:
    assert integrations.api_auth(header_name=None, query_param="appid") == (AuthKind.QUERY, "appid")
    assert integrations.api_auth(header_name="X-API-Key", query_param=None) == (AuthKind.HEADER, "X-API-Key")
    assert integrations.api_auth(header_name=None, query_param=None) == (AuthKind.BEARER, None)
