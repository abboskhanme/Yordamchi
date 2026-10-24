from pathlib import Path

import pytest

from tests.fakes.web import FakeWebTools
from yordamchi.ports.web import WebTools


class WebToolsContract:
    @pytest.fixture
    def web(self) -> WebTools:
        raise NotImplementedError

    async def test_screenshot_writes_both_images_into_dest_dir(self, web: WebTools, tmp_path: Path) -> None:
        shot = await web.screenshot("https://example.com", dest_dir=tmp_path, width=1280, height=800, timeout=30.0)
        for path in (shot.viewport_png, shot.full_page_png):
            assert path.parent == tmp_path
            assert path.stat().st_size > 0

    async def test_fetch_file_stays_inside_dest_dir(self, web: WebTools, tmp_path: Path) -> None:
        fetched = await web.fetch_file(
            "https://example.com/a.pdf", dest_dir=tmp_path, max_bytes=50_000_000, timeout=30.0
        )
        assert fetched.path.parent == tmp_path
        assert 0 < fetched.size_bytes <= 50_000_000


class TestFakeWebTools(WebToolsContract):
    @pytest.fixture
    def web(self) -> WebTools:
        return FakeWebTools()
