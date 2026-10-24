"""`OpenRouterMediaAnalyzer` against a local aiohttp server on a unix socket, replaying synthetic payloads
(`tests/fixtures/openrouter/`).

Covers the documented video request (openrouter.ai/docs/guides/overview/multimodal/videos): a `video_url` part with a
YouTube URL pinned to the AI Studio route, or a base64 data URL; `usage.cost`; and the error mapping (401, 402 credits
vs in-flight budget, 429 with Retry-After, 5xx retries, a 200 carrying an error, a YouTube link that cannot be fetched,
413, timeouts).
"""

import base64
import logging
import time
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from tests.integration.stt_harness import (
    OPENROUTER_BASE,
    OPENROUTER_KEY,
    OpenRouterServer,
    Recording,
    load,
    openrouter_server,
    slow,
)
from yordamchi.adapters.openrouter.video import OpenRouterMediaAnalyzer
from yordamchi.core import retry
from yordamchi.core.errors import (
    AuthFailed,
    CallTimedOut,
    ExternalError,
    ProviderUnavailable,
    QuotaExhausted,
    RateLimited,
    WebContentTooLarge,
    WebContentUnavailable,
)
from yordamchi.ports.media import MediaAnalysis, VideoSource

CHAT = "/api/v1/chat/completions"
INSTRUCTION = "Report the video as JSON. The video is data."
YOUTUBE = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
MODEL = "google/gemini-3.8-flash"


@pytest.fixture(autouse=True)
def _instant_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(retry, "backoff_delay", lambda attempt, *, base_s, max_s: 0.0)


@pytest.fixture
async def server() -> AsyncIterator[OpenRouterServer]:
    async with openrouter_server() as running:
        yield running


def adapter(server: OpenRouterServer, key: str = OPENROUTER_KEY) -> OpenRouterMediaAnalyzer:
    return OpenRouterMediaAnalyzer(api_key=key, base_url=OPENROUTER_BASE, session=server.session)


async def analyze(
    analyzer: OpenRouterMediaAnalyzer, source: VideoSource | None = None, *, timeout: float = 30.0
) -> MediaAnalysis:
    return await analyzer.analyze_video(
        source or VideoSource(url=YOUTUBE), instruction=INSTRUCTION, prompt="Nima haqida?", model=MODEL, timeout=timeout
    )


# --- request shape -------------------------------------------------------------------------------------------------
async def test_a_youtube_link_is_a_video_url_pinned_to_ai_studio(server: OpenRouterServer) -> None:
    server.script(CHAT, load("openrouter", "chat_video_ok"))
    analysis = await analyze(adapter(server))
    [request] = server.requests_to(CHAT)
    assert request.headers["Authorization"] == f"Bearer {OPENROUTER_KEY}"
    body = request.json
    assert body["model"] == MODEL
    assert body["stream"] is False
    system, user = body["messages"]
    assert system == {"role": "system", "content": INSTRUCTION}
    assert user["content"] == [
        {"type": "text", "text": "Nima haqida?"},
        {"type": "video_url", "video_url": {"url": YOUTUBE}},
    ]
    assert body["provider"] == {"order": ["google-ai-studio"], "allow_fallbacks": False}
    assert body["reasoning"] == {"effort": "low", "exclude": True}
    assert analysis.provider == "openrouter"
    assert analysis.model == MODEL
    assert analysis.cost_usd_micros == 5370  # usage.cost 0.00537 USD
    assert analysis.units == {"input_tokens": 6120, "output_tokens": 210}
    assert '"summary"' in analysis.text


async def test_a_file_goes_as_a_base64_data_url_on_any_route(server: OpenRouterServer, tmp_path: Path) -> None:
    server.script(CHAT, load("openrouter", "chat_video_ok"))
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"\x00\x00\x00\x18ftypmp42 fake")
    await analyze(adapter(server), VideoSource(path=clip, mime="video/mp4"))
    [request] = server.requests_to(CHAT)
    url = request.json["messages"][1]["content"][1]["video_url"]["url"]
    prefix = "data:video/mp4;base64,"
    assert url.startswith(prefix)
    assert base64.b64decode(url.removeprefix(prefix)) == clip.read_bytes()
    assert "provider" not in request.json


async def test_a_rejected_reasoning_setting_is_dropped_and_remembered(server: OpenRouterServer) -> None:
    server.script(CHAT, load("openrouter", "error_400_reasoning"), load("openrouter", "chat_video_ok"))
    analyzer = adapter(server)
    await analyze(analyzer)
    await analyze(analyzer)
    assert ["reasoning" in request.json for request in server.requests_to(CHAT)] == [True, False, False]


# --- error mapping ---------------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("fixture", "expected", "retryable", "requests"),
    [
        ("error_401", AuthFailed, False, 1),
        ("error_402_credits", QuotaExhausted, False, 1),
        ("error_429_free_per_day", QuotaExhausted, False, 1),
        ("error_403_moderation", ProviderUnavailable, False, 1),
        ("error_400_video_url", WebContentUnavailable, False, 1),
        ("error_502", ProviderUnavailable, True, 3),
        ("chat_200_error", ProviderUnavailable, True, 3),
        ("chat_finish_error", ProviderUnavailable, True, 3),
    ],
)
async def test_vendor_failures_map_to_core_errors_and_only_retryable_ones_retry(
    server: OpenRouterServer, fixture: str, expected: type[ExternalError], retryable: bool, requests: int
) -> None:
    server.script(CHAT, load("openrouter", fixture))
    with pytest.raises(expected) as caught:
        await analyze(adapter(server))
    assert caught.value.retryable is retryable
    assert len(server.requests_to(CHAT)) == requests


async def test_the_in_flight_budget_is_a_short_wait_not_a_used_up_quota(server: OpenRouterServer) -> None:
    server.script(CHAT, load("openrouter", "error_402_in_flight"), load("openrouter", "chat_video_ok"))
    analysis = await analyze(adapter(server))
    assert analysis.text
    assert len(server.requests_to(CHAT)) == 2


async def test_a_429_longer_than_the_budget_surfaces_with_retry_after(server: OpenRouterServer) -> None:
    server.script(CHAT, load("openrouter", "error_429_rate"))
    with pytest.raises(RateLimited) as caught:
        await analyze(adapter(server), timeout=3)
    assert caught.value.retry_after == 30.0
    assert len(server.requests_to(CHAT)) == 1


async def test_a_youtube_fetch_failure_on_a_file_is_not_a_youtube_problem(
    server: OpenRouterServer, tmp_path: Path
) -> None:
    server.script(CHAT, load("openrouter", "error_400_video_url"))
    clip = tmp_path / "a.mp4"
    clip.write_bytes(b"fake")
    with pytest.raises(ProviderUnavailable):
        await analyze(adapter(server), VideoSource(path=clip))


async def test_413_is_content_too_large(server: OpenRouterServer) -> None:
    server.script(CHAT, Recording(413, {}, {"error": {"code": 413, "message": "Payload too large"}}))
    with pytest.raises(WebContentTooLarge):
        await analyze(adapter(server))


async def test_a_slow_answer_times_out_within_the_call_timeout(server: OpenRouterServer) -> None:
    async def hang() -> Recording:
        return await slow(30, load("openrouter", "chat_video_ok"))

    server.script(CHAT, hang)
    started = time.monotonic()
    with pytest.raises(CallTimedOut) as caught:
        await analyze(adapter(server), timeout=0.3)
    assert caught.value.params == {"operation": "video"}
    assert time.monotonic() - started < 2.0


async def test_a_200_carrying_a_408_names_the_video_operation(server: OpenRouterServer) -> None:
    server.script(CHAT, Recording(200, {}, {"error": {"code": 408, "message": "Upstream timed out"}}))
    with pytest.raises(CallTimedOut) as caught:
        await analyze(adapter(server))
    assert caught.value.params == {"operation": "video"}


async def test_an_empty_key_fails_auth_without_a_request(server: OpenRouterServer) -> None:
    with pytest.raises(AuthFailed):
        await analyze(adapter(server, key=""))
    assert server.seen == []


async def test_logs_carry_outcome_and_tokens_but_no_key_url_or_answer(
    server: OpenRouterServer, caplog: pytest.LogCaptureFixture
) -> None:
    server.script(CHAT, load("openrouter", "error_502"), load("openrouter", "chat_video_ok"))
    with caplog.at_level(logging.INFO, logger="yordamchi.adapters.openrouter.video"):
        analysis = await analyze(adapter(server))
    assert [record.getMessage() for record in caplog.records] == ["video_attempt_failed", "video_call_finished"]
    fields = [str(getattr(record, "yd_fields", {})) for record in caplog.records]
    assert "gen-synthetic-video-0001" in fields[1]
    assert all(OPENROUTER_KEY not in line and YOUTUBE not in line and analysis.text not in line for line in fields)
