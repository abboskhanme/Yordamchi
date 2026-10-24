"""UC-08 end to end on fixtures: `shared/integrations` with the real `McpHttpClient` against the in-process OAuth MCP
server (tests/integration/mcp_harness.py). The owner adds by chat, gets the link, signs in on his phone, pastes the
localhost redirect back (vaulted at intake), and the integration works; the token then refreshes without him, and a
refused refresh tells him once (R-43; SPEC §3.2.10)."""

import re
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import timedelta

import pytest

from tests.factories import Fakes, list_replies, make_owner_origin, outbox_text, vault_secret
from tests.integration.mcp_harness import OAUTH_URL, STATIC_TOKEN, STATIC_URL, McpFixtures, mcp_fixtures
from yordamchi.adapters.mcp.client import McpHttpClient
from yordamchi.core import strings as S
from yordamchi.core.control import HeadersRequest
from yordamchi.core.paths import BotName
from yordamchi.dev.deps import DevDeps
from yordamchi.shared.integrations import service
from yordamchi.shared.integrations.service import AddIntegration, IntegrationsState

TOKEN = "tok-session-3"


@pytest.fixture
async def fixtures() -> AsyncIterator[McpFixtures]:
    async with mcp_fixtures() as running:
        yield running


@pytest.fixture
def deps(dev_deps: DevDeps, fixtures: McpFixtures) -> DevDeps:
    return replace(dev_deps, mcp=McpHttpClient(transport=fixtures.transport))


@pytest.fixture
async def state(deps: DevDeps) -> AsyncIterator[IntegrationsState]:
    flows = IntegrationsState()
    yield flows
    await service.shutdown_integrations(deps, flows)


async def served(deps: DevDeps, server: str) -> dict[str, str] | str:
    answer = await service.serve_headers(deps, HeadersRequest(server, TOKEN), session_known=lambda t: t == TOKEN)
    return answer.error_code or dict(answer.headers)


async def connect_from_the_phone(deps: DevDeps, fixtures: McpFixtures, state: IntegrationsState) -> str:
    reply = await service.add_integration(
        deps, state, make_owner_origin(update_id=20), AddIntegration("Markaz", OAUTH_URL)
    )
    link = re.search(r"https://\S+", reply)
    assert link is not None, reply
    redirect = await fixtures.sign_in(link.group(0))  # the page the phone cannot open
    pasted = await vault_secret(deps, "oauth_callback_url", redirect, update_id=21)
    return await service.complete_oauth(deps, state, make_owner_origin(update_id=21), secret=pasted)


async def test_a_static_server_is_validated_through_the_real_adapter(deps: DevDeps, state: IntegrationsState) -> None:
    secret = await vault_secret(deps, "generic_secret", STATIC_TOKEN)
    reply = await service.add_integration(
        deps, state, make_owner_origin(), AddIntegration("Bonvi HR", STATIC_URL, secret=secret)
    )
    assert reply == S.INTEGRATION_ADDED(name="Bonvi HR", bots="dev bot", tools="read_status (faqat o'qish), write_note")


async def test_oauth_from_the_phone_then_refresh_then_one_reauth_message(
    deps: DevDeps, fakes: Fakes, fixtures: McpFixtures, state: IntegrationsState
) -> None:
    done = await connect_from_the_phone(deps, fixtures, state)

    assert done == S.INTEGRATION_ADDED(name="Markaz", bots="dev bot", tools="read_status (faqat o'qish), write_note")
    first = await served(deps, "markaz")
    assert isinstance(first, dict)
    assert first["Authorization"].startswith("Bearer at-")
    assert await deps.mcp.list_tools(OAUTH_URL, first, timeout=10)  # what the session's helper hands Claude works
    assert list(await service.mcp_config_for(deps, BotName.DEV, "coding")) == ["markaz"]

    fakes.clock.set(fakes.clock.now() + timedelta(minutes=55))
    assert await service.refresh_tokens(deps) == 1
    second = await served(deps, "markaz")
    assert isinstance(second, dict)
    assert second != first
    assert fixtures.authorization.refreshes == 1
    assert await deps.mcp.list_tools(OAUTH_URL, second, timeout=10)
    assert await list_replies(deps.db) == []

    fixtures.authorization.revoke_refresh_tokens()
    fakes.clock.set(fakes.clock.now() + timedelta(minutes=55))
    await service.refresh_tokens(deps)
    await service.refresh_tokens(deps)
    await service.refresh_tokens(deps)

    (notice,) = await list_replies(deps.db)
    assert outbox_text(notice) == S.OAUTH_REAUTH(name="Markaz")
    assert await served(deps, "markaz") == "needs_reauth"

    again = await connect_from_the_phone(deps, fixtures, state)  # "Markaz'ga qayta ulan"
    assert again.startswith("«Markaz» qayta ulandi.")
    assert isinstance(await served(deps, "markaz"), dict)
