"""The MCP `headersHelper` (SPEC §3.2.10 step 6; STACK §2.1, §4.2): `bin/yordamchi-helper headers` runs
`"$YORDAMCHI_PY" -I -m yordamchi.bots.cli.helper headers`.

Claude Code runs it in a shell at every connect and reconnect of a remote MCP server (and once more after a 401/403),
with `CLAUDE_CODE_MCP_SERVER_NAME` set; it gives up after 10 s. The helper asks the running dev bot over
`YORDAMCHI_SOCK` (method `headers`, with `YORDAMCHI_SESSION_TOKEN`) and prints the answer, a JSON object of headers,
on stdout. So the token lives only in the bot's vault and in this pipe: never on the `claude` command line.

It fails closed and quietly: no socket, no token, no server name or no usable answer → exit 1, nothing on stdout,
one line on stderr without any value. Imports only `yordamchi.core` and the standard library (A9).
"""

import json
import sys
from collections.abc import Mapping, Sequence
from typing import Final

from yordamchi.core import control
from yordamchi.core.config import load_cli_config, load_mcp_server_name

EXIT_FAILED: Final = 1
ANSWER_TIMEOUT_S: Final = 8.0  # inside Claude Code's 10 s limit for a headersHelper


def fetch_headers(environ: Mapping[str, str] | None = None) -> dict[str, str] | str:
    """The headers to print, or the snake_case reason there are none."""
    config = load_cli_config(environ)
    server = load_mcp_server_name(environ)
    if config.socket is None or not config.session_token:
        return "socket_unknown"
    if not server:
        return "server_unknown"
    request = control.HeadersRequest(server_name=server, session_token=config.session_token)
    answer = control.request(config.socket, control.headers_request(request), timeout=ANSWER_TIMEOUT_S)
    headers = control.read_headers_answer(answer)
    return headers if headers is not None else "no_headers"


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args != [control.HEADERS]:
        sys.stderr.write("yordamchi-helper: usage: yordamchi-helper headers\n")
        return EXIT_FAILED
    outcome = fetch_headers()
    if isinstance(outcome, str):
        sys.stderr.write(f"yordamchi-helper: no headers ({outcome})\n")
        return EXIT_FAILED
    sys.stdout.write(json.dumps(outcome, separators=(",", ":")))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
