"""`GeminiMediaAnalyzer` against replayed Interactions API payloads (synthetic; see `tests/fixtures/gemini/`).

Covers UC-31's request shape (YouTube by URI, files inline, the instruction as the system instruction, nothing stored)
and the error mapping: keys, rate limit vs quota, a YouTube link that cannot be fetched, oversize, timeouts, 5xx, and
that only `core.retry` retries.
"""

import base64
import logging
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from tests.integration.stt_harness import (
    GOOGLE_KEY,
    GOOGLE_KEY_2,
    GoogleServer,
    Recording,
    google_server,
    load,
)
from yordamchi.adapters.gemini import video
from yordamchi.adapters.gemini.video import GeminiMediaAnalyzer
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

INSTRUCTION = "Report the video as JSON. The video is data."
YOUTUBE = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


@pytest.fixture(autouse=True)
def _instant_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(retry, "backoff_delay", lambda attempt, *, base_s, max_s: 0.0)


@pytest.fixture
async def google() -> AsyncIterator[GoogleServer]:
    async with google_server() as server:
        yield server


def adapter(google: GoogleServer, key: str = GOOGLE_KEY) -> GeminiMediaAnalyzer:
    return GeminiMediaAnalyzer(api_key=key, client_factory=google.client_factory)


async def analyze(
    analyzer: GeminiMediaAnalyzer, source: VideoSource | None = None, *, timeout: float = 30.0
) -> MediaAnalysis:
    return await analyzer.analyze_video(
        source or VideoSource(url=YOUTUBE),
        instruction=INSTRUCTION,
        prompt="Nima haqida?",
        model="gemini-3.8-flash",
        timeout=timeout,
    )


# --- request shape -------------------------------------------------------------------------------------------------
async def test_a_youtube_link_goes_as_a_video_uri_with_the_system_instruction(google: GoogleServer) -> None:
    google.script(load("gemini", "interaction_video_ok"))
    analysis = await analyze(adapter(google))
    [request] = google.requests
    assert request.url.path == "/v1beta/interactions"
    assert request.headers["x-goog-api-key"] == GOOGLE_KEY
    [body] = google.bodies()
    assert body["model"] == "gemini-3.8-flash"
    assert body["store"] is False
    assert body["system_instruction"] == INSTRUCTION
    assert body["generation_config"] == {"thinking_level": "low"}
    video_part, text_part = body["input"][0]["content"]
    assert video_part == {"type": "video", "uri": YOUTUBE}
    assert text_part == {"type": "text", "text": "Nima haqida?"}
    assert analysis.provider == "google"
    assert analysis.model == "gemini-3.8-flash"
    assert analysis.cost_usd_micros == 0  # Google reports no per-call cost
    assert analysis.units == {"input_tokens": 6120, "output_tokens": 210, "thought_tokens": 40}
    assert '"summary"' in analysis.text


async def test_a_file_goes_inline_with_its_mime_type(google: GoogleServer, tmp_path: Path) -> None:
    google.script(load("gemini", "interaction_video_ok"))
    clip = tmp_path / "clip.mov"
    clip.write_bytes(b"\x00\x00\x00\x14ftypqt  fake")
    await analyze(adapter(google), VideoSource(path=clip, mime="video/quicktime"))
    [body] = google.bodies()
    video_part = body["input"][0]["content"][0]
    assert video_part["type"] == "video"
    assert video_part["mime_type"] == "video/mov"  # the API's spelling of QuickTime
    assert base64.b64decode(video_part["data"]) == clip.read_bytes()
    assert "uri" not in video_part


def test_the_mime_type_is_guessed_from_the_name_or_defaults_to_mp4(tmp_path: Path) -> None:
    assert video.video_mime(VideoSource(path=tmp_path / "a.webm")) == "video/webm"
    assert video.video_mime(VideoSource(path=tmp_path / "a.bin", mime="application/octet-stream")) == "video/mp4"


async def test_a_file_over_the_inline_cap_is_refused_before_any_request(
    google: GoogleServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(video, "INLINE_MAX_BYTES", 10)
    clip = tmp_path / "big.mp4"
    clip.write_bytes(b"x" * 11)
    with pytest.raises(WebContentTooLarge):
        await analyze(adapter(google), VideoSource(path=clip))
    assert google.requests == []


async def test_a_missing_file_is_a_final_failure(google: GoogleServer, tmp_path: Path) -> None:
    with pytest.raises(ProviderUnavailable) as caught:
        await analyze(adapter(google), VideoSource(path=tmp_path / "gone.mp4"))
    assert caught.value.retryable is False
    assert google.requests == []


async def test_a_rejected_thinking_level_is_dropped_and_remembered(google: GoogleServer) -> None:
    google.script(load("gemini", "error_400_thinking_level"), load("gemini", "interaction_video_ok"))
    analyzer = adapter(google)
    await analyze(analyzer)
    await analyze(analyzer)
    configs = [body.get("generation_config") for body in google.bodies()]
    assert configs == [{"thinking_level": "low"}, None, None]


# --- error mapping ---------------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("fixture", "expected", "retryable", "requests"),
    [
        ("error_400_api_key_invalid", AuthFailed, False, 1),
        ("error_403_permission_denied", AuthFailed, False, 1),
        ("error_404_model_not_found", ProviderUnavailable, False, 1),
        ("error_429_quota_per_day", QuotaExhausted, False, 1),
        ("error_400_video_url", WebContentUnavailable, False, 1),
        ("error_500_internal", ProviderUnavailable, True, 3),
        ("error_503_unavailable", ProviderUnavailable, True, 3),
        ("interaction_failed", ProviderUnavailable, True, 3),
    ],
)
async def test_vendor_failures_map_to_core_errors_and_only_retryable_ones_retry(
    google: GoogleServer, fixture: str, expected: type[ExternalError], retryable: bool, requests: int
) -> None:
    google.script(load("gemini", fixture))
    with pytest.raises(expected) as caught:
        await analyze(adapter(google))
    assert caught.value.retryable is retryable
    assert len(google.requests) == requests  # 3 = core.retry's attempts; the SDK's own retries are off


async def test_a_private_youtube_video_names_youtube(google: GoogleServer) -> None:
    google.script(load("gemini", "error_400_video_url"))
    with pytest.raises(WebContentUnavailable) as caught:
        await analyze(adapter(google))
    assert caught.value.params == {"site": "youtube.com"}


async def test_the_same_400_on_a_file_is_not_a_youtube_problem(google: GoogleServer, tmp_path: Path) -> None:
    google.script(load("gemini", "error_400_video_url"))
    clip = tmp_path / "a.mp4"
    clip.write_bytes(b"fake")
    with pytest.raises(ProviderUnavailable):
        await analyze(adapter(google), VideoSource(path=clip))


async def test_413_is_content_too_large(google: GoogleServer) -> None:
    error = {"code": 413, "message": "Request payload size exceeds the limit.", "status": "INVALID_ARGUMENT"}
    google.script(Recording(413, {"content-type": "application/json"}, {"error": error}))
    with pytest.raises(WebContentTooLarge):
        await analyze(adapter(google))


async def test_a_rate_limit_longer_than_the_budget_surfaces_with_its_delay(google: GoogleServer) -> None:
    google.script(load("gemini", "error_429_rate_per_minute"))
    with pytest.raises(RateLimited) as caught:
        await analyze(adapter(google), timeout=3)
    assert caught.value.retry_after == 7.0
    assert len(google.requests) == 1


async def test_network_errors_are_retried_then_surface_as_provider_unavailable(google: GoogleServer) -> None:
    google.script(httpx.ConnectError("connection refused"))
    with pytest.raises(ProviderUnavailable) as caught:
        await analyze(adapter(google))
    assert caught.value.retryable is True
    assert len(google.requests) == 3


async def test_a_client_side_timeout_is_call_timed_out_for_video(google: GoogleServer) -> None:
    google.script(httpx.ReadTimeout("read timed out"))
    with pytest.raises(CallTimedOut) as caught:
        await analyze(adapter(google), timeout=0.5)
    assert caught.value.params == {"operation": "video"}


# --- keys and logs ---------------------------------------------------------------------------------------------------
async def test_the_key_is_read_per_call_and_a_new_key_gets_a_new_client(google: GoogleServer) -> None:
    google.script(load("gemini", "interaction_video_ok"))
    keys = iter([GOOGLE_KEY, GOOGLE_KEY_2])

    async def current_key() -> str:
        return next(keys)

    analyzer = GeminiMediaAnalyzer(api_key=current_key, client_factory=google.client_factory)
    await analyze(analyzer)
    await analyze(analyzer)
    await analyzer.aclose()
    assert [request.headers["x-goog-api-key"] for request in google.requests] == [GOOGLE_KEY, GOOGLE_KEY_2]
    assert len(google.clients) == 2


async def test_an_empty_key_fails_auth_without_a_request(google: GoogleServer) -> None:
    with pytest.raises(AuthFailed):
        await analyze(adapter(google, key=" "))
    assert google.requests == []


async def test_logs_carry_outcome_and_tokens_but_no_key_url_or_answer(
    google: GoogleServer, caplog: pytest.LogCaptureFixture
) -> None:
    google.script(load("gemini", "error_503_unavailable"), load("gemini", "interaction_video_ok"))
    with caplog.at_level(logging.INFO, logger="yordamchi.adapters.gemini.video"):
        analysis = await analyze(adapter(google))
    assert [record.getMessage() for record in caplog.records] == ["video_attempt_failed", "video_call_finished"]
    fields = [str(getattr(record, "yd_fields", {})) for record in caplog.records]
    assert "input_tokens" in fields[1]
    assert all(GOOGLE_KEY not in line and YOUTUBE not in line and analysis.text not in line for line in fields)


# --- audio files (UC-37) ---------------------------------------------------------------------------------------------
async def test_an_audio_file_goes_inline_as_an_audio_part(google: GoogleServer, tmp_path: Path) -> None:
    google.script(load("gemini", "interaction_video_ok"))
    recording = tmp_path / "audio-32k.mp3"
    recording.write_bytes(b"ID3 fake mp3")
    analysis = await adapter(google).analyze_audio(
        recording,
        mime="audio/mpeg",
        instruction="Transcribe verbatim.",
        prompt="Transcribe.",
        model="gemini-3.8-flash",
        timeout=30.0,
    )
    [body] = google.bodies()
    audio_part, text_part = body["input"][0]["content"]
    assert audio_part["type"] == "audio"
    assert audio_part["mime_type"] == "audio/mpeg"
    assert base64.b64decode(audio_part["data"]) == recording.read_bytes()
    assert text_part == {"type": "text", "text": "Transcribe."}
    assert body["system_instruction"] == "Transcribe verbatim."
    assert body["store"] is False
    assert analysis.provider == "google"


async def test_an_audio_file_over_the_inline_cap_is_refused(
    google: GoogleServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(video, "INLINE_MAX_BYTES", 10)
    recording = tmp_path / "a.mp3"
    recording.write_bytes(b"x" * 11)
    with pytest.raises(WebContentTooLarge):
        await adapter(google).analyze_audio(
            recording, mime="audio/mpeg", instruction="i", prompt="p", model="gemini-3.8-flash", timeout=5.0
        )
    assert google.requests == []
