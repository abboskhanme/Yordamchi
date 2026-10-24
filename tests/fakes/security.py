"""`FakeSecurity`: an in-memory `/usr/bin/security`, so no test ever touches the real login Keychain.

It answers the command shapes `core.keychain` builds, with the real tool's exit codes:
- `find-generic-password -s S -a A [-w]`: 0 (and the value plus a newline with `-w`), or 44 when the item is missing;
- `delete-generic-password -s S -a A`: 0 or 44;
- `-i` with `add-generic-password -U -s "S" -a "A" -T /usr/bin/security -w "V"` lines on stdin: creates or updates.
`locked` answers every call with 51 (a locked Keychain refusing interaction); `hang` raises `TimeoutError`, as
`core.proc.run_sync` does when its timeout expires. `calls` and `stdins` record what was asked, so a test can prove a
value never reached argv.
"""

import shlex
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Final

from yordamchi.core import keychain
from yordamchi.core.proc import ProcResult

LOCKED_EXIT: Final = 51
_VALUE_FLAGS: Final = frozenset({"-s", "-a", "-T", "-w", "-l", "-j"})


def _result(argv: Sequence[str], returncode: int, stdout: bytes = b"") -> ProcResult:
    return ProcResult(argv=tuple(argv), returncode=returncode, stdout=stdout, stderr=b"", duration_ms=1)


def _options(args: Sequence[str], *, value_flags: frozenset[str]) -> dict[str, str]:
    options: dict[str, str] = {}
    index = 0
    while index < len(args):
        flag = args[index]
        if flag in value_flags and index + 1 < len(args):
            options[flag] = args[index + 1]
            index += 2
        else:
            options[flag] = ""
            index += 1
    return options


@dataclass
class FakeSecurity:
    items: dict[tuple[str, str], str] = field(default_factory=dict)
    trusted: dict[tuple[str, str], str] = field(default_factory=dict)  # the `-T` application of each item
    locked: bool = False
    hang: bool = False
    fail_writes: bool = False
    calls: list[tuple[str, ...]] = field(default_factory=list)
    stdins: list[bytes] = field(default_factory=list)

    def put(self, item: keychain.KeychainItem, value: str) -> None:
        self.items[(item.service, item.account)] = value
        self.trusted[(item.service, item.account)] = keychain.SECURITY

    def get(self, item: keychain.KeychainItem) -> str | None:
        return self.items.get((item.service, item.account))

    def values(self) -> list[str]:
        return list(self.items.values())

    def __call__(self, argv: Sequence[str], *, stdin: bytes | None, timeout: float) -> ProcResult:
        assert timeout > 0
        assert argv[0] == keychain.SECURITY, argv
        self.calls.append(tuple(argv))
        if stdin is not None:
            self.stdins.append(stdin)
        if self.hang:
            raise TimeoutError(argv[0])
        if self.locked:
            return _result(argv, LOCKED_EXIT)
        if argv[1] == "-i":
            return self._interactive(argv, stdin or b"")
        return self._item_command(argv)

    def _item_command(self, argv: Sequence[str]) -> ProcResult:
        verb = argv[1]
        options = _options(argv[2:], value_flags=frozenset({"-s", "-a"}))
        key = (options["-s"], options["-a"])
        if verb not in {"find-generic-password", "delete-generic-password"}:
            return _result(argv, 1)
        if key not in self.items:
            return _result(argv, keychain.ITEM_NOT_FOUND_EXIT)
        if verb == "delete-generic-password":
            del self.items[key]
            return _result(argv, 0)
        return _result(argv, 0, (self.items[key] + "\n").encode() if "-w" in options else b"attributes\n")

    def _interactive(self, argv: Sequence[str], stdin: bytes) -> ProcResult:
        returncode = 0
        for line in stdin.decode("ascii").splitlines():
            words = shlex.split(line)
            if not words:
                continue
            if words[0] != "add-generic-password" or self.fail_writes:
                returncode = 1
                continue
            options = _options(words[1:], value_flags=_VALUE_FLAGS)
            key = (options["-s"], options["-a"])
            if key in self.items and "-U" not in options:
                returncode = 45  # errSecDuplicateItem
                continue
            self.items[key] = options["-w"]
            self.trusted[key] = options.get("-T", "")
        return _result(argv, returncode)


_: keychain.Runner = FakeSecurity()
