"""servers.service on a real SQLite file, `FakeRemoteShell` and `FakeClock` (SPEC §3.2.4, §4.2, §5.7; UC-15;
REQUIREMENTS Q3): the check suite and its bounds, the ≤ 30 min cache, alert-once, "Tekshiryapman" before any SSH
call, the read-only investigation, the jump-only host, hosts by chat and retention. Nothing connects anywhere."""

import asyncio
import inspect
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from tests.factories import (
    DEV_GROUP_ID,
    FAKE_KEYS,
    JUMP_HOST,
    SAMPLE_CONTAINERS,
    SERVER_HOST,
    Fakes,
    add_setting_row,
    df_with_root_at,
    docker_ps_table,
    list_server_checks,
    list_setting_rows,
    make_owner_origin,
    make_project,
    make_project_folder,
    projects_root_of,
    remote_ok,
    script_healthy_server,
)
from tests.fakes.agent import Pause
from tests.fakes.clock import FakeClock
from tests.fakes.remote_shell import FakeRemoteShell
from yordamchi.core import aio
from yordamchi.core import strings as S
from yordamchi.core.errors import (
    CallTimedOut,
    ProjectNotFound,
    ProviderUnavailable,
    ServerJumpOnly,
    ServerNotWatched,
    SettingInvalid,
)
from yordamchi.core.json_value import JsonValue, dumps
from yordamchi.core.paths import Paths
from yordamchi.dev.deps import DevDeps
from yordamchi.dev.gate import service as gate
from yordamchi.dev.servers import domain, service, store
from yordamchi.dev.servers.models import ServerCheck
from yordamchi.dev.turns import service as turns
from yordamchi.dev.turns import store as turns_store
from yordamchi.dev.turns.service import TurnsState
from yordamchi.ports.agent import ToolCall
from yordamchi.ports.remote_shell import RemoteResult
from yordamchi.shared.outbox import store as outbox_store
from yordamchi.shared.outbox.models import OutboxMessage
from yordamchi.shared.settings import defaults

PROJECT = "HRMentor"
SYSLOG = "/var/log/syslog"
SUITE = ["uptime", "df -h", "free -m", "docker ps"]  # the default gate list allows these (not the units probe)


# --- helpers --------------------------------------------------------------------------------------------------------
async def set_setting(deps: DevDeps, key: str, value: JsonValue) -> None:
    await add_setting_row(deps.db, key, value, at=deps.clock.now())


async def watch(deps: DevDeps, *hosts: str) -> None:
    await set_setting(deps, "servers.hosts", list(hosts))


async def allow_units_probe(deps: DevDeps) -> None:
    allowed: list[JsonValue] = [*defaults.READONLY_REMOTE_COMMANDS, domain.UNITS_COMMAND]
    await set_setting(deps, "gate.readonly_remote_commands", allowed)


def with_shell(deps: DevDeps, shell: FakeRemoteShell) -> DevDeps:
    return replace(deps, remote_shell=shell)


def summary_of(row: ServerCheck) -> dict[str, JsonValue]:
    assert isinstance(row.summary_json, dict)
    return row.summary_json


def objects(value: JsonValue) -> list[dict[str, JsonValue]]:
    assert isinstance(value, list)
    return [item for item in value if isinstance(item, dict)]


def probe_statuses(row: ServerCheck) -> dict[str, JsonValue]:
    return {str(item["key"]): item["status"] for item in objects(summary_of(row)["probes"])}


async def messages(deps: DevDeps) -> list[OutboxMessage]:
    async with deps.db.read() as s:
        return await outbox_store.list_outbox_messages(s, deps.bot.value)


def text_of(row: OutboxMessage) -> str:
    payload = row.payload_json
    return str(payload.get("text", "")) if isinstance(payload, dict) else ""


async def texts(deps: DevDeps, kind: str) -> list[str]:
    return [text_of(row) for row in await messages(deps) if row.kind == kind]


def commands(shell: FakeRemoteShell, host: str = SERVER_HOST) -> list[str]:
    return [command for called, command, _ in shell.calls if called == host]


def later(fakes: Fakes, minutes: float) -> None:
    fakes.clock.set(fakes.clock.now() + timedelta(minutes=minutes))


async def until(check: Callable[[], object], *, timeout: float = 3.0, what: str = "condition") -> None:
    """Wait (real time) for background tasks to reach a state; the scripted clock never moves here."""
    deadline = time.monotonic() + timeout
    while True:
        outcome = check()
        if inspect.isawaitable(outcome):
            outcome = await outcome
        if outcome:
            return
        if time.monotonic() > deadline:
            msg = f"{what} not reached"
            raise AssertionError(msg)
        await asyncio.sleep(0.005)


class SlowShell(FakeRemoteShell):
    """Each command takes `seconds` of the clock; `on_first` runs just before the first command is sent."""

    def __init__(self, clock: FakeClock, *, seconds: float, on_first: Callable[[], object] | None = None) -> None:
        super().__init__()
        self.clock = clock
        self.seconds = seconds
        self.on_first = on_first

    async def run(self, host: str, command: str, *, timeout: float) -> RemoteResult:
        if not self.calls and self.on_first is not None:
            outcome = self.on_first()
            if inspect.isawaitable(outcome):
                await outcome
        self.clock.set(self.clock.now() + timedelta(seconds=min(self.seconds, timeout)))
        return await super().run(host, command, timeout=timeout)


# --- the check suite (SPEC §3.2.4, §5.7) -----------------------------------------------------------------------------
async def test_a_scheduled_check_runs_only_what_the_gate_allows_and_stores_the_readings(
    dev_deps: DevDeps, fakes: Fakes
) -> None:
    await watch(dev_deps, SERVER_HOST)
    await set_setting(dev_deps, "servers.log_paths", [f"{SERVER_HOST}:{SYSLOG}", "other:/var/log/x.log"])
    script_healthy_server(fakes.remote_shell)
    fakes.remote_shell.respond(SERVER_HOST, f"tail -n 200 {SYSLOG}", remote_ok(SERVER_HOST, "Oct 24 boot ok\n"))

    done = await service.check_due_servers(dev_deps)

    # The units probe is not on SPEC §5.7's default list, so the gate refuses it and it is never sent.
    assert commands(fakes.remote_shell) == [*SUITE, f"tail -n 200 {SYSLOG}"]
    assert [status.host for status in done.checked] == [SERVER_HOST]
    assert done.checked[0].ok
    assert done.next_in_s == domain.CHECK_TICK_S
    (row,) = await list_server_checks(dev_deps.db)
    assert (row.host, row.ok, row.error_code, row.at) == (SERVER_HOST, True, None, fakes.clock.now())
    summary = summary_of(row)
    assert summary["trigger"] == "scheduled"
    assert summary["disks"] == [
        {"mount": "/", "used_pct": 85, "size": "147G", "avail": "22G"},
        {"mount": "/srv/data", "used_pct": 9, "size": "880G", "avail": "766G"},
    ]
    assert probe_statuses(row) == {
        "uptime": "ok",
        "disk": "ok",
        "memory": "ok",
        "containers": "ok",
        "units": "refused",
        f"log:{SYSLOG}": "ok",
    }
    assert await texts(dev_deps, "notice") == []


async def test_once_the_gate_list_allows_the_units_probe_a_failed_unit_is_alerted(
    dev_deps: DevDeps, fakes: Fakes
) -> None:
    await watch(dev_deps, SERVER_HOST)
    await allow_units_probe(dev_deps)
    script_healthy_server(fakes.remote_shell, failed_units="● nginx.service loaded failed failed nginx\n")

    await service.check_due_servers(dev_deps)

    assert commands(fakes.remote_shell) == [*SUITE, domain.UNITS_COMMAND]
    alerts = [row for row in await messages(dev_deps) if row.kind == "notice"]
    assert [text_of(row) for row in alerts] == [
        "bonvi-srv serverida muammo: nginx.service xizmati ishdan chiqqan (failed)."
    ]
    assert alerts[0].chat_id == DEV_GROUP_ID


async def test_output_is_clipped_and_scrubbed_before_it_is_stored(dev_deps: DevDeps, fakes: Fakes) -> None:
    await watch(dev_deps, SERVER_HOST)
    await set_setting(dev_deps, "servers.log_paths", [f"{SERVER_HOST}:{SYSLOG}"])
    script_healthy_server(fakes.remote_shell)
    many = tuple(
        (f"{index:012x}", "img", '"run"', "1 hour ago", "Up 1 hour", "", f"app-{index}-1") for index in range(5000)
    )
    fakes.remote_shell.respond(SERVER_HOST, "docker ps", remote_ok(SERVER_HOST, docker_ps_table(many)))
    key = FAKE_KEYS["anthropic_api_key"]
    noisy = [f"Oct 24 app: ERROR {'w' * 300}" for _ in range(99)]
    tail = "\n".join([*noisy, f"Oct 24 app: ERROR login failed with key {key}"])
    fakes.remote_shell.respond(SERVER_HOST, f"tail -n 200 {SYSLOG}", remote_ok(SERVER_HOST, tail))

    await service.check_due_servers(dev_deps)

    (row,) = await list_server_checks(dev_deps.db)
    stored = dumps(row.summary_json)
    assert len(stored) < 30_000  # about 450 KB came back
    assert key not in stored  # the newest error line is kept, with its key masked
    summary = summary_of(row)
    assert len(objects(summary["containers"])) == domain.LIST_MAX
    assert all(len(str(probe["excerpt"])) <= domain.EXCERPT_MAX_CHARS for probe in objects(summary["probes"]))
    (log,) = objects(summary["logs"])
    assert log["count"] == 100
    lines = log["lines"]
    assert isinstance(lines, list)
    assert all(len(str(line)) <= domain.LINE_MAX_CHARS for line in lines)
    assert str(lines[-1]).startswith("Oct 24 app: ERROR login failed with key ")


async def test_every_command_is_bounded_by_the_timeout_settings(dev_deps: DevDeps, fakes: Fakes) -> None:
    await watch(dev_deps, SERVER_HOST)
    await set_setting(dev_deps, "timeouts.server_command_s", 12.0)
    script_healthy_server(fakes.remote_shell)

    await service.check_due_servers(dev_deps)

    assert [timeout for _, _, timeout in fakes.remote_shell.calls] == [12.0] * len(SUITE)


async def test_a_slow_host_stops_at_its_check_budget(dev_deps: DevDeps, fakes: Fakes) -> None:
    slow = SlowShell(fakes.clock, seconds=50)
    script_healthy_server(slow)
    deps = with_shell(dev_deps, slow)
    await watch(deps, SERVER_HOST)
    await set_setting(deps, "servers.log_paths", [f"{SERVER_HOST}:{SYSLOG}"])
    await set_setting(deps, "timeouts.server_check_s", 100.0)
    started = fakes.clock.now()

    await service.check_due_servers(deps)

    # 30 s each (`timeouts.server_command_s`), then what is left of the 100 s budget, then nothing.
    assert [timeout for _, _, timeout in slow.calls] == [30.0, 30.0, 30.0, 10.0]
    assert fakes.clock.now() - started <= timedelta(seconds=100)
    (row,) = await list_server_checks(deps.db)
    assert probe_statuses(row)["units"] == "refused"
    assert probe_statuses(row)[f"log:{SYSLOG}"] == "skipped"


async def test_a_host_that_does_not_answer_is_not_waited_on_and_says_why(dev_deps: DevDeps, fakes: Fakes) -> None:
    await watch(dev_deps, SERVER_HOST)
    fakes.remote_shell.respond(SERVER_HOST, "uptime", ProviderUnavailable(provider=SERVER_HOST))

    first = await service.check_due_servers(dev_deps)

    assert commands(fakes.remote_shell) == ["uptime"]  # the rest is skipped, not timed out one by one
    (row,) = await list_server_checks(dev_deps.db)
    assert (row.ok, row.error_code) == (False, "provider_unavailable")
    assert domain.status_line(first.checked[0]) == "bonvi-srv: ulanib bo'lmadi — javob bermayapti (tarmoqni tekshiring)"
    assert await texts(dev_deps, "notice") == []  # one failed check is a blip

    later(fakes, 30)
    await service.check_due_servers(dev_deps)
    assert await texts(dev_deps, "notice") == ["bonvi-srv serverida muammo: server javob bermayapti."]


async def test_a_command_that_times_out_after_the_host_answered_only_loses_that_reading(
    dev_deps: DevDeps, fakes: Fakes
) -> None:
    await watch(dev_deps, SERVER_HOST)
    script_healthy_server(fakes.remote_shell)
    fakes.remote_shell.respond(SERVER_HOST, "docker ps", CallTimedOut(operation="ssh"))

    done = await service.check_due_servers(dev_deps)

    assert commands(fakes.remote_shell) == SUITE
    status = done.checked[0]
    assert status.reachable
    assert status.report is not None
    assert status.report.containers is None
    assert status.ok


# --- the cache (UC-15: ≤ 30 min answers directly) -------------------------------------------------------------------
async def test_a_check_younger_than_the_stale_limit_answers_from_the_cache_without_ssh(
    dev_deps: DevDeps, fakes: Fakes
) -> None:
    await watch(dev_deps, SERVER_HOST)
    script_healthy_server(fakes.remote_shell)
    await service.check_due_servers(dev_deps)
    fakes.remote_shell.calls.clear()
    later(fakes, 10)

    answer = await service.answer_server_status(dev_deps, chat_id=DEV_GROUP_ID, ref="route-1", text="serverlar?")

    assert fakes.remote_shell.calls == []
    assert answer.checked == ()
    assert answer.reply.splitlines() == [
        "bonvi-srv: hammasi joyida · yuklama 0.52 · disk 85% (/) · xotira 46% · 2 ta konteyner",
        "Ma'lumot: 10 daqiqa oldin",
    ]
    assert await texts(dev_deps, "report") == [answer.reply]  # no "Tekshiryapman" for a cached answer


async def test_an_older_check_is_refreshed_before_answering(dev_deps: DevDeps, fakes: Fakes) -> None:
    await watch(dev_deps, SERVER_HOST)
    script_healthy_server(fakes.remote_shell)
    await service.check_due_servers(dev_deps)
    fakes.remote_shell.calls.clear()
    later(fakes, 31)

    answer = await service.answer_server_status(dev_deps, chat_id=DEV_GROUP_ID, ref="route-2")

    assert answer.checked == (SERVER_HOST,)
    assert commands(fakes.remote_shell) == SUITE
    assert answer.reply.splitlines()[-1] == "Ma'lumot: hozirgina"
    assert await texts(dev_deps, "report") == ["Tekshiryapman (~3 daqiqa): bonvi-srv.", answer.reply]
    rows = await list_server_checks(dev_deps.db)
    assert [summary_of(row)["trigger"] for row in rows] == ["scheduled", "on_demand"]


async def test_tekshiryapman_is_queued_within_two_seconds_before_any_ssh_call(dev_deps: DevDeps, fakes: Fakes) -> None:
    seen: dict[str, object] = {}

    async def at_first_command() -> None:
        seen["at"] = fakes.clock.now()
        seen["rows"] = [(row.kind, row.priority, text_of(row)) for row in await messages(dev_deps)]

    slow = SlowShell(fakes.clock, seconds=20, on_first=at_first_command)
    script_healthy_server(slow)
    deps = with_shell(dev_deps, slow)
    await watch(deps, SERVER_HOST)
    started = fakes.clock.now()

    answer = await service.answer_server_status(deps, chat_id=DEV_GROUP_ID, ref="route-3", reply_to=77)

    assert seen["rows"] == [("report", 0, "Tekshiryapman (~3 daqiqa): bonvi-srv.")]
    rows = await messages(deps)
    assert rows[0].created_at - started <= timedelta(seconds=2)
    assert rows[0].reply_to_message_id == 77
    assert text_of(rows[-1]) == answer.reply
    assert rows[-1].created_at - started <= timedelta(minutes=3)


async def test_the_status_of_one_named_host_checks_only_that_host(dev_deps: DevDeps, fakes: Fakes) -> None:
    await watch(dev_deps, SERVER_HOST, "db-srv")
    script_healthy_server(fakes.remote_shell)
    script_healthy_server(fakes.remote_shell, "db-srv")

    answer = await service.answer_server_status(dev_deps, chat_id=DEV_GROUP_ID, ref="r", text="db-srv qanday?")

    assert answer.checked == ("db-srv",)
    assert commands(fakes.remote_shell, SERVER_HOST) == []
    assert answer.reply.startswith("db-srv: hammasi joyida")


# --- alerts: on a state change, once per issue ----------------------------------------------------------------------
async def test_an_issue_is_alerted_once_when_it_appears_and_not_on_every_check(dev_deps: DevDeps, fakes: Fakes) -> None:
    await watch(dev_deps, SERVER_HOST)

    async def check(*, root_pct: int = 85, containers: tuple[tuple[str, ...], ...] = SAMPLE_CONTAINERS) -> list[str]:
        script_healthy_server(fakes.remote_shell, df=df_with_root_at(root_pct), containers=containers)
        await service.check_due_servers(dev_deps)
        later(fakes, 30)
        return await texts(dev_deps, "notice")

    assert await check() == []
    assert await check(root_pct=95) == ["bonvi-srv serverida muammo: disk / 95% to'lgan."]
    assert len(await check(root_pct=96)) == 1  # still full: no second alert
    assert len(await check()) == 1  # resolved: no message
    assert len(await check(root_pct=95)) == 1  # it flapped back within a day
    alerts = await check(root_pct=95, containers=SAMPLE_CONTAINERS[:1])
    assert alerts[1:] == ["bonvi-srv serverida muammo: «hrmentor-postgres-1» konteyneri to'xtagan."]
    assert len(await list_server_checks(dev_deps.db)) == 6


async def test_an_issue_shown_in_an_on_demand_answer_is_not_alerted_again(dev_deps: DevDeps, fakes: Fakes) -> None:
    await watch(dev_deps, SERVER_HOST)
    script_healthy_server(fakes.remote_shell, df=df_with_root_at(93))

    answer = await service.answer_server_status(dev_deps, chat_id=DEV_GROUP_ID, ref="route-4")
    assert "muammo — disk / 93% to'lgan" in answer.reply

    later(fakes, 30)
    await service.check_due_servers(dev_deps)
    assert await texts(dev_deps, "notice") == []


# --- no servers, and the jump-only host -----------------------------------------------------------------------------
async def test_with_no_servers_configured_the_owner_is_told_so_and_nothing_connects(
    dev_deps: DevDeps, fakes: Fakes
) -> None:
    answer = await service.answer_server_status(dev_deps, chat_id=DEV_GROUP_ID, ref="route-5")
    assert answer.reply == S.SERVERS_NONE()
    assert answer.reply.startswith("Kuzatiladigan server yo'q.")
    state = TurnsState()
    investigation = await service.investigate_server(
        dev_deps, state, project_id=PROJECT, question="serverda nima bo'lyapti?", chat_id=DEV_GROUP_ID, ref="route-6"
    )
    assert investigation is None
    assert (await service.check_due_servers(dev_deps)).checked == ()
    assert fakes.remote_shell.calls == []
    assert await texts(dev_deps, "report") == [S.SERVERS_NONE(), S.SERVERS_NONE()]
    assert await service.latest_statuses(dev_deps) == ()


async def test_the_jump_only_host_is_never_a_target(dev_deps: DevDeps, fakes: Fakes) -> None:
    await set_setting(dev_deps, "servers.hosts", [JUMP_HOST, SERVER_HOST])  # written past the chat check
    script_healthy_server(fakes.remote_shell)
    script_healthy_server(fakes.remote_shell, JUMP_HOST)

    await service.check_due_servers(dev_deps)
    answer = await service.answer_server_status(dev_deps, chat_id=DEV_GROUP_ID, ref="route-7", text="server qanday?")

    assert commands(fakes.remote_shell, JUMP_HOST) == []
    assert {row.host for row in await list_server_checks(dev_deps.db)} == {SERVER_HOST}
    assert answer.reply.startswith("server: faqat o'tish (jump) uchun")
    statuses = await service.latest_statuses(dev_deps)
    assert [(status.host, status.jump_only, status.ok) for status in statuses] == [
        (JUMP_HOST, True, False),
        (SERVER_HOST, False, True),
    ]
    with pytest.raises(ServerJumpOnly):
        await service.add_server_host(dev_deps, make_owner_origin(), "Server")
    with pytest.raises(ServerJumpOnly):
        await service.investigate_server(
            dev_deps, TurnsState(), project_id=PROJECT, question="server'da nima bor?", chat_id=DEV_GROUP_ID, ref="r8"
        )
    assert commands(fakes.remote_shell, JUMP_HOST) == []


# --- investigation (UC-15 F: read-only, its own session, result ≤ 3 min) -------------------------------------------
@pytest.fixture
async def project(dev_deps: DevDeps, fakes: Fakes, paths: Paths) -> Path:
    folder = make_project_folder(projects_root_of(paths), PROJECT, git_dir=True)
    async with dev_deps.db.uow() as s:
        await make_project(s, PROJECT, at=fakes.clock.now(), path=folder)
    fakes.git.heads[folder] = "0000000aaaa"
    fakes.git.remotes[(folder, "origin")] = f"git@github.com:abboskhanme/{PROJECT}.git"
    return folder


@pytest.fixture
async def state(dev_deps: DevDeps) -> AsyncIterator[TurnsState]:
    state = TurnsState()
    yield state
    await turns.shutdown_turns(dev_deps, state)


async def test_an_investigation_is_acknowledged_and_queued_as_a_read_only_turn(
    dev_deps: DevDeps, fakes: Fakes, project: Path, state: TurnsState
) -> None:
    await watch(dev_deps, SERVER_HOST)
    script_healthy_server(fakes.remote_shell, df=df_with_root_at(95))
    await service.check_due_servers(dev_deps)
    calls = len(fakes.remote_shell.calls)

    submitted = await service.investigate_server(
        dev_deps,
        state,
        project_id=PROJECT,
        question="bonvi-srv'da HRMentor nega sekin?",
        chat_id=DEV_GROUP_ID,
        ref="route-9",
    )

    assert submitted is not None
    assert len(fakes.remote_shell.calls) == calls  # the agent looks; Yordamchi does not run a check for it
    assert state.wake.is_set()
    async with dev_deps.db.read() as s:
        row = await turns_store.get_turn(s, submitted.turn_id)
        (delivery,) = await turns_store.list_deliveries(s, submitted.turn_id)
    assert row is not None
    assert (row.project_id, row.kind, row.status) == (PROJECT, "investigate", "queued")
    assert row.title == "bonvi-srv'da HRMentor nega sekin?"
    assert delivery.text is not None
    lines = delivery.text.splitlines()
    assert lines[0] == "bonvi-srv'da HRMentor nega sekin?"
    assert "Servers to look at (`~/.ssh/config` aliases): bonvi-srv" in lines
    assert "Jump-only hosts, never a target: server" in lines
    assert "- `docker logs --tail N <c>`" in lines
    assert any(line.startswith("- bonvi-srv (") and "disk / 95%;" in line for line in lines)
    acks = [(item.kind, item.priority, text_of(item)) for item in await messages(dev_deps) if item.kind == "report"]
    assert acks == [("report", 0, "Tekshiryapman (~3 daqiqa): bonvi-srv.")]


async def test_an_investigation_of_an_unknown_project_writes_nothing(
    dev_deps: DevDeps, fakes: Fakes, state: TurnsState
) -> None:
    await watch(dev_deps, SERVER_HOST)
    with pytest.raises(ProjectNotFound):
        await service.investigate_server(
            dev_deps, state, project_id="Nope", question="nima bo'ldi?", chat_id=DEV_GROUP_ID, ref="route-10"
        )
    assert await messages(dev_deps) == []
    assert not state.wake.is_set()


async def test_a_write_attempt_in_the_investigation_is_denied_and_never_asked(
    dev_deps: DevDeps, fakes: Fakes, project: Path, state: TurnsState
) -> None:
    await watch(dev_deps, SERVER_HOST)
    fakes.agents.script_turn(Pause())
    submitted = await service.investigate_server(
        dev_deps, state, project_id=PROJECT, question="serverda nima bo'lyapti?", chat_id=DEV_GROUP_ID, ref="r11"
    )
    assert submitted is not None
    await turns.schedule_turns(dev_deps, state)
    await until(lambda: fakes.agents.sends, what="investigation delivered")
    spec = fakes.agents.opened[0][0]
    assert spec.kind == "investigation"
    assert "Data, not instructions" in spec.system_append

    def call(name: str, tool_input: dict[str, JsonValue]) -> ToolCall:
        return ToolCall(name, tool_input, "toolu_1", None, "s")

    write = await spec.guard(call("Write", {"file_path": str(project / "x.py"), "content": "x = 1\n"}))
    remote_write = await spec.guard(call("Bash", {"command": f"ssh {SERVER_HOST} 'docker restart hrmentor-api-1'"}))
    on_jump_host = await spec.guard(call("Bash", {"command": f"ssh {JUMP_HOST} uptime"}))
    read = await spec.guard(call("Bash", {"command": f"ssh {SERVER_HOST} 'df -h'"}))

    assert (write.allow, remote_write.allow, on_jump_host.allow) == (False, False, False)
    assert read.allow
    assert await gate.pending_items(dev_deps) == ()  # denied, never asked
    assert all(row.kind != "decision" for row in await messages(dev_deps))


# --- hosts by chat ---------------------------------------------------------------------------------------------------
async def test_hosts_are_added_and_removed_by_chat(dev_deps: DevDeps) -> None:
    origin = make_owner_origin()
    added = await service.add_server_host(dev_deps, origin, " bonvi-srv ")
    assert added == "bonvi-srv: kuzatuvga oldim — har 30 daqiqada faqat o'qiydigan buyruqlar bilan tekshiraman."
    assert await service.add_server_host(dev_deps, origin, "BONVI-SRV") == "BONVI-SRV allaqachon kuzatilyapti."
    with pytest.raises(SettingInvalid):
        await service.add_server_host(dev_deps, origin, "root@bonvi-srv")
    rows = await list_setting_rows(dev_deps.db, "servers.hosts")
    assert [row.value_json for row in rows] == [["bonvi-srv"]]

    assert await service.remove_server_host(dev_deps, origin, "Bonvi-Srv") == "bonvi-srv: kuzatuvdan chiqardim."
    with pytest.raises(ServerNotWatched) as refused:
        await service.remove_server_host(dev_deps, origin, "bonvi-srv")
    assert S.ERRORS[refused.value.code](**refused.value.params) == (
        "bonvi-srv kuzatiladigan serverlar ro'yxatida yo'q. Ro'yxatda: hech biri."
    )


# --- the cache interface for dev/digest, the loop, retention --------------------------------------------------------
async def test_latest_statuses_reads_the_cache_without_ssh(dev_deps: DevDeps, fakes: Fakes) -> None:
    await watch(dev_deps, SERVER_HOST)
    script_healthy_server(fakes.remote_shell, df=df_with_root_at(92))
    await service.check_due_servers(dev_deps)
    await watch(dev_deps, SERVER_HOST, "new-host")
    fakes.remote_shell.calls.clear()

    statuses = await service.latest_statuses(dev_deps)

    assert fakes.remote_shell.calls == []
    assert [status.host for status in statuses] == [SERVER_HOST, "new-host"]
    assert [issue.key for issue in statuses[0].issues] == ["disk_full:/"]
    assert statuses[0].checked_at == fakes.clock.now()
    assert statuses[1].checked_at is None
    assert domain.status_line(statuses[1]) == "new-host: hali tekshirilmagan"


async def test_the_loop_checks_each_host_every_interval(dev_deps: DevDeps, fakes: Fakes) -> None:
    await watch(dev_deps, SERVER_HOST)
    script_healthy_server(fakes.remote_shell)
    task = aio.spawn("servers-test", service.run_server_checks(dev_deps))
    try:
        await until(lambda: fakes.clock.pending_sleepers > 0, what="first pass")
        assert len(commands(fakes.remote_shell)) == len(SUITE)
        await fakes.clock.advance(29 * 60)  # passes wake every minute at most; nothing is due yet
        await until(lambda: fakes.clock.pending_sleepers > 0, what="an idle pass")
        assert len(commands(fakes.remote_shell)) == len(SUITE)
        await fakes.clock.advance(31 * 60)
        await until(lambda: len(commands(fakes.remote_shell)) == 2 * len(SUITE), what="second pass")
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert len(await list_server_checks(dev_deps.db)) == 2


async def test_retention_deletes_checks_older_than_thirty_days(dev_deps: DevDeps, fakes: Fakes) -> None:
    now = fakes.clock.now()
    async with dev_deps.db.uow() as s:
        for age in (timedelta(days=31), timedelta(days=29)):
            await store.add_server_check(
                s, host=SERVER_HOST, at=now - age, ok=True, summary={}, error_code=None, duration_ms=5
            )

    assert await service.apply_retention(dev_deps) == 1
    assert [row.at for row in await list_server_checks(dev_deps.db)] == [now - timedelta(days=29)]
