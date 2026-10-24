"""integrations.service on a real SQLite file with the fake MCP client (SPEC §3.2.10; UC-08 and its ACs; R-43):
add / validate / list / rename / assign / remove / restore by chat, the OAuth paste-back flow from the phone, token
refresh and its one failure message, the `headers` socket method, and session injection that holds no secret and
reads nothing but Yordamchi's own registry (the account-rotation AC)."""

import asyncio
import json
import re
import shlex
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from tests.factories import (
    INTEGRATION_OAUTH_URL,
    INTEGRATION_TOKEN,
    INTEGRATION_URL,
    Fakes,
    list_replies,
    make_mcp_tools,
    make_owner_origin,
    outbox_text,
    vault_secret,
)
from tests.owner_text import shown
from yordamchi.adapters.claude import options
from yordamchi.core import aio, redact
from yordamchi.core import strings as S
from yordamchi.core.control import HeadersRequest
from yordamchi.core.errors import (
    IntegrationAssignRefused,
    IntegrationExists,
    IntegrationFailed,
    IntegrationNameInvalid,
    IntegrationNotFound,
    IntegrationUrlInvalid,
    OAuthCallbackInvalid,
    OAuthNotPending,
    VaultSecretNotFound,
    YordamchiError,
)
from yordamchi.core.paths import BotName
from yordamchi.dev.deps import DevDeps
from yordamchi.personal.deps import PersonalDeps
from yordamchi.ports.agent import GuardVerdict, SessionSpec, ToolCall
from yordamchi.ports.mac_host import ClaudeAccount, ClaudeManagerState, InteractiveSession
from yordamchi.shared.audit import store as audit_store
from yordamchi.shared.deps import SharedDeps
from yordamchi.shared.integrations import service
from yordamchi.shared.integrations.domain import AuthKind, IntegrationKind, IntegrationStatus
from yordamchi.shared.integrations.models import Integration, OAuthClient
from yordamchi.shared.integrations.service import AddApiKey, AddIntegration, IntegrationsState
from yordamchi.shared.vault import service as vault

TOKEN = "tok-session-1"
BEARER = {"Authorization": f"Bearer {INTEGRATION_TOKEN}"}


@pytest.fixture
async def state(dev_deps: DevDeps) -> AsyncIterator[IntegrationsState]:
    flows = IntegrationsState()
    yield flows
    await service.shutdown_integrations(dev_deps, flows)


def known(token: str) -> bool:
    return token == TOKEN


def link_in(text: str) -> str:
    match = re.search(r"https://\S+", text)
    assert match is not None, text
    return match.group(0)


async def add_static(
    deps: SharedDeps, fakes: Fakes, state: IntegrationsState, *, name: str = "Bonvi HR", both: bool = False
) -> str:
    fakes.mcp.add_server(INTEGRATION_URL, make_mcp_tools(), headers=BEARER)
    ref = await vault_secret(deps, "generic_secret", INTEGRATION_TOKEN)
    request = AddIntegration(name=name, url=INTEGRATION_URL, secret=ref, both=both)
    return await service.add_integration(deps, state, make_owner_origin(deps.bot), request)


async def start_oauth(deps: SharedDeps, fakes: Fakes, state: IntegrationsState, *, name: str = "Markaz") -> str:
    if INTEGRATION_OAUTH_URL not in fakes.mcp.oauth_servers:
        fakes.mcp.add_oauth_server(INTEGRATION_OAUTH_URL, make_mcp_tools())
    request = AddIntegration(name=name, url=INTEGRATION_OAUTH_URL)
    return await service.add_integration(deps, state, make_owner_origin(deps.bot, update_id=10), request)


async def add_oauth(deps: SharedDeps, fakes: Fakes, state: IntegrationsState, *, name: str = "Markaz") -> str:
    """The whole phone flow: the link, the owner's sign-in, his paste (vaulted by intake), the confirmation."""
    reply = await start_oauth(deps, fakes, state, name=name)
    redirect = fakes.mcp.sign_in(link_in(reply))
    pasted = await vault_secret(deps, "oauth_callback_url", redirect, update_id=11)
    return await service.complete_oauth(deps, state, make_owner_origin(deps.bot, update_id=11), secret=pasted)


async def rows(deps: SharedDeps) -> list[Integration]:
    async with deps.db.read() as s:
        return list((await s.scalars(select(Integration).order_by(Integration.id))).all())


async def oauth_client(deps: SharedDeps) -> OAuthClient:
    async with deps.db.read() as s:
        client: OAuthClient | None = await s.scalar(select(OAuthClient))
    assert client is not None
    return client


async def headers(deps: SharedDeps, server: str) -> dict[str, str] | str:
    answer = await service.serve_headers(deps, HeadersRequest(server, TOKEN), session_known=known)
    return answer.error_code or dict(answer.headers)


def failure_text(error: YordamchiError) -> str:
    return S.ERRORS[error.code](**error.params)


# --- add, validate, list ----------------------------------------------------------------------------------------------
async def test_a_static_mcp_server_is_validated_stored_and_confirmed(
    dev_deps: DevDeps, fakes: Fakes, state: IntegrationsState
) -> None:
    reply = await add_static(dev_deps, fakes, state)

    assert reply == S.INTEGRATION_ADDED(
        name="Bonvi HR", bots="dev bot", tools="employees_list (faqat o'qish), salary_set"
    )
    (row,) = await rows(dev_deps)
    assert (row.slug, row.kind, row.auth, row.status) == ("bonvi_hr", "mcp_http", "bearer", "ok")
    assert row.last_validated_at == fakes.clock.now()
    assert INTEGRATION_TOKEN not in json.dumps([row.url, row.tools_cache_json, row.bots_json, row.tool_policy_json])
    assert fakes.mcp.list_calls == [(INTEGRATION_URL, BEARER)]
    async with dev_deps.db.read() as s:
        events = await audit_store.list_audit_events(s, kind="integration_changed")
    assert [event.data_json for event in events] == [
        {"action": "added", "auth": "bearer", "kind": "mcp_http", "origin_update_id": 1, "slug": "bonvi_hr"}
    ]


async def test_the_list_by_chat_shows_status_and_last_check(
    dev_deps: DevDeps, fakes: Fakes, state: IntegrationsState
) -> None:
    await add_static(dev_deps, fakes, state)

    listing = await service.show_integrations(dev_deps)

    assert shown(listing).splitlines()[1] == "• Bonvi HR — MCP, dev bot: ishlayapti (tekshirildi: 2026-10-23 14:00)"
    assert INTEGRATION_TOKEN not in listing


async def test_an_empty_list_says_how_to_add(dev_deps: DevDeps) -> None:
    assert await service.show_integrations(dev_deps) == S.INTEGRATION_LIST_EMPTY()


async def test_a_refused_token_names_the_integration_and_the_fix(
    dev_deps: DevDeps, fakes: Fakes, state: IntegrationsState
) -> None:
    fakes.mcp.add_server(INTEGRATION_URL, make_mcp_tools(), headers={"Authorization": "Bearer other"})
    ref = await vault_secret(dev_deps, "generic_secret", INTEGRATION_TOKEN)

    with pytest.raises(IntegrationFailed) as raised:
        await service.add_integration(
            dev_deps, state, make_owner_origin(), AddIntegration("Bonvi HR", INTEGRATION_URL, secret=ref)
        )

    assert raised.value.reason_code == "auth_failed"
    text = failure_text(raised.value)
    assert text.startswith("«Bonvi HR» ulanmadi: kalit yoki token rad etildi.")
    assert "Yangi kalit yuboring" in text
    assert await rows(dev_deps) == []


async def test_an_unreachable_server_is_a_clear_message_and_nothing_is_stored(
    dev_deps: DevDeps, state: IntegrationsState
) -> None:
    with pytest.raises(IntegrationFailed) as raised:
        await service.add_integration(dev_deps, state, make_owner_origin(), AddIntegration("Bonvi HR", INTEGRATION_URL))

    assert raised.value.reason_code == "unreachable"
    assert "server javob bermayapti" in failure_text(raised.value)
    assert await rows(dev_deps) == []


async def test_a_token_given_to_an_oauth_server_is_reported_as_refused(
    dev_deps: DevDeps, fakes: Fakes, state: IntegrationsState
) -> None:
    fakes.mcp.add_oauth_server(INTEGRATION_OAUTH_URL, make_mcp_tools())
    ref = await vault_secret(dev_deps, "generic_secret", INTEGRATION_TOKEN)
    with pytest.raises(IntegrationFailed) as raised:
        await service.add_integration(
            dev_deps, state, make_owner_origin(), AddIntegration("Markaz", INTEGRATION_OAUTH_URL, secret=ref)
        )
    assert raised.value.reason_code == "auth_failed"
    assert state.flows == {}


async def test_a_working_integration_is_not_added_twice(
    dev_deps: DevDeps, fakes: Fakes, state: IntegrationsState
) -> None:
    await add_static(dev_deps, fakes, state)
    with pytest.raises(IntegrationExists):
        await service.add_integration(dev_deps, state, make_owner_origin(), AddIntegration("bonvi hr", INTEGRATION_URL))


async def test_a_new_token_reconnects_a_failing_integration_in_place(
    dev_deps: DevDeps, fakes: Fakes, state: IntegrationsState
) -> None:
    await add_static(dev_deps, fakes, state)
    fakes.mcp.required_headers[INTEGRATION_URL] = {"Authorization": "Bearer rotated-token-0123456789"}
    with pytest.raises(IntegrationFailed):
        await service.check_integration(dev_deps, "Bonvi HR")
    assert (await rows(dev_deps))[0].status == "auth_failed"

    ref = await vault_secret(dev_deps, "generic_secret", "rotated-token-0123456789", update_id=901)
    reply = await service.add_integration(
        dev_deps, state, make_owner_origin(), AddIntegration("Bonvi HR", INTEGRATION_URL, secret=ref)
    )

    assert reply.startswith("«Bonvi HR» qayta ulandi.")
    (row,) = await rows(dev_deps)
    assert (row.status, row.secret_id) == ("ok", ref.id)


async def test_bad_names_urls_and_assignments_are_refused_before_any_call(
    dev_deps: DevDeps, personal_deps: PersonalDeps, fakes: Fakes, state: IntegrationsState
) -> None:
    with pytest.raises(IntegrationNameInvalid):
        await service.add_integration(dev_deps, state, make_owner_origin(), AddIntegration("1C", INTEGRATION_URL))
    with pytest.raises(IntegrationUrlInvalid):
        await service.add_integration(dev_deps, state, make_owner_origin(), AddIntegration("HR", "http://hr.uz/mcp"))
    with pytest.raises(IntegrationAssignRefused):
        await service.add_integration(
            personal_deps,
            state,
            make_owner_origin(BotName.PERSONAL),
            AddIntegration("HR", INTEGRATION_URL, both=True),
        )
    assert fakes.mcp.list_calls == []


async def test_an_owner_origin_of_the_other_bot_changes_nothing(dev_deps: DevDeps, state: IntegrationsState) -> None:
    with pytest.raises(ValueError, match="cannot change"):
        await service.add_integration(
            dev_deps, state, make_owner_origin(BotName.PERSONAL), AddIntegration("HR", INTEGRATION_URL)
        )


# --- rename, assign, remove, restore ----------------------------------------------------------------------------------
async def test_rename_changes_the_name_and_the_session_server_name(
    dev_deps: DevDeps, fakes: Fakes, state: IntegrationsState
) -> None:
    await add_static(dev_deps, fakes, state)

    reply = await service.rename_integration(dev_deps, make_owner_origin(), "bonvi hr", "Kadrlar")

    assert reply == S.INTEGRATION_RENAMED(old="Bonvi HR", name="Kadrlar")
    assert list(await service.mcp_config_for(dev_deps, BotName.DEV, "coding")) == ["kadrlar"]
    assert await headers(dev_deps, "kadrlar") == BEARER


async def test_rename_onto_another_integration_is_refused(
    dev_deps: DevDeps, fakes: Fakes, state: IntegrationsState
) -> None:
    await add_static(dev_deps, fakes, state)
    await add_oauth(dev_deps, fakes, state)
    with pytest.raises(IntegrationExists):
        await service.rename_integration(dev_deps, make_owner_origin(), "Bonvi HR", "markaz")


async def test_the_dev_bot_assigns_to_both_and_the_personal_bot_keeps_its_own(
    dev_deps: DevDeps, personal_deps: PersonalDeps, fakes: Fakes, state: IntegrationsState
) -> None:
    await add_static(dev_deps, fakes, state)
    reply = await service.assign_integration(dev_deps, make_owner_origin(), "Bonvi HR", [BotName.DEV, BotName.PERSONAL])
    assert reply == S.INTEGRATION_ASSIGNED(name="Bonvi HR", bots="ikkala bot")
    assert (await rows(dev_deps))[0].bots_json == ["dev", "personal"]

    await add_static(personal_deps, fakes, state, name="Shaxsiy HR")
    with pytest.raises(IntegrationAssignRefused):
        await service.assign_integration(
            personal_deps, make_owner_origin(BotName.PERSONAL), "Shaxsiy HR", [BotName.DEV]
        )


async def test_a_removed_integration_is_absent_from_the_next_session_and_restorable(
    dev_deps: DevDeps, fakes: Fakes, state: IntegrationsState
) -> None:
    await add_static(dev_deps, fakes, state)
    assert list(await service.mcp_config_for(dev_deps, BotName.DEV, "coding")) == ["bonvi_hr"]

    reply = await service.remove_integration(dev_deps, state, make_owner_origin(), "Bonvi HR")

    assert reply == S.INTEGRATION_REMOVED(name="Bonvi HR")
    assert await service.mcp_config_for(dev_deps, BotName.DEV, "coding") == {}
    assert await headers(dev_deps, "bonvi_hr") == "integration_unknown"
    assert await service.show_integrations(dev_deps) == S.INTEGRATION_LIST_EMPTY()

    await fakes.clock.advance(29 * 24 * 3600)
    assert await service.restore_integration(dev_deps, make_owner_origin(), "bonvi hr") == S.INTEGRATION_RESTORED(
        name="Bonvi HR"
    )
    assert list(await service.mcp_config_for(dev_deps, BotName.DEV, "coding")) == ["bonvi_hr"]


async def test_a_removal_older_than_thirty_days_cannot_be_restored(
    dev_deps: DevDeps, fakes: Fakes, state: IntegrationsState
) -> None:
    await add_static(dev_deps, fakes, state)
    await service.remove_integration(dev_deps, state, make_owner_origin(), "Bonvi HR")
    fakes.clock.set(fakes.clock.now() + timedelta(days=31))
    with pytest.raises(IntegrationNotFound):
        await service.restore_integration(dev_deps, make_owner_origin(), "Bonvi HR")


async def test_removing_or_renaming_an_unknown_name_says_so(dev_deps: DevDeps, state: IntegrationsState) -> None:
    with pytest.raises(IntegrationNotFound) as raised:
        await service.remove_integration(dev_deps, state, make_owner_origin(), "Maktab")
    assert failure_text(raised.value).startswith("«Maktab» degan integratsiya yo'q.")
    with pytest.raises(IntegrationNotFound):
        await service.rename_integration(dev_deps, make_owner_origin(), "Maktab", "X")


async def test_a_removed_name_can_be_added_again(dev_deps: DevDeps, fakes: Fakes, state: IntegrationsState) -> None:
    await add_static(dev_deps, fakes, state)
    await service.remove_integration(dev_deps, state, make_owner_origin(), "Bonvi HR")
    await add_static(dev_deps, fakes, state)
    assert [(row.slug, row.removed_at is None) for row in await rows(dev_deps)] == [
        ("bonvi_hr", False),
        ("bonvi_hr", True),
    ]


async def test_a_check_records_the_status_and_the_time(
    dev_deps: DevDeps, fakes: Fakes, state: IntegrationsState
) -> None:
    await add_static(dev_deps, fakes, state)
    await fakes.clock.advance(3600)
    fakes.mcp.unreachable.add(INTEGRATION_URL)

    with pytest.raises(IntegrationFailed) as raised:
        await service.check_integration(dev_deps, "Bonvi HR")
    assert raised.value.reason_code == "unreachable"
    (row,) = await rows(dev_deps)
    assert (row.status, row.status_code, row.last_validated_at) == (
        "unreachable",
        "provider_unavailable",
        fakes.clock.now(),
    )

    fakes.mcp.unreachable.clear()
    assert (await service.check_integration(dev_deps, "Bonvi HR")).startswith("«Bonvi HR»: ishlayapti.")
    assert (await rows(dev_deps))[0].status == "ok"


# --- plain API keys ---------------------------------------------------------------------------------------------------
async def test_an_api_key_is_stored_with_its_url_and_purpose_and_listed(dev_deps: DevDeps) -> None:
    key = await vault_secret(dev_deps, "generic_secret", "weather-key-" + "Z9y8X7w6V5u4T3s2")

    reply = await service.add_api_key(
        dev_deps,
        make_owner_origin(),
        AddApiKey(
            name="Ob-havo",
            base_url="https://api.weather.example.uz/v1",
            description="Toshkent ob-havosi",
            secret=key,
            header_name="X-API-Key",
        ),
    )

    assert reply == S.API_KEY_ADDED(name="Ob-havo", bots="dev bot", url="https://api.weather.example.uz/v1")
    (row,) = await rows(dev_deps)
    assert (row.kind, row.auth, row.header_name, row.secret_id, row.description) == (
        "api_http",
        "header",
        "X-API-Key",
        key.id,
        "Toshkent ob-havosi",
    )
    assert "• Ob-havo — API kalit, dev bot" in shown(await service.show_integrations(dev_deps))
    assert await service.mcp_config_for(dev_deps, BotName.DEV, "coding") == {}  # not an MCP server
    with pytest.raises(IntegrationExists):
        await service.add_api_key(
            dev_deps, make_owner_origin(), AddApiKey("Ob-havo", "https://api.weather.example.uz/v1", "", key)
        )


# --- OAuth from the phone (R-43) --------------------------------------------------------------------------------------
async def test_the_paste_back_flow_completes_from_the_phone(
    dev_deps: DevDeps, fakes: Fakes, state: IntegrationsState
) -> None:
    reply = await start_oauth(dev_deps, fakes, state)

    assert reply.startswith("«Markaz» uchun kirish kerak. Havolani oching, ruxsat bering:")
    assert "http://localhost:33418/" in reply
    assert "15 daqiqa kutaman" in reply
    assert "redirect_uri=http%3A%2F%2Flocalhost%3A33418%2Fcallback" in link_in(reply)
    assert await rows(dev_deps) == []  # nothing is stored before the token exchange

    redirect = fakes.mcp.sign_in(link_in(reply))
    pasted = await vault_secret(dev_deps, "oauth_callback_url", redirect, update_id=11)
    done = await service.complete_oauth(dev_deps, state, make_owner_origin(update_id=11), secret=pasted)

    assert done == S.INTEGRATION_ADDED(name="Markaz", bots="dev bot", tools="employees_list (faqat o'qish), salary_set")
    (row,) = await rows(dev_deps)
    assert (row.slug, row.auth, row.status) == ("markaz", "oauth", "ok")
    client = await oauth_client(dev_deps)
    assert client.ciphertext is not None
    assert b"at-0002-fixture" not in client.ciphertext
    assert client.expires_at == fakes.clock.now() + timedelta(hours=1)
    assert await headers(dev_deps, "markaz") == {"Authorization": "Bearer at-0002-fixture"}
    assert state.flows == {}
    with pytest.raises(VaultSecretNotFound):  # the single-use code is not kept
        await vault.open_secret(dev_deps, pasted.name)
    assert redact.scrubbed("at-0002-fixture rt-0002-fixture") == "••••ture ••••ture"
    assert await list_replies(dev_deps.db) == []  # the outcome was the paste's reply, not a second message


async def test_pasting_only_the_code_completes_the_single_waiting_flow(
    dev_deps: DevDeps, fakes: Fakes, state: IntegrationsState
) -> None:
    reply = await start_oauth(dev_deps, fakes, state)
    code = re.search(r"code=([^&]+)", fakes.mcp.sign_in(link_in(reply)))
    assert code is not None

    done = await service.complete_oauth(dev_deps, state, make_owner_origin(update_id=12), pasted=code.group(1))

    assert done.startswith("«Markaz» ulandi")


async def test_a_paste_without_a_waiting_flow_or_without_a_code_is_explained(
    dev_deps: DevDeps, fakes: Fakes, state: IntegrationsState
) -> None:
    with pytest.raises(OAuthNotPending) as not_pending:
        await service.complete_oauth(
            dev_deps, state, make_owner_origin(), pasted="http://localhost:33418/callback?code=x1y2&state=s"
        )
    assert "kutmayapti" in failure_text(not_pending.value)

    await start_oauth(dev_deps, fakes, state)
    with pytest.raises(OAuthCallbackInvalid):
        await service.complete_oauth(
            dev_deps, state, make_owner_origin(), pasted="http://localhost:33418/callback?error=access_denied"
        )
    with pytest.raises(OAuthNotPending):  # a state that no flow issued
        await service.complete_oauth(
            dev_deps, state, make_owner_origin(), pasted="http://localhost:33418/callback?code=x1y2&state=forged"
        )
    assert "markaz" in state.flows


async def test_no_paste_within_the_window_tells_the_owner_once(
    dev_deps: DevDeps, fakes: Fakes, state: IntegrationsState
) -> None:
    await start_oauth(dev_deps, fakes, state)
    task = state.flows["markaz"].task
    assert task is not None

    await fakes.clock.advance(15 * 60)
    await asyncio.wait_for(task, timeout=5)

    (notice,) = await list_replies(dev_deps.db)
    assert outbox_text(notice) == S.ERR_OAUTH_PASTE_TIMED_OUT(name="Markaz", minutes=15)
    assert state.flows == {}
    assert await rows(dev_deps) == []


async def test_a_server_that_needs_a_client_metadata_document_says_which_setting(
    dev_deps: DevDeps, fakes: Fakes, state: IntegrationsState
) -> None:
    fakes.mcp.add_oauth_server(INTEGRATION_OAUTH_URL, make_mcp_tools(), needs_client_metadata=True)

    with pytest.raises(IntegrationFailed) as raised:
        await start_oauth(dev_deps, fakes, state)

    assert raised.value.reason_code == "oauth_client_metadata_needed"
    assert "integrations.oauth_client_metadata_url" in failure_text(raised.value)
    assert state.flows == {}


async def test_asking_again_replaces_the_waiting_flow(
    dev_deps: DevDeps, fakes: Fakes, state: IntegrationsState
) -> None:
    first = await start_oauth(dev_deps, fakes, state)
    second = await start_oauth(dev_deps, fakes, state)
    assert link_in(first) != link_in(second)

    stale = fakes.mcp.sign_in(link_in(first))
    with pytest.raises(OAuthNotPending):
        await service.complete_oauth(dev_deps, state, make_owner_origin(), pasted=stale)
    done = await service.complete_oauth(dev_deps, state, make_owner_origin(), pasted=fakes.mcp.sign_in(link_in(second)))
    assert done.startswith("«Markaz» ulandi")


# --- token refresh ----------------------------------------------------------------------------------------------------
async def test_a_token_is_refreshed_inside_the_lead_time_only(
    dev_deps: DevDeps, fakes: Fakes, state: IntegrationsState
) -> None:
    await add_oauth(dev_deps, fakes, state)
    assert await service.refresh_tokens(dev_deps) == 0
    assert fakes.mcp.refreshes == []

    fakes.clock.set(fakes.clock.now() + timedelta(minutes=51))
    assert await service.refresh_tokens(dev_deps) == 1

    assert await headers(dev_deps, "markaz") == {"Authorization": "Bearer at-0003-fixture"}
    client = await oauth_client(dev_deps)
    assert client.last_refresh_at == fakes.clock.now()
    assert client.expires_at == fakes.clock.now() + timedelta(hours=1)
    assert await service.refresh_tokens(dev_deps) == 0  # the new grant is not due yet
    assert await list_replies(dev_deps.db) == []  # a refresh needs nothing from the owner


async def test_two_refused_refreshes_tell_the_owner_once_and_reauth_fixes_it(
    dev_deps: DevDeps, fakes: Fakes, state: IntegrationsState
) -> None:
    await add_oauth(dev_deps, fakes, state)
    fakes.mcp.oauth_servers[INTEGRATION_OAUTH_URL].refuse_refresh = True
    fakes.clock.set(fakes.clock.now() + timedelta(minutes=55))

    await service.refresh_tokens(dev_deps)
    assert (await rows(dev_deps))[0].status == "ok"
    assert await list_replies(dev_deps.db) == []

    await service.refresh_tokens(dev_deps)
    (row,) = await rows(dev_deps)
    assert (row.status, row.status_code) == ("needs_reauth", "oauth_reauth_required")
    (notice,) = await list_replies(dev_deps.db)
    assert outbox_text(notice) == S.OAUTH_REAUTH(name="Markaz")

    await service.refresh_tokens(dev_deps)
    assert len(fakes.mcp.refreshes) == 2  # a dead grant is not retried
    assert len(await list_replies(dev_deps.db)) == 1  # told once until fixed
    assert await headers(dev_deps, "markaz") == "needs_reauth"
    assert await service.mcp_config_for(dev_deps, BotName.DEV, "coding") == {}
    assert "qayta kirish kerak" in await service.show_integrations(dev_deps)

    fakes.mcp.oauth_servers[INTEGRATION_OAUTH_URL].refuse_refresh = False
    reconnected = await add_oauth(dev_deps, fakes, state)
    assert reconnected.startswith("«Markaz» qayta ulandi.")
    client = await oauth_client(dev_deps)
    assert (client.refresh_failures, client.reauth_told_at) == (0, None)
    assert list(await service.mcp_config_for(dev_deps, BotName.DEV, "coding")) == ["markaz"]
    assert len(await rows(dev_deps)) == 1


async def test_an_unreachable_token_endpoint_is_not_a_refusal(
    dev_deps: DevDeps, fakes: Fakes, state: IntegrationsState
) -> None:
    await add_oauth(dev_deps, fakes, state)
    fakes.clock.set(fakes.clock.now() + timedelta(minutes=55))
    fakes.mcp.unreachable.add(INTEGRATION_OAUTH_URL)

    for _ in range(3):
        assert await service.refresh_tokens(dev_deps) == 0

    (row,) = await rows(dev_deps)
    assert row.status == "unreachable"
    assert (await oauth_client(dev_deps)).refresh_failures == 0
    assert await list_replies(dev_deps.db) == []
    fakes.mcp.unreachable.clear()
    assert await service.refresh_tokens(dev_deps) == 1
    assert (await rows(dev_deps))[0].status == "ok"


async def test_the_refresher_loop_runs_on_the_setting_interval(
    dev_deps: DevDeps, fakes: Fakes, state: IntegrationsState
) -> None:
    await add_oauth(dev_deps, fakes, state)
    loop = aio.spawn("integrations_refresh", service.run_token_refresher(dev_deps))
    try:
        for _ in range(200):
            if fakes.clock.pending_sleepers:
                break
            await asyncio.sleep(0.01)
        assert fakes.clock.sleeps[-1] == 120.0
        fakes.clock.set(fakes.clock.now() + timedelta(minutes=50))
        await fakes.clock.advance(120)
        for _ in range(50):
            if fakes.mcp.refreshes:
                break
            await asyncio.sleep(0.01)
        assert fakes.mcp.refreshes == [INTEGRATION_OAUTH_URL]
    finally:
        loop.cancel()
        await asyncio.gather(loop, return_exceptions=True)


async def test_publishing_grants_masks_every_stored_token(
    dev_deps: DevDeps, fakes: Fakes, state: IntegrationsState
) -> None:
    await add_oauth(dev_deps, fakes, state)
    redact.set_known_secrets([])

    assert await service.publish_grants(dev_deps) == 1

    assert "at-0002-fixture" not in redact.scrubbed("token at-0002-fixture")
    assert "rt-0002-fixture" not in redact.scrubbed("token rt-0002-fixture")


# --- the headers socket method ----------------------------------------------------------------------------------------
async def test_headers_go_only_to_a_live_session_for_an_assigned_server(
    dev_deps: DevDeps, personal_deps: PersonalDeps, fakes: Fakes, state: IntegrationsState
) -> None:
    await add_static(dev_deps, fakes, state)

    assert await headers(dev_deps, "bonvi_hr") == BEARER
    stranger = await service.serve_headers(dev_deps, HeadersRequest("bonvi_hr", "forged"), session_known=known)
    assert (stranger.error_code, dict(stranger.headers)) == ("session_unknown", {})
    assert await headers(dev_deps, "maktab") == "integration_unknown"

    await service.assign_integration(dev_deps, make_owner_origin(), "Bonvi HR", [BotName.PERSONAL])
    assert await headers(dev_deps, "bonvi_hr") == "integration_unknown"


async def test_a_named_header_and_an_unreadable_secret(
    dev_deps: DevDeps, fakes: Fakes, state: IntegrationsState
) -> None:
    fakes.mcp.add_server(INTEGRATION_URL, make_mcp_tools(), headers={"X-API-Key": INTEGRATION_TOKEN})
    ref = await vault_secret(dev_deps, "generic_secret", INTEGRATION_TOKEN)
    await service.add_integration(
        dev_deps,
        state,
        make_owner_origin(),
        AddIntegration("Bonvi HR", INTEGRATION_URL, secret=ref, header_name="X-API-Key"),
    )
    assert await headers(dev_deps, "bonvi_hr") == {"X-API-Key": INTEGRATION_TOKEN}

    await vault.remove_secret(dev_deps, make_owner_origin(update_id=77), ref.name)
    assert await headers(dev_deps, "bonvi_hr") == "vault_secret_not_found"


async def test_a_server_without_auth_gets_empty_headers(
    dev_deps: DevDeps, fakes: Fakes, state: IntegrationsState
) -> None:
    fakes.mcp.add_server(INTEGRATION_URL, make_mcp_tools())
    await service.add_integration(dev_deps, state, make_owner_origin(), AddIntegration("Ochiq", INTEGRATION_URL))
    assert await headers(dev_deps, "ochiq") == {}
    assert (await rows(dev_deps))[0].auth == AuthKind.NONE.value


# --- session injection: no secret on argv, nothing account-bound ------------------------------------------------------
async def _spec(deps: DevDeps) -> SessionSpec:
    async def allow(_call: ToolCall) -> GuardVerdict:
        return GuardVerdict(allow=True)

    servers = await service.mcp_config_for(deps, BotName.DEV, "coding")
    return SessionSpec(kind="coding", cwd=Path("/tmp/project"), model="opus", guard=allow, mcp_servers=servers)


async def test_the_session_config_holds_no_secret(dev_deps: DevDeps, fakes: Fakes, state: IntegrationsState) -> None:
    await add_static(dev_deps, fakes, state)
    await add_oauth(dev_deps, fakes, state)

    spec = await _spec(dev_deps)
    serialized = json.dumps(options.mcp_config(spec, {}))  # what `--mcp-config` puts on the command line

    helper = shlex.quote(f"{dev_deps.paths.current_release}/bin/yordamchi-helper") + " headers"
    assert json.loads(serialized) == {
        "bonvi_hr": {"type": "http", "url": INTEGRATION_URL, "headersHelper": helper},
        "markaz": {"type": "http", "url": INTEGRATION_OAUTH_URL, "headersHelper": helper},
    }
    for secret in (INTEGRATION_TOKEN, "at-0002-fixture", "rt-0002-fixture", "Bearer", "Authorization"):
        assert secret not in serialized


async def test_only_dev_executor_sessions_get_remote_servers(
    dev_deps: DevDeps, fakes: Fakes, state: IntegrationsState
) -> None:
    await add_static(dev_deps, fakes, state, both=True)
    assert list(await service.mcp_config_for(dev_deps, BotName.DEV, "investigation")) == ["bonvi_hr"]
    assert await service.mcp_config_for(dev_deps, BotName.DEV, "smoke") == {}
    assert await service.mcp_config_for(dev_deps, BotName.PERSONAL, "coding") == {}  # the gateway is T76


class AccountsUnreadable:
    """A MacHost that fails every call: a config that still builds was never derived from the Claude account."""

    async def memory_pressure_level(self, *, timeout: float) -> int:
        raise AssertionError("integrations must not read the host")

    async def free_disk_bytes(self, path: Path, *, timeout: float) -> int:
        raise AssertionError("integrations must not read the host")

    async def interactive_sessions(self, *, timeout: float) -> list[InteractiveSession]:
        raise AssertionError("integrations must not read the host")

    async def claude_manager_state(self, *, timeout: float) -> ClaudeManagerState | None:
        raise AssertionError("integrations must not read the Claude account")


def account(active: str) -> ClaudeManagerState:
    accounts = tuple(
        ClaudeAccount(
            email=f"{name}@example.uz",
            status="ok",
            five_hour_pct=10.0,
            seven_day_pct=5.0,
            five_hour_resets_at=None,
            seven_day_resets_at=None,
            is_active=name == active,
        )
        for name in ("a", "b")
    )
    return ClaudeManagerState(active_id=active, auto_switch=True, accounts=accounts)


async def test_an_integration_added_under_account_a_is_the_same_after_rotation_to_b(
    dev_deps: DevDeps, fakes: Fakes, state: IntegrationsState
) -> None:
    fakes.mac_host.manager_state = account("a")
    await add_static(dev_deps, fakes, state)
    await add_oauth(dev_deps, fakes, state)
    under_a = await service.mcp_config_for(dev_deps, BotName.DEV, "coding")
    headers_a = await headers(dev_deps, "markaz")

    fakes.mac_host.manager_state = account("b")  # ClaudeManager rotated; no restart, no re-entry
    under_b = await service.mcp_config_for(dev_deps, BotName.DEV, "coding")
    blind = replace(dev_deps, mac_host=AccountsUnreadable())

    assert under_a == under_b == await service.mcp_config_for(blind, BotName.DEV, "coding")
    assert headers_a == await headers(blind, "markaz") == {"Authorization": "Bearer at-0002-fixture"}
    assert set(under_b) == {"bonvi_hr", "markaz"}


async def test_injection_skips_failing_integrations_but_keeps_an_unreachable_one(
    dev_deps: DevDeps, fakes: Fakes, state: IntegrationsState
) -> None:
    await add_static(dev_deps, fakes, state)
    fakes.mcp.unreachable.add(INTEGRATION_URL)
    with pytest.raises(IntegrationFailed):
        await service.check_integration(dev_deps, "Bonvi HR")
    assert list(await service.mcp_config_for(dev_deps, BotName.DEV, "coding")) == ["bonvi_hr"]

    fakes.mcp.unreachable.clear()
    fakes.mcp.required_headers[INTEGRATION_URL] = {"Authorization": "Bearer other"}
    with pytest.raises(IntegrationFailed):
        await service.check_integration(dev_deps, "Bonvi HR")
    assert await service.mcp_config_for(dev_deps, BotName.DEV, "coding") == {}


async def test_allowed_tool_names_cover_the_read_only_tools(
    dev_deps: DevDeps, fakes: Fakes, state: IntegrationsState
) -> None:
    await add_static(dev_deps, fakes, state)
    assert await service.allowed_tool_names(dev_deps, BotName.DEV) == {"mcp__bonvi_hr__employees_list"}


async def test_the_views_carry_kind_and_auth_but_no_secret(
    dev_deps: DevDeps, fakes: Fakes, state: IntegrationsState
) -> None:
    await add_static(dev_deps, fakes, state)
    (view,) = await service.list_integrations(dev_deps)
    assert (view.kind, view.auth, view.status) == (IntegrationKind.MCP_HTTP, AuthKind.BEARER, IntegrationStatus.OK)
    assert INTEGRATION_TOKEN not in repr(view)


async def test_retention_deletes_removed_integrations_and_their_grants_after_thirty_days(
    dev_deps: DevDeps, fakes: Fakes, state: IntegrationsState
) -> None:
    await add_static(dev_deps, fakes, state)
    await add_oauth(dev_deps, fakes, state)
    await service.remove_integration(dev_deps, state, make_owner_origin(), "Markaz")

    fakes.clock.set(fakes.clock.now() + timedelta(days=30))
    assert await service.apply_retention(dev_deps) == 0  # still restorable today
    fakes.clock.set(fakes.clock.now() + timedelta(seconds=1))
    assert await service.apply_retention(dev_deps) == 1

    assert [row.slug for row in await rows(dev_deps)] == ["bonvi_hr"]
    async with dev_deps.db.read() as s:
        assert await s.scalar(select(OAuthClient)) is None
