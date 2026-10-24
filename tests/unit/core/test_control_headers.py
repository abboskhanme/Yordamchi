"""core.control, the `headers` method (SPEC §3.1.1; T50): the request line, the answer, and the bot's dispatcher.
Every malformed or unexpected answer is no headers at all (the helper then fails closed)."""

import json

import pytest

from yordamchi.core import control
from yordamchi.core.control import HeadersAnswer, HeadersRequest, ProtocolError
from yordamchi.core.errors import IntegrationNotFound


def test_the_request_names_the_server_and_carries_the_session_token() -> None:
    line = control.headers_request(HeadersRequest("bonvi_hr", "tok-1"))

    assert line.endswith(b"\n")
    assert json.loads(line) == {"method": "headers", "server_name": "bonvi_hr", "session_token": "tok-1"}
    assert control.method_of(line) == control.HEADERS
    assert control.parse_headers_request(line) == HeadersRequest("bonvi_hr", "tok-1")


def test_the_token_never_shows_in_a_repr() -> None:
    assert "tok-1" not in repr(HeadersRequest("bonvi_hr", "tok-1"))
    assert "secret" not in repr(HeadersAnswer(headers={"Authorization": "Bearer secret"}))


@pytest.mark.parametrize(
    "line",
    [
        b"not json\n",
        b'{"method": "gate_check", "server_name": "x", "session_token": "t"}\n',
        b'{"method": "headers", "session_token": "t"}\n',
        b'{"method": "headers", "server_name": "x"}\n',
        b'{"method": "headers", "server_name": 3, "session_token": "t"}\n',
        json.dumps({"method": "headers", "server_name": "x" * 65, "session_token": "t"}).encode(),
    ],
)
def test_a_malformed_request_is_a_protocol_error(line: bytes) -> None:
    with pytest.raises(ProtocolError):
        control.parse_headers_request(line)


def test_an_answer_is_the_headers_or_an_error_code() -> None:
    ok = control.headers_answer_line(HeadersAnswer(headers={"Authorization": "Bearer a"}))
    assert control.read_headers_answer(ok.rstrip(b"\n")) == {"Authorization": "Bearer a"}
    empty = control.headers_answer_line(HeadersAnswer())
    assert control.read_headers_answer(empty) == {}  # a server without auth
    refused = control.headers_answer_line(HeadersAnswer(error_code="session_unknown"))
    assert json.loads(refused) == {"error_code": "session_unknown"}
    assert control.read_headers_answer(refused) is None


@pytest.mark.parametrize(
    "line",
    [
        None,
        b"",
        b"garbage",
        b'{"headers": "Bearer a"}',
        b'{"headers": {"Authorization": 3}}',
        b'{"headers": {"Authorization": "Bearer a\\r\\nX-Evil: 1"}}',
        b'{"headers": {"Bad\\nName": "v"}}',
        b'{"headers": {"": "v"}}',
        json.dumps({"headers": {f"h{index}": "v" for index in range(17)}}).encode(),
        b"x" * (control.MAX_LINE_BYTES + 1),
    ],
)
def test_anything_else_is_no_headers(line: bytes | None) -> None:
    assert control.read_headers_answer(line) is None


async def test_dispatch_turns_failures_into_error_codes() -> None:
    seen: list[HeadersRequest] = []

    async def handler(request: HeadersRequest) -> HeadersAnswer:
        seen.append(request)
        if request.server_name == "missing":
            raise IntegrationNotFound(name="missing")
        return HeadersAnswer(headers={"Authorization": "Bearer a"})

    served = await control.dispatch_headers(control.headers_request(HeadersRequest("bonvi_hr", "t")), handler=handler)
    assert json.loads(served) == {"headers": {"Authorization": "Bearer a"}}
    missing = await control.dispatch_headers(control.headers_request(HeadersRequest("missing", "t")), handler=handler)
    assert json.loads(missing) == {"error_code": "integration_not_found"}
    assert json.loads(await control.dispatch_headers(b"{}\n", handler=handler)) == {"error_code": "bad_request"}
    assert [request.server_name for request in seen] == ["bonvi_hr", "missing"]
