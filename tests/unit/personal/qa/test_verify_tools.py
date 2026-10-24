"""`qa.service.verify_tools` (T77; NFR-SEC-08, SPEC §5.3 personal "start check", spike S5): one throwaway ask with
exactly the tools a real answer gets. A refused tool list raises (the bot refuses to start); Claude out of reach is
`False` (the first answer makes the same check); the throwaway conversation is always closed and never stored."""

import pytest

from tests.factories import OWNER_ID, Fakes
from yordamchi.core.errors import AuthFailed, PersonalToolsMismatch
from yordamchi.personal.deps import PersonalDeps
from yordamchi.personal.qa import service, store
from yordamchi.ports.assistant import AssistantTool, AssistantToolset

MEDIA = AssistantToolset(
    server="yordamchi_media",
    tools=(AssistantTool(name="screenshot", description="x", input_schema={"type": "object"}),),
)


async def test_a_session_that_passes_is_verified_and_closed(personal_deps: PersonalDeps, fakes: Fakes) -> None:
    assert await service.verify_tools(personal_deps, toolsets=(MEDIA,)) is True
    [probe] = fakes.assistant.requests
    assert probe.kind == "qa"
    assert [toolset.server for toolset in probe.toolsets] == ["yordamchi_bridge", "yordamchi_media"]
    assert "start-up check" in probe.prompt
    assert fakes.assistant.resets == [("qa", probe.conversation)]
    async with personal_deps.db.read() as s:
        assert await store.find_live_session(s, 0) is None  # never one of the owner's conversations


async def test_a_refused_tool_list_raises(personal_deps: PersonalDeps, fakes: Fakes) -> None:
    fakes.assistant.reply_with("qa", PersonalToolsMismatch())
    with pytest.raises(PersonalToolsMismatch):
        await service.verify_tools(personal_deps, toolsets=(MEDIA,))
    assert len(fakes.assistant.resets) == 1


async def test_claude_out_of_reach_is_unverified_not_refused(personal_deps: PersonalDeps, fakes: Fakes) -> None:
    fakes.assistant.reply_with("qa", AuthFailed(provider="claude"))
    assert await service.verify_tools(personal_deps) is False


async def test_the_warm_session_has_the_answers_tools(personal_deps: PersonalDeps, fakes: Fakes) -> None:
    """So the first answer reuses the warm process instead of relaunching it for another tool list."""
    await service.warm_conversation(personal_deps, OWNER_ID, toolsets=(MEDIA,))
    [profile] = fakes.assistant.warmed
    assert [toolset.server for toolset in profile.toolsets] == ["yordamchi_bridge", "yordamchi_media"]
