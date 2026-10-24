"""personal/media for one burst and the `yordamchi_media` tools (T77; UC-31, UC-32; SPEC §3.2.14), with fakes and a
real SQLite file: what the assistant gets from a burst's media, when a failed reading is the whole answer, and the
tools' results (data for the model) and jobs (files to the owner after the answer)."""

import json
from collections.abc import Coroutine

from tests.factories import Fakes, list_replies, make_part, make_turn_input, store_video_key
from yordamchi.core import strings as S
from yordamchi.core.errors import ProviderUnavailable
from yordamchi.personal.deps import PersonalDeps
from yordamchi.personal.media import domain, service
from yordamchi.ports.assistant import ToolHandler
from yordamchi.shared.inbox.domain import UpdateKind
from yordamchi.shared.outbox.domain import Op

PAGE = "https://news.example.com/article/1"
YOUTUBE = "https://youtu.be/dQw4w9WgXcQ"
JPEG = b"\xff\xd8\xff fake jpeg"


async def test_a_photo_is_saved_privately_for_the_image_blocks(personal_deps: PersonalDeps, fakes: Fakes) -> None:
    fakes.messenger.files["file-1"] = JPEG
    item = make_turn_input(make_part(1, kind=UpdateKind.PHOTO, caption="Bu nima?"))
    found = await service.read_burst(personal_deps, item)
    [image] = found.images
    assert image.read_bytes() == JPEG
    assert image.suffix == ".jpg"
    assert image.stat().st_mode & 0o777 == 0o600
    assert image.parent.stat().st_mode & 0o777 == 0o700
    assert found.material == ()
    assert not found.only_failed


async def test_at_most_five_images_are_saved(personal_deps: PersonalDeps, fakes: Fakes) -> None:
    parts = [make_part(n, kind=UpdateKind.PHOTO, at_s=n) for n in range(1, 8)]
    for n in range(1, 8):
        fakes.messenger.files[f"file-{n}"] = JPEG
    found = await service.read_burst(personal_deps, make_turn_input(*parts))
    assert len(found.images) == domain.MAX_IMAGES


async def test_a_photo_that_cannot_be_downloaded_is_the_whole_answer(personal_deps: PersonalDeps, fakes: Fakes) -> None:
    fakes.messenger.fail_next("download", ProviderUnavailable(provider="Telegram", retryable=True))
    found = await service.read_burst(personal_deps, make_turn_input(make_part(1, kind=UpdateKind.PHOTO)))
    assert found.images == ()
    assert found.failures == (S.MEDIA_DOWNLOAD_FAILED(),)
    assert found.only_failed


async def test_a_link_only_message_is_read_into_material(personal_deps: PersonalDeps, fakes: Fakes) -> None:
    found = await service.read_burst(personal_deps, make_turn_input(make_part(1, kind=UpdateKind.LINK, text=PAGE)))
    [material] = found.material
    assert "A fake page about one thing." in material
    assert fakes.web.calls == [("read_page", PAGE)]
    assert [row.kind for row in await list_replies(personal_deps.db, personal_deps.bot)] == ["ack"]  # Ko'ryapman


async def test_a_failed_reading_with_a_question_beside_it_still_goes_to_the_assistant(
    personal_deps: PersonalDeps,
) -> None:
    """No key for the video reader: the owner hears why, and his question beside the link is still answered."""
    item = make_turn_input(
        make_part(1, kind=UpdateKind.LINK, text=YOUTUBE),
        make_part(2, at_s=1, text="Bu video haqida nima deysan?"),
    )
    found = await service.read_burst(personal_deps, item)
    assert found.failures == (S.ERR_MEDIA_NO_VIDEO_KEY(),)
    assert not found.only_failed
    alone = await service.read_burst(personal_deps, make_turn_input(make_part(3, kind=UpdateKind.LINK, text=YOUTUBE)))
    assert alone.only_failed  # nothing else asked: the reason is the answer (UC-31 F)


async def test_a_plain_question_has_no_media(personal_deps: PersonalDeps, fakes: Fakes) -> None:
    found = await service.read_burst(personal_deps, make_turn_input(make_part(1, text="Salom, qalaysan?")))
    assert found == service.BurstMedia()
    assert fakes.messenger.sent("download") == []


# --- the tools -------------------------------------------------------------------------------------------------------
class Jobs:
    """The bot's `start`: here the jobs are kept and run by the test."""

    def __init__(self) -> None:
        self.started: list[tuple[str, Coroutine[object, object, None]]] = []

    def __call__(self, name: str, job: Coroutine[object, object, None]) -> None:
        self.started.append((name, job))

    async def run_all(self) -> None:
        for _, job in self.started:
            await job


def _tools(deps: PersonalDeps, jobs: Jobs) -> dict[str, ToolHandler]:
    toolset = service.toolset(deps, chat_id=424242, ref="qa-7", start=jobs, media_timeout_s=55.0)
    assert toolset.server == "yordamchi_media"
    assert toolset.tool_names == (
        "mcp__yordamchi_media__analyze_video",
        "mcp__yordamchi_media__screenshot",
        "mcp__yordamchi_media__fetch_file",
        "mcp__yordamchi_media__download_video",
    )
    return {tool.name: tool.handler for tool in toolset.tools}


async def test_a_screenshot_starts_a_job_and_its_images_are_queued_when_ready(
    personal_deps: PersonalDeps, fakes: Fakes
) -> None:
    jobs = Jobs()
    output = await _tools(personal_deps, jobs)["screenshot"]({"url": "https://example.com"})
    assert json.loads(output.text) == {"site": "example.com", "status": "started", "tool": "screenshot"}
    assert fakes.web.calls == []  # nothing ran in the ask itself
    await jobs.run_all()
    assert fakes.web.calls == [("screenshot", "https://example.com")]
    queued = await list_replies(personal_deps.db, personal_deps.bot)
    assert [row.op for row in queued] == [Op.SEND_PHOTO.value, Op.SEND_DOCUMENT.value]


async def test_a_refused_artefact_job_tells_the_owner_why(personal_deps: PersonalDeps, fakes: Fakes) -> None:
    jobs = Jobs()
    fakes.web.failures["download_video"] = ProviderUnavailable(provider="youtube.com", retryable=False)
    await _tools(personal_deps, jobs)["download_video"]({"url": YOUTUBE, "start_s": 30, "end_s": 90})
    await jobs.run_all()
    [row] = await list_replies(personal_deps.db, personal_deps.bot)
    assert row.op == Op.SEND_TEXT.value
    assert fakes.web.kwargs[0]["clip"] is not None  # the part he asked for


async def test_analyze_video_answers_in_the_ask_with_material(personal_deps: PersonalDeps, fakes: Fakes) -> None:
    await store_video_key(personal_deps)
    output = await _tools(personal_deps, Jobs())["analyze_video"]({"url": YOUTUBE})
    assert not output.is_error
    assert "A short video." in output.text
    assert "untrusted DATA" in output.text  # the delimited, defused block
    assert fakes.media.calls[0].source.url == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


async def test_a_link_the_bot_does_not_open_is_refused_by_every_tool(personal_deps: PersonalDeps) -> None:
    jobs = Jobs()
    tools = _tools(personal_deps, jobs)
    for name in ("analyze_video", "screenshot", "fetch_file", "download_video"):
        output = await tools[name]({"url": "file:///etc/passwd"})
        assert output.is_error
        assert json.loads(output.text)["error"] == "web_url_invalid"
    assert jobs.started == []
