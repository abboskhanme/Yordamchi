from pathlib import Path

import pytest

from tests.fakes.media import FakeMediaAnalyzer
from yordamchi.core.errors import QuotaExhausted
from yordamchi.ports.media import MediaAnalyzer, VideoSource


class MediaAnalyzerContract:
    @pytest.fixture
    def analyzer(self) -> MediaAnalyzer:
        raise NotImplementedError

    async def test_analyze_returns_text_and_provider(self, analyzer: MediaAnalyzer) -> None:
        analysis = await analyzer.analyze_video(
            VideoSource(url="https://youtu.be/x"), prompt="nima haqida?", model="m", timeout=60.0
        )
        assert analysis.text
        assert analysis.provider


class TestFakeMediaAnalyzer(MediaAnalyzerContract):
    @pytest.fixture
    def analyzer(self) -> MediaAnalyzer:
        return FakeMediaAnalyzer()

    async def test_scripted_quota_error(self) -> None:
        analyzer = FakeMediaAnalyzer()
        analyzer.script(QuotaExhausted(provider="google"))
        with pytest.raises(QuotaExhausted):
            await analyzer.analyze_video(VideoSource(url="https://youtu.be/x"), prompt="p", model="m", timeout=60.0)


def test_a_video_source_is_a_path_or_a_url(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="either"):
        VideoSource()
    with pytest.raises(ValueError, match="either"):
        VideoSource(path=tmp_path / "v.mp4", url="https://youtu.be/x")
