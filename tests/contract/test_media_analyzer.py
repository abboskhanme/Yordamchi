"""The `MediaAnalyzer` contract: the fake and both adapters (offline, on synthetic payloads) behave the same, and the
real adapters pass it live with a key (`pytest -m live`, `YORDAMCHI_LIVE_{GOOGLE,OPENROUTER}_API_KEY`)."""

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from tests.fakes.media import FakeMediaAnalyzer
from tests.integration.stt_harness import (
    GOOGLE_KEY,
    OPENROUTER_BASE,
    OPENROUTER_KEY,
    GoogleServer,
    OpenRouterServer,
    Recording,
    google_server,
    live_key,
    load,
    openrouter_server,
    slow,
)
from yordamchi.adapters.gemini.video import GeminiMediaAnalyzer
from yordamchi.adapters.openrouter.video import OpenRouterMediaAnalyzer
from yordamchi.core import retry
from yordamchi.core.errors import CallTimedOut, ExternalError, QuotaExhausted
from yordamchi.ports.media import MediaAnalyzer, VideoSource

CHAT = "/api/v1/chat/completions"
INSTRUCTION = "Report what the video is, as JSON."
# "Me at the zoo" (19 s), YouTube's first upload: small, public and not going anywhere.
LIVE_VIDEO = "https://www.youtube.com/watch?v=jNQXAC9IVRw"


class MediaAnalyzerContract:
    @pytest.fixture
    def analyzer(self) -> MediaAnalyzer:
        raise NotImplementedError

    @pytest.fixture
    def model(self) -> str:
        return "m-1"

    @pytest.fixture
    def url(self) -> str:
        return "https://www.youtube.com/watch?v=jNQXAC9IVRw"

    async def test_analyze_returns_text_and_provider(self, analyzer: MediaAnalyzer, model: str, url: str) -> None:
        analysis = await analyzer.analyze_video(
            VideoSource(url=url), instruction=INSTRUCTION, prompt="nima haqida?", model=model, timeout=60.0
        )
        assert analysis.text
        assert analysis.provider in {"google", "openrouter"}
        assert analysis.cost_usd_micros >= 0
        assert analysis.latency_ms >= 0


class MediaAnalyzerFileContract:
    """Offline: a local file is read and sent; quota and timeouts surface as the same `core.errors` classes."""

    @pytest.fixture
    def analyzer(self) -> MediaAnalyzer:
        raise NotImplementedError

    @pytest.fixture
    def out_of_quota(self) -> MediaAnalyzer:
        raise NotImplementedError

    @pytest.fixture
    def too_slow(self) -> MediaAnalyzer:
        raise NotImplementedError

    async def test_a_local_file_is_analyzed(self, analyzer: MediaAnalyzer, tmp_path: Path) -> None:
        video = tmp_path / "video.mp4"
        video.write_bytes(b"\x00\x00\x00\x18ftypmp42 fake")
        analysis = await analyzer.analyze_video(
            VideoSource(path=video, mime="video/mp4"), instruction=INSTRUCTION, prompt="p", model="m-1", timeout=30.0
        )
        assert analysis.text

    async def test_quota_exhaustion_is_quota_exhausted_and_not_retryable(self, out_of_quota: MediaAnalyzer) -> None:
        with pytest.raises(QuotaExhausted) as caught:
            await out_of_quota.analyze_video(
                VideoSource(url="https://youtu.be/x"), instruction=INSTRUCTION, prompt="p", model="m", timeout=30.0
            )
        assert isinstance(caught.value, ExternalError)
        assert caught.value.retryable is False

    async def test_a_call_longer_than_its_timeout_is_call_timed_out(self, too_slow: MediaAnalyzer) -> None:
        with pytest.raises(CallTimedOut):
            await too_slow.analyze_video(
                VideoSource(url="https://youtu.be/x"), instruction=INSTRUCTION, prompt="p", model="m", timeout=0.3
            )


# --- the fake ------------------------------------------------------------------------------------------------------
class TestFakeMediaAnalyzer(MediaAnalyzerContract, MediaAnalyzerFileContract):
    @pytest.fixture
    def analyzer(self) -> MediaAnalyzer:
        return FakeMediaAnalyzer()

    @pytest.fixture
    def out_of_quota(self) -> MediaAnalyzer:
        fake = FakeMediaAnalyzer()
        fake.script(QuotaExhausted(provider="google"))
        return fake

    @pytest.fixture
    def too_slow(self) -> MediaAnalyzer:
        fake = FakeMediaAnalyzer()
        fake.script(CallTimedOut(operation="video"))
        return fake


def test_a_video_source_is_a_path_or_a_url(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="either"):
        VideoSource()
    with pytest.raises(ValueError, match="either"):
        VideoSource(path=tmp_path / "v.mp4", url="https://youtu.be/x")


# --- the adapters, offline -----------------------------------------------------------------------------------------
@pytest.fixture
def _instant_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(retry, "backoff_delay", lambda attempt, *, base_s, max_s: 0.0)


@pytest.mark.usefixtures("_instant_backoff")
class TestGeminiMediaAnalyzer(MediaAnalyzerContract, MediaAnalyzerFileContract):
    @pytest.fixture
    async def google(self) -> AsyncIterator[GoogleServer]:
        async with google_server() as server:
            yield server

    def _adapter(self, google: GoogleServer) -> GeminiMediaAnalyzer:
        return GeminiMediaAnalyzer(api_key=GOOGLE_KEY, client_factory=google.client_factory)

    @pytest.fixture
    def analyzer(self, google: GoogleServer) -> MediaAnalyzer:
        google.script(load("gemini", "interaction_video_ok"))
        return self._adapter(google)

    @pytest.fixture
    def out_of_quota(self, google: GoogleServer) -> MediaAnalyzer:
        google.script(load("gemini", "error_429_quota_per_day"))
        return self._adapter(google)

    @pytest.fixture
    def too_slow(self, google: GoogleServer) -> MediaAnalyzer:
        async def hang(request: httpx.Request) -> Recording:
            del request
            return await slow(30, load("gemini", "interaction_video_ok"))

        google.script(hang)
        return self._adapter(google)


@pytest.mark.usefixtures("_instant_backoff")
class TestOpenRouterMediaAnalyzer(MediaAnalyzerContract, MediaAnalyzerFileContract):
    @pytest.fixture
    async def server(self) -> AsyncIterator[OpenRouterServer]:
        async with openrouter_server() as running:
            yield running

    def _adapter(self, server: OpenRouterServer) -> OpenRouterMediaAnalyzer:
        return OpenRouterMediaAnalyzer(api_key=OPENROUTER_KEY, base_url=OPENROUTER_BASE, session=server.session)

    @pytest.fixture
    def analyzer(self, server: OpenRouterServer) -> MediaAnalyzer:
        server.script(CHAT, load("openrouter", "chat_video_ok"))
        return self._adapter(server)

    @pytest.fixture
    def out_of_quota(self, server: OpenRouterServer) -> MediaAnalyzer:
        server.script(CHAT, load("openrouter", "error_402_credits"))
        return self._adapter(server)

    @pytest.fixture
    def too_slow(self, server: OpenRouterServer) -> MediaAnalyzer:
        async def hang() -> Recording:
            return await slow(30, load("openrouter", "chat_video_ok"))

        server.script(CHAT, hang)
        return self._adapter(server)


# --- the adapters, live (`pytest -m live`; skipped without the key) ------------------------------------------------
@pytest.mark.live
class TestGeminiMediaAnalyzerLive(MediaAnalyzerContract):
    @pytest.fixture
    async def analyzer(self) -> AsyncIterator[MediaAnalyzer]:
        adapter = GeminiMediaAnalyzer(api_key=live_key("YORDAMCHI_LIVE_GOOGLE_API_KEY"))
        yield adapter
        await adapter.aclose()

    @pytest.fixture
    def model(self) -> str:
        return "gemini-3.8-flash"

    @pytest.fixture
    def url(self) -> str:
        return LIVE_VIDEO


@pytest.mark.live
class TestOpenRouterMediaAnalyzerLive(MediaAnalyzerContract):
    @pytest.fixture
    async def analyzer(self) -> AsyncIterator[MediaAnalyzer]:
        adapter = OpenRouterMediaAnalyzer(api_key=live_key("YORDAMCHI_LIVE_OPENROUTER_API_KEY"))
        yield adapter
        await adapter.aclose()

    @pytest.fixture
    def model(self) -> str:
        return "google/gemini-3.8-flash"

    @pytest.fixture
    def url(self) -> str:
        return LIVE_VIDEO
