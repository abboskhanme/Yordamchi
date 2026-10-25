"""The `Transcriber` contract: the fake and both adapters (offline, on synthetic payloads) behave the same, and the
real adapters pass it live with a key (`pytest -m live`)."""

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from tests.fakes.clock import FakeClock
from tests.fakes.speech import FakeTranscriber
from tests.integration.stt_harness import (
    GOOGLE_KEY,
    OPENROUTER_BASE,
    OPENROUTER_KEY,
    GoogleServer,
    LiveServer,
    OpenRouterServer,
    Recording,
    fake_ffmpeg,
    google_server,
    live_done,
    live_final,
    live_key,
    load,
    make_wav,
    openrouter_server,
    slow,
)
from yordamchi.adapters.gemini.stt import GeminiTranscriber
from yordamchi.adapters.openrouter.stt import OpenRouterTranscriber
from yordamchi.core import retry
from yordamchi.core.errors import AuthFailed, CallTimedOut, ExternalError, QuotaExhausted
from yordamchi.ports.speech import Transcriber

LANGUAGES = ("uz-UZ", "ru-RU", "en-US")
MODE = "smart"
FALLBACK_MODEL = "google/gemini-3.5-transcribe"
CHAT = "/api/v1/chat/completions"


class TranscriberContract:
    @pytest.fixture
    def transcriber(self) -> Transcriber:
        raise NotImplementedError

    @pytest.fixture
    def audio(self) -> tuple[bytes, str]:
        return b"OggS...", "audio/ogg"

    @pytest.fixture
    def model(self) -> str:
        return "m-1"

    async def test_returns_text_with_its_provenance(
        self, transcriber: Transcriber, audio: tuple[bytes, str], model: str
    ) -> None:
        data, mime = audio
        transcript = await transcriber.transcribe(
            data,
            mime=mime,
            languages=LANGUAGES,
            vocabulary=("BonviHR",),
            model=model,
            mode=MODE,
            fallback_model=FALLBACK_MODEL,
            timeout=8.0,
        )
        assert isinstance(transcript.text, str)
        assert transcript.provider
        assert transcript.provider_path
        assert transcript.model == model
        assert transcript.audio_ms >= 0
        assert transcript.latency_ms >= 0

    async def test_works_without_hints(self, transcriber: Transcriber, audio: tuple[bytes, str], model: str) -> None:
        data, mime = audio
        transcript = await transcriber.transcribe(
            data,
            mime=mime,
            languages=(),
            vocabulary=(),
            model=model,
            mode=MODE,
            fallback_model=FALLBACK_MODEL,
            timeout=8.0,
        )
        assert isinstance(transcript.text, str)


class TranscriberFailureContract:
    """Offline only: every implementation surfaces quota and timeouts as the same `core.errors` classes."""

    @pytest.fixture
    def out_of_quota(self) -> Transcriber:
        raise NotImplementedError

    @pytest.fixture
    def too_slow(self) -> Transcriber:
        raise NotImplementedError

    async def test_quota_exhaustion_is_quota_exhausted_and_not_retryable(self, out_of_quota: Transcriber) -> None:
        with pytest.raises(QuotaExhausted) as caught:
            await out_of_quota.transcribe(
                b"OggS...",
                mime="audio/ogg",
                languages=LANGUAGES,
                vocabulary=(),
                model="m-1",
                mode=MODE,
                fallback_model=FALLBACK_MODEL,
                timeout=8.0,
            )
        assert isinstance(caught.value, ExternalError)
        assert caught.value.retryable is False

    async def test_a_call_longer_than_its_timeout_is_call_timed_out(self, too_slow: Transcriber) -> None:
        with pytest.raises(CallTimedOut):
            await too_slow.transcribe(
                b"OggS...",
                mime="audio/ogg",
                languages=LANGUAGES,
                vocabulary=(),
                model="m-1",
                mode=MODE,
                fallback_model=FALLBACK_MODEL,
                timeout=0.3,
            )


class TranscriberKeyContract:
    """Offline only: `validate_key` is on the port, so `shared/voice` can check a new key through it (SPEC §3.2.11)."""

    @pytest.fixture
    def checker(self) -> tuple[Transcriber, str, str]:
        """The implementation, a key it accepts and a model that key can use."""
        raise NotImplementedError

    async def test_a_usable_model_is_listed_as_available(self, checker: tuple[Transcriber, str, str]) -> None:
        transcriber, key, model = checker
        check = await transcriber.validate_key(key, models=(model,), timeout=10.0)
        assert check.provider
        assert check.available_models == (model,)
        assert check.missing_models == ()
        assert check.ok


# --- the fake ------------------------------------------------------------------------------------------------------
class TestFakeTranscriber(TranscriberContract, TranscriberFailureContract, TranscriberKeyContract):
    @pytest.fixture
    def transcriber(self) -> Transcriber:
        return FakeTranscriber()

    @pytest.fixture
    def checker(self) -> tuple[Transcriber, str, str]:
        return FakeTranscriber(), GOOGLE_KEY, "gemini-3.5-transcribe"

    @pytest.fixture
    def out_of_quota(self) -> Transcriber:
        fake = FakeTranscriber()
        fake.script(QuotaExhausted(provider="google"))
        return fake

    @pytest.fixture
    def too_slow(self) -> Transcriber:
        fake = FakeTranscriber()  # no clock: a latency over the timeout raises at once, as a real timeout would
        fake.script("kech", latency_s=9.0)
        return fake

    async def test_records_hints_and_vocabulary(self) -> None:
        transcriber = FakeTranscriber()
        await transcriber.transcribe(
            b"abc",
            mime="audio/ogg",
            languages=LANGUAGES,
            vocabulary=("HRMentor",),
            model="m",
            mode=MODE,
            fallback_model=FALLBACK_MODEL,
            timeout=8.0,
        )
        assert transcriber.calls[0].languages == LANGUAGES
        assert transcriber.calls[0].vocabulary == ("HRMentor",)
        assert (transcriber.calls[0].mode, transcriber.calls[0].fallback_model) == (MODE, FALLBACK_MODEL)

    async def test_scripted_key_checks(self) -> None:
        transcriber = FakeTranscriber(provider="openrouter")
        transcriber.script_key_check(AuthFailed(provider="openrouter"))
        with pytest.raises(AuthFailed):
            await transcriber.validate_key(OPENROUTER_KEY, models=("m",), timeout=10.0)
        [call] = transcriber.key_calls
        assert (call.models, call.timeout) == (("m",), 10.0)
        assert OPENROUTER_KEY not in repr(call)

    async def test_scripted_errors_and_timeouts(self) -> None:
        transcriber = FakeTranscriber(clock=FakeClock())
        transcriber.script(QuotaExhausted(provider="google"))
        with pytest.raises(QuotaExhausted):
            await transcriber.transcribe(
                b"a",
                mime="audio/ogg",
                languages=(),
                vocabulary=(),
                model="m",
                mode=MODE,
                fallback_model=FALLBACK_MODEL,
                timeout=8.0,
            )
        transcriber.script("kech", latency_s=9.0)
        with pytest.raises(CallTimedOut):
            await transcriber.transcribe(
                b"a",
                mime="audio/ogg",
                languages=(),
                vocabulary=(),
                model="m",
                mode=MODE,
                fallback_model=FALLBACK_MODEL,
                timeout=0.0,
            )


# --- the adapters, offline -----------------------------------------------------------------------------------------
@pytest.fixture
def _instant_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(retry, "backoff_delay", lambda attempt, *, base_s, max_s: 0.0)


@pytest.mark.usefixtures("_instant_backoff")
class TestGeminiTranscriber(TranscriberContract, TranscriberFailureContract, TranscriberKeyContract):
    @pytest.fixture
    async def google(self) -> AsyncIterator[GoogleServer]:
        async with google_server() as server:
            yield server

    @pytest.fixture
    def checker(self, google: GoogleServer) -> tuple[Transcriber, str, str]:
        google.script(load("gemini", "model_get_ok"))
        adapter = GeminiTranscriber(api_key=GOOGLE_KEY, client_factory=google.client_factory)
        return adapter, GOOGLE_KEY, "gemini-3.5-transcribe"

    @pytest.fixture
    def transcriber(self, google: GoogleServer) -> Transcriber:
        google.script(load("gemini", "interaction_transcribe_ok"))
        return GeminiTranscriber(api_key=GOOGLE_KEY, client_factory=google.client_factory)

    @pytest.fixture
    def out_of_quota(self, google: GoogleServer) -> Transcriber:
        google.script(load("gemini", "error_429_quota_per_day"))
        return GeminiTranscriber(api_key=GOOGLE_KEY, client_factory=google.client_factory)

    @pytest.fixture
    def too_slow(self, google: GoogleServer) -> Transcriber:
        async def hang(request: httpx.Request) -> Recording:
            del request
            return await slow(30, load("gemini", "interaction_transcribe_ok"))

        google.script(hang)
        return GeminiTranscriber(api_key=GOOGLE_KEY, client_factory=google.client_factory)


class TestGeminiLiveTranscriber(TranscriberContract):
    """The same adapter on a Live model (`gemini-3.5-transcribe-live`, the default since 2026-10-25)."""

    @pytest.fixture
    def transcriber(self, tmp_path: Path) -> Transcriber:
        server = LiveServer()
        server.script([live_final("salom"), live_done()])
        ffmpeg = fake_ffmpeg(tmp_path / "bin", output=bytes(32_000))
        return GeminiTranscriber(api_key=GOOGLE_KEY, client_factory=server.client_factory, ffmpeg=ffmpeg)

    @pytest.fixture
    def model(self) -> str:
        return "gemini-3.5-transcribe-live"


@pytest.mark.usefixtures("_instant_backoff")
class TestOpenRouterTranscriber(TranscriberContract, TranscriberFailureContract, TranscriberKeyContract):
    @pytest.fixture
    async def server(self) -> AsyncIterator[OpenRouterServer]:
        async with openrouter_server() as running:
            yield running

    @pytest.fixture
    def checker(self, server: OpenRouterServer, ffmpeg: Path) -> tuple[Transcriber, str, str]:
        server.script("/api/v1/key", load("openrouter", "key_ok"))
        server.script("/api/v1/models/google/gemini-3.8-flash/endpoints", load("openrouter", "endpoints_ok"))
        return self._adapter(server, ffmpeg), OPENROUTER_KEY, "google/gemini-3.8-flash"

    @pytest.fixture
    def ffmpeg(self, tmp_path: Path) -> Path:
        return fake_ffmpeg(tmp_path / "bin", output=make_wav(1000))

    def _adapter(self, server: OpenRouterServer, ffmpeg: Path) -> OpenRouterTranscriber:
        return OpenRouterTranscriber(
            api_key=OPENROUTER_KEY, ffmpeg=ffmpeg, base_url=OPENROUTER_BASE, session=server.session
        )

    @pytest.fixture
    def transcriber(self, server: OpenRouterServer, ffmpeg: Path) -> Transcriber:
        server.script(CHAT, load("openrouter", "chat_ok"))
        return self._adapter(server, ffmpeg)

    @pytest.fixture
    def out_of_quota(self, server: OpenRouterServer, ffmpeg: Path) -> Transcriber:
        server.script(CHAT, load("openrouter", "error_402_credits"))
        return self._adapter(server, ffmpeg)

    @pytest.fixture
    def too_slow(self, server: OpenRouterServer, ffmpeg: Path) -> Transcriber:
        async def hang() -> Recording:
            return await slow(30, load("openrouter", "chat_ok"))

        server.script(CHAT, hang)
        return self._adapter(server, ffmpeg)


# --- the adapters, live (`pytest -m live`; skipped without the key) ------------------------------------------------
@pytest.mark.live
class TestGeminiTranscriberLive(TranscriberContract):
    @pytest.fixture
    async def transcriber(self) -> AsyncIterator[Transcriber]:
        adapter = GeminiTranscriber(api_key=live_key("YORDAMCHI_LIVE_GOOGLE_API_KEY"))
        yield adapter
        await adapter.aclose()

    @pytest.fixture
    def audio(self) -> tuple[bytes, str]:
        return make_wav(1000), "audio/wav"

    @pytest.fixture
    def model(self) -> str:
        return "gemini-3.5-transcribe"


@pytest.mark.live
class TestOpenRouterTranscriberLive(TranscriberContract):
    @pytest.fixture
    async def transcriber(self) -> AsyncIterator[Transcriber]:
        adapter = OpenRouterTranscriber(api_key=live_key("YORDAMCHI_LIVE_OPENROUTER_API_KEY"), ffmpeg=None)
        yield adapter
        await adapter.aclose()

    @pytest.fixture
    def audio(self) -> tuple[bytes, str]:
        return make_wav(1000), "audio/wav"

    @pytest.fixture
    def model(self) -> str:
        return "google/gemini-3.8-flash"
