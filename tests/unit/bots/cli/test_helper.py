"""bots.cli.helper, in process: every reason for no headers before the socket, and the usage check. The helper runs
as a real subprocess against a socket in tests/integration/test_helper_cli.py."""

from pathlib import Path

import pytest

from yordamchi.bots.cli import helper


def test_without_a_socket_or_a_token_there_are_no_headers() -> None:
    assert helper.fetch_headers({"CLAUDE_CODE_MCP_SERVER_NAME": "bonvi_hr"}) == "socket_unknown"
    assert helper.fetch_headers({"YORDAMCHI_SOCK": "/tmp/x.sock", "CLAUDE_CODE_MCP_SERVER_NAME": "a"}) == (
        "socket_unknown"
    )


def test_without_a_server_name_there_are_no_headers() -> None:
    environ = {"YORDAMCHI_SOCK": "/tmp/x.sock", "YORDAMCHI_SESSION_TOKEN": "t"}
    assert helper.fetch_headers(environ) == "server_unknown"


def test_an_absent_socket_is_no_headers(tmp_path: Path) -> None:
    environ = {
        "YORDAMCHI_SOCK": str(tmp_path / "none.sock"),
        "YORDAMCHI_SESSION_TOKEN": "t",
        "CLAUDE_CODE_MCP_SERVER_NAME": "bonvi_hr",
    }
    assert helper.fetch_headers(environ) == "no_headers"


@pytest.mark.parametrize("argv", [[], ["token"], ["headers", "extra"]])
def test_only_the_headers_command_is_accepted(argv: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    assert helper.main(argv) == helper.EXIT_FAILED
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "usage" in captured.err


def test_the_answer_bound_stays_inside_claude_codes_ten_seconds() -> None:
    assert helper.ANSWER_TIMEOUT_S < 10
