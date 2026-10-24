"""Integration use cases (SPEC §3.2.10; UC-08, UC-35; NFR-SEC-07; D11; R-43).

The owner says "Bonvi HR'ga ulan: <url> [token]" or "mana API kaliti". The token was already moved into the vault at
intake (SPEC §3.2.1 step 3), so the intent hands over a `SecretRef`, never a value. Then:

1. **Validate** within `timeouts.integration_validate_s`: connect and list the tools (`McpClient.list_tools`).
   Any failure → `IntegrationFailed(name, reason, fix)`, which the owner reads as one Uzbek line (UC-08 F).
2. **OAuth from the phone** when the server answers 401 with OAuth metadata and no token was given: the SDK's flow
   runs in a background task. Its authorization URL is the reply (`S.OAUTH_LINK`); the owner signs in on his phone,
   the localhost redirect fails to load, and he pastes that URL (or just the code) back. `complete_oauth` hands it to
   the waiting flow; the token exchange completes and the grant is sealed. A restart mid-flow loses it: the owner
   asks again (SPEC §3.2.10 step 3).
3. **Store** the row in this bot's own registry (`integrations`) and confirm with the tool list, read-only marked.
4. **Inject** into every dev executor session: `mcp_config_for` returns `url` + `headersHelper` only. The helper asks
   `serve_headers` over the control socket for the header at connect time. Nothing reads a Claude account, so the
   config is identical whichever account is active (UC-08 rotation AC).
5. **Refresh**: `refresh_tokens` (every `integrations.refresh_every_s`) refreshes grants expiring within
   `integrations.refresh_lead_min`. `integrations.refresh_failures_max` refusals in a row → `needs_reauth` and one
   `S.OAUTH_REAUTH` until the owner signs in again.

Plain API keys (`kind=api_http`) are stored and listed here; the read-only HTTP tool that uses them is T76 (P3).
"""

import asyncio
import secrets
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Final

from yordamchi.core import aio, json_value, redact
from yordamchi.core import strings as S
from yordamchi.core.control import HeadersAnswer, HeadersRequest
from yordamchi.core.errors import (
    AuthFailed,
    CallTimedOut,
    ExternalError,
    IntegrationExists,
    IntegrationFailed,
    IntegrationNotFound,
    OAuthCallbackInvalid,
    OAuthNotPending,
    OAuthPasteTimedOut,
    OAuthReauthRequired,
    OAuthRequired,
    VaultDecryptFailed,
    VaultSecretNotFound,
    YordamchiError,
)
from yordamchi.core.log import get_log
from yordamchi.core.paths import BotName
from yordamchi.core.text import BotText
from yordamchi.persistence import bulk
from yordamchi.persistence.uow import Session
from yordamchi.ports.agent import McpHttpServer
from yordamchi.ports.keystore import Sealed
from yordamchi.ports.mcp import McpTool, OAuthAuthorized, OAuthCallback, OAuthClientConfig, OAuthGrant
from yordamchi.shared.access import service as access
from yordamchi.shared.access.domain import OwnerOrigin
from yordamchi.shared.audit import service as audit
from yordamchi.shared.audit.domain import AuditActor, AuditKind
from yordamchi.shared.deps import SharedDeps
from yordamchi.shared.integrations import domain, store
from yordamchi.shared.integrations.domain import (
    AddPlan,
    AuthKind,
    IntegrationKind,
    IntegrationStatus,
    IntegrationView,
)
from yordamchi.shared.integrations.models import Integration, OAuthClient
from yordamchi.shared.outbox import service as outbox
from yordamchi.shared.outbox.domain import Kind
from yordamchi.shared.settings import defaults
from yordamchi.shared.settings import service as settings
from yordamchi.shared.settings.domain import Snapshot
from yordamchi.shared.vault import service as vault
from yordamchi.shared.vault.domain import SecretRef

log = get_log(__name__)

AUDIT_REF_TYPE: Final = "integration"


# --- requests and state ---------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class AddIntegration:
    """ "<name>'ga ulan: <url> [token]". `secret` is the token intake vaulted; `header_name` sends it in a named
    header instead of `Authorization: Bearer`; `both` is "ikkala bot uchun" (dev bot only)."""

    name: str
    url: str
    secret: SecretRef | None = None
    both: bool = False
    header_name: str | None = None


@dataclass(frozen=True, slots=True)
class AddApiKey:
    """ "mana <name> API kaliti: <base url> — <what it is for>" (UC-08; the read-only HTTP tool is T76)."""

    name: str
    base_url: str
    description: str
    secret: SecretRef
    both: bool = False
    header_name: str | None = None


@dataclass(slots=True)
class _Flow:
    """One OAuth flow waiting for the owner. `link` resolves when the SDK has the authorization URL, `callback` when
    he pastes the redirect, `outcome` when the flow ends. `waiter` says `complete_oauth` will deliver the outcome
    as its reply; otherwise the flow posts it through the outbox."""

    flow_id: str
    plan: AddPlan
    origin: OwnerOrigin
    link: asyncio.Future[str]
    callback: asyncio.Future[OAuthCallback]
    outcome: asyncio.Future[BotText]
    state: str | None = None
    waiter: bool = False
    task: asyncio.Task[None] | None = None


@dataclass(slots=True)
class IntegrationsState:
    """In-memory OAuth flows by slug. Built once in `bots/<bot>/app.py`; `shutdown_integrations` cancels them."""

    flows: dict[str, _Flow] = field(default_factory=dict)


# --- views ----------------------------------------------------------------------------------------------------------
def _view(row: Integration) -> IntegrationView:
    return IntegrationView(
        id=row.id,
        slug=row.slug,
        display_name=row.display_name,
        kind=IntegrationKind(row.kind),
        url=row.url,
        auth=AuthKind(row.auth),
        bots=domain.bots_from_json(row.bots_json),
        status=IntegrationStatus(row.status),
        tools=domain.tools_from_cache(row.tools_cache_json),
        policy=domain.policy_from_json(row.tool_policy_json),
        description=row.description,
        last_validated_at=row.last_validated_at,
        removed_at=row.removed_at,
    )


async def list_integrations(deps: SharedDeps) -> tuple[IntegrationView, ...]:
    """This bot's active integrations, oldest first. Never a secret."""
    async with deps.db.read() as s:
        rows = await store.list_active_integrations(s)
    return tuple(_view(row) for row in rows)


async def show_integrations(deps: SharedDeps) -> BotText:
    """ "integratsiyalar": each with its kind, bots, status and last check (UC-08: list by chat)."""
    snap = await settings.snapshot(deps)
    return domain.list_text(await list_integrations(deps), timezone=snap[defaults.UI_TIMEZONE])


async def _find(deps: SharedDeps, name: str) -> IntegrationView:
    found = domain.find(await list_integrations(deps), name)
    if found is None:
        raise IntegrationNotFound(name=" ".join(name.split())[:64])
    return found


def _own(deps: SharedDeps, origin: OwnerOrigin) -> None:
    if origin.bot is not deps.bot:
        msg = f"an owner origin of the {origin.bot} bot cannot change {deps.bot} integrations"
        raise ValueError(msg)


def _failed(name: str, error: YordamchiError) -> IntegrationFailed:
    reason = domain.reason_of(error.code)
    why, fix = domain.failure_parts(reason)
    return IntegrationFailed(name=name, reason=why, fix=fix, reason_code=reason)


def _internal_failure(name: str) -> IntegrationFailed:
    why, fix = domain.failure_parts("internal")
    return IntegrationFailed(name=name, reason=why, fix=fix, reason_code="internal")


async def _audit(
    s: Session, deps: SharedDeps, row: Integration, *, action: str, at: datetime, origin: OwnerOrigin | None
) -> None:
    await audit.record_event(
        s,
        bot=deps.bot,
        at=at,
        actor=AuditActor.OWNER if origin is not None else AuditActor.SYSTEM,
        kind=AuditKind.INTEGRATION_CHANGED,
        ref_type=AUDIT_REF_TYPE,
        ref_id=str(row.id),
        data={
            "action": action,
            "slug": row.slug,
            "kind": row.kind,
            "auth": row.auth,
            "origin_update_id": origin.update_id if origin is not None else None,
        },
    )


# --- secrets and headers --------------------------------------------------------------------------------------------
async def _secret_value(deps: SharedDeps, secret_id: int | None) -> str:
    """The plaintext of the vault secret an integration points at, by id (a name can be reused after removal)."""
    ref = next((ref for ref in await vault.list_secrets(deps) if ref.id == secret_id), None)
    if ref is None:
        raise VaultSecretNotFound(name=f"#{secret_id}")
    return await vault.open_secret(deps, ref.name)


def _open_grant(deps: SharedDeps, row: Integration, client: OAuthClient) -> OAuthGrant:
    if client.ciphertext is None or client.nonce is None:
        raise VaultDecryptFailed(name=row.slug)
    sealed = Sealed(ciphertext=client.ciphertext, nonce=client.nonce, key_fp=client.key_fp)
    plaintext = deps.keystore.unseal(sealed, aad=domain.grant_aad(row.id), name=row.slug)
    try:
        document = json_value.loads(plaintext.decode())
    except ValueError:
        raise VaultDecryptFailed(name=row.slug) from None
    grant = domain.grant_from_document(document, issuer=client.issuer, client_id=client.client_id)
    if grant is None:
        raise VaultDecryptFailed(name=row.slug)
    return grant


def _seal_grant(deps: SharedDeps, row: Integration, grant: OAuthGrant) -> Sealed:
    redact.add_known_secrets([grant.access_token, *grant.secret_values])
    document = json_value.dumps(domain.grant_document(grant)).encode()
    return deps.keystore.seal(document, aad=domain.grant_aad(row.id))


async def _row_headers(deps: SharedDeps, row: Integration, client: OAuthClient | None) -> dict[str, str]:
    auth = AuthKind(row.auth)
    if auth is AuthKind.OAUTH:
        if client is None:
            raise VaultDecryptFailed(name=row.slug)
        grant = _open_grant(deps, row, client)
        return domain.auth_headers(AuthKind.BEARER, value=grant.access_token, header_name=None)
    if auth is AuthKind.NONE:
        return {}
    return domain.auth_headers(auth, value=await _secret_value(deps, row.secret_id), header_name=row.header_name)


def _client_config(snap: Snapshot) -> OAuthClientConfig:
    return OAuthClientConfig(
        redirect_uri=domain.redirect_uri(snap[defaults.INTEGRATIONS_OAUTH_REDIRECT_PORT]),
        client_name=domain.CLIENT_NAME,
        client_metadata_url=snap[defaults.INTEGRATIONS_OAUTH_CLIENT_METADATA_URL] or None,
    )


# --- add ------------------------------------------------------------------------------------------------------------
async def add_integration(
    deps: SharedDeps, state: IntegrationsState, origin: OwnerOrigin, request: AddIntegration
) -> BotText:
    """Validate and store an MCP integration, or start its OAuth flow and return the sign-in link.

    Re-adding an integration that is failing (a new token, "qayta ulan" after an expired grant) re-validates it in
    place; re-adding a working one without a new token raises `IntegrationExists`.
    """
    _own(deps, origin)
    plan = domain.plan_add(name=request.name, url=request.url, said_in=deps.bot, both=request.both)
    snap = await settings.snapshot(deps)
    async with deps.db.read() as s:
        existing = await store.find_active_integration(s, plan.slug)
    if existing is not None and existing.status == IntegrationStatus.OK and request.secret is None:
        raise IntegrationExists(name=existing.display_name)
    auth, headers = AuthKind.NONE, {}
    if request.secret is not None:
        auth = AuthKind.HEADER if request.header_name else AuthKind.BEARER
        try:
            value = await vault.open_secret(deps, request.secret.name)
        except YordamchiError as error:
            raise _failed(plan.display_name, error) from error
        headers = domain.auth_headers(auth, value=value, header_name=request.header_name)
    try:
        tools = await deps.mcp.list_tools(plan.url, headers, timeout=snap[defaults.TIMEOUTS_INTEGRATION_VALIDATE_S])
    except OAuthRequired as error:
        if request.secret is not None:
            raise _failed(plan.display_name, AuthFailed(provider=str(error.params["provider"]))) from error
        return await _start_oauth(deps, state, origin, plan, snap)
    except ExternalError as error:
        log.warning("integration_validation_failed", slug=plan.slug, code=error.code.value)
        raise _failed(plan.display_name, error) from error
    secret_id = request.secret.id if request.secret is not None else None
    return await _save(deps, origin, plan, tools, auth=auth, header_name=request.header_name, secret_id=secret_id)


async def _save(
    deps: SharedDeps,
    origin: OwnerOrigin,
    plan: AddPlan,
    tools: Sequence[McpTool],
    *,
    auth: AuthKind,
    header_name: str | None,
    secret_id: int | None,
) -> BotText:
    now = deps.clock.now()
    async with deps.db.uow() as s:
        row = await store.find_active_integration(s, plan.slug)
        reconnected = row is not None
        if row is None:
            row = await store.add_integration(
                s,
                slug=plan.slug,
                display_name=plan.display_name,
                kind=IntegrationKind.MCP_HTTP.value,
                url=plan.url,
                auth=auth.value,
                header_name=header_name,
                secret_id=secret_id,
                bots=domain.bots_json(plan.bots),
                tool_policy=domain.default_policy_json(),
                tools_cache=domain.tools_cache(tools),
                status=IntegrationStatus.OK.value,
                last_validated_at=now,
                created_at=now,
            )
        else:
            row.header_name = header_name
            await store.set_credentials(s, row, auth=auth.value, secret_id=secret_id)
            await store.set_status(
                s, row, status=IntegrationStatus.OK.value, status_code=None, validated_at=now,
                tools_cache=domain.tools_cache(tools),
            )  # fmt: skip
        await _audit(s, deps, row, action="reconnected" if reconnected else "added", at=now, origin=origin)
    log.info("integration_added", integration_id=row.id, slug=row.slug, auth=auth.value, tools=len(tools))
    return _added_text(_view(row), reconnected=reconnected)


def _added_text(view: IntegrationView, *, reconnected: bool) -> BotText:
    tools = domain.tools_text(view.tools)
    if reconnected:
        return S.INTEGRATION_RECONNECTED(name=view.display_name, tools=tools)
    return S.INTEGRATION_ADDED(name=view.display_name, bots=domain.bots_label(view.bots), tools=tools)


async def add_api_key(deps: SharedDeps, origin: OwnerOrigin, request: AddApiKey) -> BotText:
    """Store a plain API key integration: a name, a base URL, what it is for, and the vaulted key (by id)."""
    _own(deps, origin)
    plan = domain.plan_add(name=request.name, url=request.base_url, said_in=deps.bot, both=request.both)
    now = deps.clock.now()
    auth = AuthKind.HEADER if request.header_name else AuthKind.BEARER
    async with deps.db.uow() as s:
        if await store.find_active_integration(s, plan.slug) is not None:
            raise IntegrationExists(name=plan.display_name)
        row = await store.add_integration(
            s,
            slug=plan.slug,
            display_name=plan.display_name,
            kind=IntegrationKind.API_HTTP.value,
            url=plan.url,
            description=" ".join(request.description.split())[:200],
            auth=auth.value,
            header_name=request.header_name,
            secret_id=request.secret.id,
            bots=domain.bots_json(plan.bots),
            tool_policy=domain.default_policy_json(),
            tools_cache=[],
            status=IntegrationStatus.OK.value,
            created_at=now,
        )
        await _audit(s, deps, row, action="added", at=now, origin=origin)
    log.info("integration_added", integration_id=row.id, slug=row.slug, auth=auth.value, tools=0)
    return S.API_KEY_ADDED(name=plan.display_name, bots=domain.bots_label(plan.bots), url=plan.url)


# --- OAuth paste-back (R-43) ----------------------------------------------------------------------------------------
async def _start_oauth(
    deps: SharedDeps, state: IntegrationsState, origin: OwnerOrigin, plan: AddPlan, snap: Snapshot
) -> BotText:
    _cancel_flow(state, plan.slug)
    loop = asyncio.get_running_loop()
    flow = _Flow(
        flow_id=secrets.token_hex(4),
        plan=plan,
        origin=origin,
        link=loop.create_future(),
        callback=loop.create_future(),
        outcome=loop.create_future(),
    )
    state.flows[plan.slug] = flow
    flow.task = aio.spawn(f"oauth_{plan.slug}", _run_oauth(deps, state, flow, snap))
    log.info("integration_oauth_started", slug=plan.slug, flow_id=flow.flow_id)
    try:
        url = await asyncio.wait_for(asyncio.shield(flow.link), timeout=snap[defaults.TIMEOUTS_INTEGRATION_VALIDATE_S])
    except TimeoutError:
        _cancel_flow(state, plan.slug)
        raise _failed(plan.display_name, CallTimedOut(operation="mcp")) from None
    return S.OAUTH_LINK(
        name=plan.display_name,
        url=url,
        port=snap[defaults.INTEGRATIONS_OAUTH_REDIRECT_PORT],
        minutes=snap[defaults.INTEGRATIONS_OAUTH_PASTE_TIMEOUT_MIN],
    )


def _cancel_flow(state: IntegrationsState, slug: str) -> None:
    flow = state.flows.pop(slug, None)
    if flow is not None and flow.task is not None:
        flow.task.cancel()


async def _expire_paste(deps: SharedDeps, flow: _Flow, minutes: int) -> None:
    await deps.clock.sleep(minutes * 60)
    if not flow.callback.done():
        flow.callback.set_exception(OAuthPasteTimedOut(name=flow.plan.display_name, minutes=minutes))


async def _run_oauth(deps: SharedDeps, state: IntegrationsState, flow: _Flow, snap: Snapshot) -> None:
    validate_s = snap[defaults.TIMEOUTS_INTEGRATION_VALIDATE_S]
    minutes = snap[defaults.INTEGRATIONS_OAUTH_PASTE_TIMEOUT_MIN]
    timers: list[asyncio.Task[None]] = []

    async def on_link(url: str) -> None:
        flow.state = domain.state_of(url)
        if not flow.link.done():
            flow.link.set_result(url)

    async def wait_callback() -> OAuthCallback:
        timers.append(aio.spawn(f"oauth_paste_{flow.plan.slug}", _expire_paste(deps, flow, minutes)))
        return await flow.callback

    try:
        authorized = await deps.mcp.authorize(
            flow.plan.url,
            client=_client_config(snap),
            on_link=on_link,
            wait_callback=wait_callback,
            request_timeout=validate_s,
            timeout=minutes * 60 + 4 * validate_s,
        )
        text = await _store_oauth(deps, flow, authorized)
    except YordamchiError as error:
        failure = error if isinstance(error, OAuthPasteTimedOut) else _failed(flow.plan.display_name, error)
        log.warning("integration_oauth_failed", slug=flow.plan.slug, flow_id=flow.flow_id, code=error.code.value)
        if not flow.link.done():
            flow.link.set_exception(failure)  # before the link: `add_integration` raises it as its reply
            return
        text = S.ERRORS[failure.code](**failure.params)
    except Exception:
        # Never a silent miss (UC-08 F): the owner hears that it failed, then `aio` logs the traceback.
        failure = _internal_failure(flow.plan.display_name)
        if flow.link.done():
            await _deliver(deps, flow, S.ERRORS[failure.code](**failure.params))
        else:
            flow.link.set_exception(failure)
        raise
    finally:
        for timer in timers:
            timer.cancel()
        if state.flows.get(flow.plan.slug) is flow:
            del state.flows[flow.plan.slug]
    await _deliver(deps, flow, text)


async def _deliver(deps: SharedDeps, flow: _Flow, text: BotText) -> None:
    """The flow's outcome: the reply of the waiting `complete_oauth`, else a notice through the outbox."""
    if not flow.outcome.done():
        flow.outcome.set_result(text)
    if flow.waiter:
        return
    snap = await settings.snapshot(deps)
    chat = await access.resolve_home_chat(deps)
    now = deps.clock.now()
    async with deps.db.uow() as s:
        await outbox.enqueue_text(
            s,
            bot=deps.bot,
            chat_id=chat,
            text=text,
            kind=Kind.NOTICE,
            ref=f"oauth:{flow.flow_id}",
            at=now,
            max_parts=snap[defaults.OUTBOX_MAX_PARTS],
        )


async def _store_oauth(deps: SharedDeps, flow: _Flow, authorized: OAuthAuthorized) -> BotText:
    now, plan, grant = deps.clock.now(), flow.plan, authorized.grant
    async with deps.db.uow() as s:
        row = await store.find_active_integration(s, plan.slug)
        reconnected = row is not None
        if row is None:
            row = await store.add_integration(
                s,
                slug=plan.slug,
                display_name=plan.display_name,
                kind=IntegrationKind.MCP_HTTP.value,
                url=plan.url,
                auth=AuthKind.OAUTH.value,
                bots=domain.bots_json(plan.bots),
                tool_policy=domain.default_policy_json(),
                tools_cache=domain.tools_cache(authorized.tools),
                status=IntegrationStatus.OK.value,
                last_validated_at=now,
                created_at=now,
            )
        else:
            await store.set_credentials(s, row, auth=AuthKind.OAUTH.value, secret_id=None)
            await store.set_status(
                s, row, status=IntegrationStatus.OK.value, status_code=None, validated_at=now,
                tools_cache=domain.tools_cache(authorized.tools),
            )  # fmt: skip
        client = await store.put_oauth_client(
            s, integration_id=row.id, issuer=grant.issuer, client_id=grant.client_id, key_fp=deps.keystore.fingerprint()
        )
        sealed = _seal_grant(deps, row, grant)
        await store.set_grant(
            s,
            client,
            ciphertext=sealed.ciphertext,
            nonce=sealed.nonce,
            key_fp=sealed.key_fp,
            expires_at=domain.expires_at(now, grant.expires_in),
            refreshed_at=None,
        )
        await _audit(s, deps, row, action="oauth_authorized", at=now, origin=flow.origin)
    log.info("integration_added", integration_id=row.id, slug=row.slug, auth="oauth", tools=len(authorized.tools))
    return _added_text(_view(row), reconnected=reconnected)


async def complete_oauth(
    deps: SharedDeps,
    state: IntegrationsState,
    origin: OwnerOrigin,
    *,
    secret: SecretRef | None = None,
    pasted: str | None = None,
) -> BotText:
    """The owner pasted the redirect URL (intake vaulted it as `oauth_callback_url`: pass its `secret`) or typed just
    the code (`pasted`). Resumes the waiting flow and returns its outcome; a code is single-use, so the vaulted URL
    is removed at once."""
    _own(deps, origin)
    raw = await vault.open_secret(deps, secret.name) if secret is not None else (pasted or "")
    callback = domain.parse_callback(raw)
    if callback is None:
        raise OAuthCallbackInvalid
    waiting = {slug: flow.state for slug, flow in state.flows.items() if flow.link.done() and not flow.callback.done()}
    slug = domain.match_flow(waiting, callback)
    if slug is None:
        raise OAuthNotPending
    if secret is not None:
        await vault.remove_secret(deps, origin, secret.name)
    flow = state.flows[slug]
    flow.waiter = True
    flow.callback.set_result(callback)
    log.info("integration_oauth_pasted", slug=slug, flow_id=flow.flow_id, with_state=callback.state is not None)
    snap = await settings.snapshot(deps)
    try:
        return await asyncio.wait_for(
            asyncio.shield(flow.outcome), timeout=2 * snap[defaults.TIMEOUTS_INTEGRATION_VALIDATE_S]
        )
    except TimeoutError:
        flow.waiter = False  # the flow posts its outcome itself when it ends
        return S.OAUTH_STILL_WORKING(name=flow.plan.display_name)


async def shutdown_integrations(deps: SharedDeps, state: IntegrationsState) -> None:
    """Cancel every waiting flow (SPEC §2.6); the owner asks again after the restart."""
    tasks = [flow.task for flow in state.flows.values() if flow.task is not None]
    log.info("integrations_shut_down", bot=deps.bot.value, flows=len(tasks))
    state.flows.clear()
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


# --- remove, restore, rename, assign, check -------------------------------------------------------------------------
async def remove_integration(deps: SharedDeps, state: IntegrationsState, origin: OwnerOrigin, name: str) -> BotText:
    """Mark it removed: absent from the next session (UC-08 AC); `restore_integration` brings it back for 30 days.
    Its vault secret stays (masked, in the pre-push hash file) until the owner removes the key itself."""
    _own(deps, origin)
    view = await _find(deps, name)
    _cancel_flow(state, view.slug)
    now = deps.clock.now()
    async with deps.db.uow() as s:
        row = await store.get_integration(s, view.id)
        if row is None or row.removed_at is not None:
            raise IntegrationNotFound(name=view.display_name)
        await store.mark_integration_removed(s, row, at=now)
        await _audit(s, deps, row, action="removed", at=now, origin=origin)
    log.info("integration_removed", integration_id=view.id, slug=view.slug)
    return S.INTEGRATION_REMOVED(name=view.display_name)


async def restore_integration(deps: SharedDeps, origin: OwnerOrigin, name: str) -> BotText:
    """Undo a removal of the last 30 days, unless the name is taken again meanwhile."""
    _own(deps, origin)
    now = deps.clock.now()
    async with deps.db.uow() as s:
        removed = await store.list_removed_integrations(s, since=now - domain.RESTORE_WINDOW)
        view = domain.find((_view(row) for row in removed), name)
        if view is None:
            raise IntegrationNotFound(name=" ".join(name.split())[:64])
        if await store.find_active_integration(s, view.slug) is not None:
            raise IntegrationExists(name=view.display_name)
        row = next(row for row in removed if row.id == view.id)
        await store.mark_integration_removed(s, row, at=None)
        await _audit(s, deps, row, action="restored", at=now, origin=origin)
    log.info("integration_restored", integration_id=view.id, slug=view.slug)
    return S.INTEGRATION_RESTORED(name=view.display_name)


async def rename_integration(deps: SharedDeps, origin: OwnerOrigin, name: str, new_name: str) -> BotText:
    """A new display name and slug; sessions see the new server name from their next open."""
    _own(deps, origin)
    view = await _find(deps, name)
    slug, display_name = domain.slugify(new_name), " ".join(new_name.split())[:64]
    now = deps.clock.now()
    async with deps.db.uow() as s:
        taken = await store.find_active_integration(s, slug)
        if taken is not None and taken.id != view.id:
            raise IntegrationExists(name=taken.display_name)
        row = await store.get_integration(s, view.id)
        if row is None:
            raise IntegrationNotFound(name=view.display_name)
        await store.set_names(s, row, slug=slug, display_name=display_name)
        await _audit(s, deps, row, action="renamed", at=now, origin=origin)
    log.info("integration_renamed", integration_id=view.id, slug=slug)
    return S.INTEGRATION_RENAMED(old=view.display_name, name=display_name)


async def assign_integration(deps: SharedDeps, origin: OwnerOrigin, name: str, bots: Sequence[BotName]) -> BotText:
    """Per-bot assignment (SPEC §2.3): the dev bot may give its integration to the personal bot, or to both; the
    personal bot keeps its own for itself."""
    _own(deps, origin)
    view = await _find(deps, name)
    assigned = domain.check_assignment(deps.bot, bots)
    now = deps.clock.now()
    async with deps.db.uow() as s:
        row = await store.get_integration(s, view.id)
        if row is None:
            raise IntegrationNotFound(name=view.display_name)
        await store.set_bots(s, row, bots=domain.bots_json(assigned))
        await _audit(s, deps, row, action="assigned", at=now, origin=origin)
    log.info("integration_assigned", integration_id=view.id, bots=",".join(bot.value for bot in assigned))
    return S.INTEGRATION_ASSIGNED(name=view.display_name, bots=domain.bots_label(assigned))


async def check_integration(deps: SharedDeps, name: str) -> BotText:
    """Re-validate now and record the status and the check time. A failure is the owner's Uzbek message (UC-08 F)."""
    view = await _find(deps, name)
    snap = await settings.snapshot(deps)
    async with deps.db.read() as s:
        row = await store.get_integration(s, view.id)
        client = await store.get_oauth_client(s, view.id)
    if row is None:
        raise IntegrationNotFound(name=view.display_name)
    if view.kind is IntegrationKind.API_HTTP:
        status = S.INTEGRATION_STATUSES[view.status.value]()
        return S.INTEGRATION_CHECKED(name=view.display_name, status=status, tools=S.INTEGRATION_NO_TOOLS())
    try:
        headers = await _row_headers(deps, row, client)
        tools = await deps.mcp.list_tools(view.url, headers, timeout=snap[defaults.TIMEOUTS_INTEGRATION_VALIDATE_S])
    except YordamchiError as error:
        await _record_status(deps, view.id, status=domain.status_after(error.code), code=error.code.value)
        raise _failed(view.display_name, error) from error
    await _record_status(deps, view.id, status=IntegrationStatus.OK, code=None, tools=tools)
    return S.INTEGRATION_CHECKED(
        name=view.display_name,
        status=S.INTEGRATION_STATUSES[IntegrationStatus.OK.value](),
        tools=domain.tools_text(domain.tools_from_cache(domain.tools_cache(tools))),
    )


async def _record_status(
    deps: SharedDeps,
    integration_id: int,
    *,
    status: IntegrationStatus,
    code: str | None,
    tools: Sequence[McpTool] | None = None,
) -> None:
    now = deps.clock.now()
    cache = domain.tools_cache(tools) if tools is not None else None
    async with deps.db.uow() as s:
        row = await store.get_integration(s, integration_id)
        if row is not None:
            await store.set_status(s, row, status=status.value, status_code=code, validated_at=now, tools_cache=cache)
    log.info("integration_checked", integration_id=integration_id, status=status.value, code=code or "")


# --- sessions: injection and the headers helper ---------------------------------------------------------------------
async def mcp_config_for(deps: SharedDeps, bot: BotName, session_kind: str) -> dict[str, McpHttpServer]:
    """`SessionSpec.mcp_servers` for a session `bot` opens now (T24 turns spec, T12 assistant; T34/T58 wire it).

    Built from Yordamchi's registry alone, so it is the same under every Claude account (UC-08 rotation AC). An HTTP
    server carries only its URL and the `headersHelper` command: no token, key or header value is in it, and so none
    reaches `--mcp-config` on the command line (STACK §4.2). Dev `coding`/`investigation` sessions get the active,
    assigned MCP integrations; smoke sessions and the personal bot get none (its gateway is T76). claude.ai account
    connectors stay off (`ENABLE_CLAUDEAI_MCP_SERVERS=false`, `adapters/claude/options.py`).
    """
    async with deps.db.read() as s:
        rows = await store.list_active_integrations(s)
    helper = domain.helper_command(str(deps.paths.current_release))
    return domain.session_servers((_view(row) for row in rows), bot=bot, session_kind=session_kind, helper=helper)


async def allowed_tool_names(deps: SharedDeps, bot: BotName) -> frozenset[str]:
    """`mcp__<slug>__<tool>` names that run without asking (read-only, or allowed at add time), for the gate."""
    return domain.allowed_tools(await list_integrations(deps), bot)


async def serve_headers(
    deps: SharedDeps, request: HeadersRequest, *, session_known: Callable[[str], bool]
) -> HeadersAnswer:
    """The control socket's `headers` method (SPEC §3.1.1): the auth header for one server of a live session.

    `session_known` is the turns feature's check that the token belongs to a live session of this bot (T34/T58
    wiring). An unknown token, an unknown or unassigned server, an expired grant or an unreadable secret is an
    `error_code`, never an exception: the helper then prints nothing and Claude Code reports the server as failed.
    """
    if not session_known(request.session_token):
        log.warning("integration_headers_refused", reason="session_unknown")
        return HeadersAnswer(error_code="session_unknown")
    async with deps.db.read() as s:
        row = await store.find_active_integration(s, request.server_name)
        client = await store.get_oauth_client(s, row.id) if row is not None else None
    if row is None or row.kind != IntegrationKind.MCP_HTTP or deps.bot not in domain.bots_from_json(row.bots_json):
        log.warning("integration_headers_refused", reason="integration_unknown")
        return HeadersAnswer(error_code="integration_unknown")
    if row.status == IntegrationStatus.NEEDS_REAUTH:
        return HeadersAnswer(error_code=IntegrationStatus.NEEDS_REAUTH.value)
    try:
        headers = await _row_headers(deps, row, client)
    except YordamchiError as error:
        log.warning("integration_headers_refused", reason=error.code.value, slug=row.slug)
        return HeadersAnswer(error_code=error.code.value)
    log.info("integration_headers_served", slug=row.slug, auth=row.auth)
    return HeadersAnswer(headers=headers)


# --- OAuth token service: refresh and masking -----------------------------------------------------------------------
async def publish_grants(deps: SharedDeps) -> int:
    """Put every sealed OAuth token into the masking set (startup, beside `vault.publish_secrets`). Returns how many
    grants opened; one that does not open is logged and skipped."""
    async with deps.db.read() as s:
        pairs = await store.list_oauth_clients(s)
    opened = 0
    for row, client in pairs:
        try:
            grant = _open_grant(deps, row, client)
        except VaultDecryptFailed:
            log.warning("integration_grant_unreadable", integration_id=row.id)
            continue
        redact.add_known_secrets([grant.access_token, *grant.secret_values])
        opened += 1
    log.info("integration_grants_published", grants=len(pairs), opened=opened)
    return opened


async def refresh_tokens(deps: SharedDeps) -> int:
    """One pass of the token service: refresh each grant of this bot that expires within the lead time. Returns how
    many were refreshed. The bot that added an integration owns its row and refreshes it (SPEC §2.3)."""
    snap = await settings.snapshot(deps)
    now = deps.clock.now()
    async with deps.db.read() as s:
        pairs = await store.list_oauth_clients(s)
    lead = snap[defaults.INTEGRATIONS_REFRESH_LEAD_MIN]
    due = [
        (row, client)
        for row, client in pairs
        if row.status != IntegrationStatus.NEEDS_REAUTH and domain.refresh_due(client.expires_at, now, lead_min=lead)
    ]
    refreshed = 0
    for row, client in due:
        refreshed += await _refresh_one(deps, snap, row, client)
    return refreshed


async def _refresh_one(deps: SharedDeps, snap: Snapshot, row: Integration, client: OAuthClient) -> int:
    try:
        grant = _open_grant(deps, row, client)
        fresh = await deps.mcp.refresh(
            row.url, grant, client=_client_config(snap), timeout=snap[defaults.TIMEOUTS_INTEGRATION_VALIDATE_S]
        )
    except (OAuthReauthRequired, AuthFailed, VaultDecryptFailed) as error:
        await _refresh_refused(deps, snap, row.id, error)
        return 0
    except ExternalError as error:
        log.warning("integration_refresh_failed", integration_id=row.id, code=error.code.value)
        await _record_status(deps, row.id, status=IntegrationStatus.UNREACHABLE, code=error.code.value)
        return 0
    now = deps.clock.now()
    async with deps.db.uow() as s:
        current = await store.get_integration(s, row.id)
        stored = await store.get_oauth_client(s, row.id)
        if current is None or stored is None or current.removed_at is not None:
            return 0
        sealed = _seal_grant(deps, current, fresh)
        await store.set_grant(
            s,
            stored,
            ciphertext=sealed.ciphertext,
            nonce=sealed.nonce,
            key_fp=sealed.key_fp,
            expires_at=domain.expires_at(now, fresh.expires_in),
            refreshed_at=now,
        )
        if current.status != IntegrationStatus.OK:
            await store.set_status(s, current, status=IntegrationStatus.OK.value, status_code=None, validated_at=None)
    log.info("integration_token_refreshed", integration_id=row.id)
    return 1


async def _refresh_refused(deps: SharedDeps, snap: Snapshot, integration_id: int, error: YordamchiError) -> None:
    """One more refused refresh; at the limit → `needs_reauth` and `S.OAUTH_REAUTH`, once until he signs in again."""
    chat = await access.resolve_home_chat(deps)
    now = deps.clock.now()
    limit = snap[defaults.INTEGRATIONS_REFRESH_FAILURES_MAX]
    async with deps.db.uow() as s:
        row = await store.get_integration(s, integration_id)
        client = await store.get_oauth_client(s, integration_id)
        if row is None or client is None:
            return
        outcome = domain.after_refused_refresh(client.refresh_failures, limit=limit)
        await store.set_refresh_failures(s, client, failures=outcome.failures)
        told = False
        if outcome.needs_reauth:
            reauth = IntegrationStatus.NEEDS_REAUTH.value
            await store.set_status(s, row, status=reauth, status_code=error.code.value, validated_at=None)
            if client.reauth_told_at is None:
                await store.mark_reauth_told(s, client, at=now)
                await outbox.enqueue_text(
                    s,
                    bot=deps.bot,
                    chat_id=chat,
                    text=S.OAUTH_REAUTH(name=row.display_name),
                    kind=Kind.NOTICE,
                    ref=f"oauth_reauth:{row.id}:{int(now.timestamp())}",
                    at=now,
                    max_parts=snap[defaults.OUTBOX_MAX_PARTS],
                )
                told = True
    log.warning(
        "integration_refresh_refused",
        integration_id=integration_id,
        failures=outcome.failures,
        needs_reauth=outcome.needs_reauth,
        told=told,
    )


async def run_token_refresher(deps: SharedDeps) -> None:
    """The long-lived loop: `aio.forever("integrations_refresh", lambda: integrations.run_token_refresher(deps))`."""
    while True:
        await refresh_tokens(deps)
        snap = await settings.snapshot(deps)
        await deps.clock.sleep(snap[defaults.INTEGRATIONS_REFRESH_EVERY_S])


async def apply_retention(deps: SharedDeps) -> int:
    """Delete integrations removed more than 30 days ago with their sealed grants, in batches (SPEC §4.2)."""
    cutoff = domain.retention_cutoff(deps.clock.now())
    deleted = await bulk.run_in_batches(deps.db, lambda s, limit: store.delete_removed_before(s, cutoff, limit=limit))
    log.info("integrations_retention_applied", deleted=deleted)
    return deleted
