"""The ssh/scp/sftp/rsync gate shim (SPEC §5.7 Layer 2): `shims/<tool>` runs
`"$YORDAMCHI_PY" -I -m yordamchi.bots.cli.shim <shims dir> <tool> "$@"`.

It asks the running dev bot (`gate_check` over `YORDAMCHI_SOCK`, with `YORDAMCHI_SESSION_TOKEN`) whether this exact
invocation may run, then execs the real binary (the first one on `PATH` outside the shims directory) or refuses with
exit 126. It fails closed: no socket, no token, no answer or any other answer than a yes is a refusal. The bot
allows a read-only command, a git host, or an invocation a grant consumed moments ago in this session covers.

Imports only `yordamchi.core` and the standard library (A9): it starts on every ssh call a script makes.
"""

import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from yordamchi.core import control
from yordamchi.core.config import load_cli_config

EXIT_REFUSED: Final = 126  # what a shell returns for "found but cannot run"
TOOLS: Final = frozenset({"ssh", "scp", "sftp", "rsync"})  # `gate.domain.SHIM_TOOLS`
ANSWER_TIMEOUT_S: Final = 30.0  # the bot's check is bounded by `gate.hook_timeout_s` (≤ 60 s); this is the backstop


@dataclass(frozen=True, slots=True)
class Run:
    """Exec this binary with this argv."""

    binary: str
    argv: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Refuse:
    reason: str


def real_binary(tool: str, search_path: str, shim_dir: Path) -> str | None:
    """The first executable `tool` on `search_path` that is not a shim (never ourselves, whatever the PATH order)."""
    shims = shim_dir.resolve()
    for entry in search_path.split(os.pathsep):
        if not entry:
            continue
        candidate = Path(entry) / tool
        try:
            resolved = candidate.resolve(strict=True)
            folder = Path(entry).resolve()
        except OSError:
            continue
        if shims in (folder, resolved.parent):
            continue
        if resolved.is_file() and os.access(resolved, os.X_OK):
            return str(candidate)
    return None


def decide(args: Sequence[str], *, environ: Mapping[str, str] | None = None, cwd: str | None = None) -> Run | Refuse:
    """`args` = the shims directory, the tool, then the tool's own arguments."""
    if len(args) < 2 or args[1] not in TOOLS:
        return Refuse("usage")
    shim_dir, tool, rest = Path(args[0]), args[1], tuple(args[2:])
    if not shim_dir.is_dir():
        return Refuse("shim_dir_unknown")
    config = load_cli_config(environ)
    if config.socket is None or not config.session_token:
        return Refuse("gate_unreachable")
    check = control.GateCheck(tool, rest, cwd or str(Path.cwd()), config.session_token)
    answer = control.read_gate_answer(
        control.request(config.socket, control.gate_check_request(check), timeout=ANSWER_TIMEOUT_S)
    )
    if not answer.allow:
        return Refuse(answer.reason)
    binary = real_binary(tool, config.search_path, shim_dir)
    if binary is None:
        return Refuse("not_installed")
    return Run(binary, (tool, *rest))


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    outcome = decide(args)
    if isinstance(outcome, Run):
        sys.stdout.flush()
        os.execv(outcome.binary, list(outcome.argv))  # noqa: S606  # the real binary, argv as the caller gave it
    tool = args[1] if len(args) > 1 else "shim"
    sys.stderr.write(
        f"yordamchi: {tool} refused by the gate ({outcome.reason}). A server change needs the owner's approval of "
        "this exact command first; do not retry it another way.\n"
    )
    return EXIT_REFUSED


if __name__ == "__main__":
    raise SystemExit(main())
