"""The `WebTools` contract, against `FakeWebTools` and (opt-in, `-m live`) the real `LocalWebTools`.

Live needs the network, `uv run playwright install chromium`, and for the video cases the static ffmpeg in
`DATA/bin` (`scripts/install.py`); a missing piece skips its case."""

import stat
from pathlib import Path

import pytest

from tests.factories import make_video_limits
from tests.fakes.web import FakeWebTools
from yordamchi.core import proc
from yordamchi.core.paths import Paths
from yordamchi.personal.artefacts import domain
from yordamchi.ports.web import WebTools

MIB = 1024 * 1024
LIMITS = make_video_limits()
# "Me at the zoo" (19 s), YouTube's first upload: small, public and not going anywhere.
TEST_VIDEO_URL = "https://www.youtube.com/watch?v=jNQXAC9IVRw"


def _private(path: Path) -> bool:
    return stat.S_IMODE(path.stat().st_mode) == 0o600


class WebToolsContract:
    file_url = "https://example.com/a.pdf"
    video_url = TEST_VIDEO_URL
    page_url = "https://en.wikipedia.org/wiki/Tashkent"

    @pytest.fixture
    def web(self) -> WebTools:
        raise NotImplementedError

    async def test_screenshot_writes_both_images_into_dest_dir(self, web: WebTools, tmp_path: Path) -> None:
        shot = await web.screenshot("https://example.com", dest_dir=tmp_path, width=1280, height=800, timeout=30.0)
        for path in (shot.viewport_png, shot.full_page_png):
            assert path.parent == tmp_path
            assert path.stat().st_size > 0
            assert _private(path)

    async def test_fetch_file_stays_inside_dest_dir(self, web: WebTools, tmp_path: Path) -> None:
        fetched = await web.fetch_file(self.file_url, dest_dir=tmp_path, max_bytes=50 * MIB, timeout=30.0)
        assert fetched.path.parent == tmp_path
        assert 0 < fetched.size_bytes <= 50 * MIB
        assert _private(fetched.path)

    async def test_download_video_fits_the_download_cap(self, web: WebTools, tmp_path: Path) -> None:
        video = await web.download_video(
            self.video_url,
            dest_dir=tmp_path,
            max_bytes=LIMITS.download_max_bytes,
            format_selector=domain.format_selector(LIMITS),
            max_duration_s=domain.max_fit_duration_s(LIMITS),
            clip=None,
            timeout=180.0,
        )
        assert video.path.parent == tmp_path
        assert 0 < video.size_bytes <= LIMITS.download_max_bytes
        assert video.mime.startswith("video/")
        assert _private(video.path)

    async def test_reencode_writes_an_mp4_next_to_the_source(self, web: WebTools, tmp_path: Path) -> None:
        source = await web.download_video(
            self.video_url,
            dest_dir=tmp_path,
            max_bytes=LIMITS.download_max_bytes,
            format_selector=domain.format_selector(LIMITS),
            max_duration_s=None,
            clip=None,
            timeout=180.0,
        )
        out = await web.reencode_video(
            source.path, dest_dir=tmp_path, video_kbps=300, audio_kbps=64, max_height=240, timeout=180.0
        )
        assert out.path.parent == tmp_path
        assert out.path != source.path
        assert out.mime == "video/mp4"
        assert out.size_bytes > 0

    async def test_read_page_returns_text_within_the_cap(self, web: WebTools) -> None:
        page = await web.read_page(self.page_url, max_chars=2000, max_bytes=2 * MIB, timeout=30.0)
        assert page.text
        assert len(page.text) <= 2000
        assert page.url.startswith("https://")
        assert 0 < page.size_bytes <= 2 * MIB


class TestFakeWebTools(WebToolsContract):
    @pytest.fixture
    def web(self) -> WebTools:
        return FakeWebTools()


@pytest.mark.live
class TestLocalWebTools(WebToolsContract):
    file_url = "https://www.python.org/static/img/python-logo.png"

    @pytest.fixture
    def web(self, request: pytest.FixtureRequest) -> WebTools:
        from yordamchi.adapters.web.tools import LocalWebTools

        paths = Paths.defaults(Path.home())
        if ("video" in request.node.name or "reencode" in request.node.name) and not (paths.bin / "ffmpeg").exists():
            pytest.skip("the static ffmpeg is not installed in DATA/bin (scripts/install.py)")
        child_path = proc.build_search_path(paths, None)
        return LocalWebTools.from_paths(paths, child_path=child_path)
