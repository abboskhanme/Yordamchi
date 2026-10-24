"""`FakeRemoteShell`: scripted results per (host, command); everything else succeeds with empty output."""

from yordamchi.ports.remote_shell import RemoteResult, RemoteShell


class FakeRemoteShell:
    def __init__(self) -> None:
        self.responses: dict[tuple[str, str], RemoteResult | Exception] = {}
        self.calls: list[tuple[str, str, float]] = []

    def respond(self, host: str, command: str, result: RemoteResult | Exception) -> None:
        self.responses[(host, command)] = result

    async def run(self, host: str, command: str, *, timeout: float) -> RemoteResult:
        self.calls.append((host, command, timeout))
        response = self.responses.get((host, command))
        if isinstance(response, Exception):
            raise response
        return response or RemoteResult(host=host, exit_code=0, stdout="", stderr="", duration_ms=1)


_: RemoteShell = FakeRemoteShell()
