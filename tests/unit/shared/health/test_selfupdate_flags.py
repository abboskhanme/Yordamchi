"""The bots' side of a self-update (SPEC §3.2.12 steps 2, 5–7; UC-19 "0 lost messages"): the probation flag names
one release, the drain flag gets a drained marker back, and a probation poll only peeks: the offset never moves, so
Telegram keeps every owner message for whichever release ends up running."""

import json
import stat

from tests.factories import Fakes
from yordamchi.shared.deps import SharedDeps
from yordamchi.shared.health import domain, service
from yordamchi.shared.inbox import service as inbox


def write_flag(deps: SharedDeps, name: str, body: dict[str, object]) -> None:
    deps.paths.flags.mkdir(parents=True, exist_ok=True)
    (deps.paths.flags / name).write_text(json.dumps(body), encoding="utf-8")


async def test_probation_peeks_from_the_stored_offset_and_never_advances_it(dev_deps: SharedDeps) -> None:
    await inbox.advance_poll_offset(dev_deps, [40, 41])
    seen: list[int] = []

    async def peek(offset: int) -> int:
        seen.append(offset)
        return 3  # the owner wrote three messages during the update

    for _ in range(5):
        assert await service.probation_peek(dev_deps, peek) == 3
    assert seen == [42] * 5
    assert await inbox.get_poll_offset(dev_deps) == 42  # all three are still held by Telegram


async def test_the_probation_flag_is_ours_only_when_it_names_this_release(dev_deps: SharedDeps) -> None:
    assert await service.read_probation(dev_deps, "2026.10.25-bbbbbbb") is None
    write_flag(dev_deps, "probation.json", {"id": "u000001", "version": "2026.10.25-bbbbbbb", "nonce": "n1"})
    flag = await service.read_probation(dev_deps, "2026.10.25-bbbbbbb")
    assert flag == domain.ProbationFlag(update_id="u000001", version="2026.10.25-bbbbbbb", nonce="n1")
    assert await service.read_probation(dev_deps, "2026.10.24-aaaaaaa") is None  # the old release restarting


async def test_a_drained_bot_answers_the_drain_flag_with_its_id(dev_deps: SharedDeps, fakes: Fakes) -> None:
    assert await service.read_drain(dev_deps) is None
    write_flag(dev_deps, "drain.json", {"id": "u000002", "since": fakes.clock.now().isoformat(), "drain_max_min": 30})
    flag = await service.read_drain(dev_deps)
    assert flag is not None
    assert (flag.update_id, flag.since, flag.drain_max_min) == ("u000002", fakes.clock.now(), 30)
    await service.mark_drained(dev_deps, flag)
    marker = dev_deps.paths.drained_marker(dev_deps.bot)
    assert json.loads(marker.read_text(encoding="utf-8"))["id"] == "u000002"
    assert stat.S_IMODE(marker.stat().st_mode) == 0o600


def test_malformed_flags_are_ignored() -> None:
    assert domain.probation_for({"version": "v", "nonce": ""}, "v") is None
    assert domain.probation_for([1], "v") is None
    assert domain.drain_flag({"id": ""}) is None
    assert domain.drain_flag({"id": "u1", "since": "nope", "drain_max_min": True}) == domain.DrainFlag("u1", None, 30)
