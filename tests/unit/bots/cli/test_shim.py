"""bots.cli.shim, in process: finding the real binary past the shims, and every refusal before the socket. The real
shims run as subprocesses against a socket in tests/integration/test_gate_cli.py."""

from pathlib import Path

from yordamchi.bots.cli import shim
from yordamchi.bots.cli.shim import Refuse

REPO_SHIMS = Path(__file__).resolve().parents[4] / "shims"


def _executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def test_the_real_binary_is_the_first_one_outside_the_shims(tmp_path: Path) -> None:
    shims = tmp_path / "release" / "shims"
    _executable(shims / "ssh")
    real = _executable(tmp_path / "bin" / "ssh")
    linked = tmp_path / "links"
    linked.mkdir()
    (linked / "ssh").symlink_to(shims / "ssh")  # a PATH entry that only links back to a shim

    search = f"{shims}:{linked}:{tmp_path / 'missing'}::{tmp_path / 'bin'}"
    assert shim.real_binary("ssh", search, shims) == str(real)
    assert shim.real_binary("ssh", str(shims), shims) is None
    (tmp_path / "bin" / "scp").write_text("not executable", encoding="utf-8")
    assert shim.real_binary("scp", search, shims) is None


def test_every_shim_script_hands_its_own_tool_to_the_module() -> None:
    for tool in sorted(shim.TOOLS):
        script = (REPO_SHIMS / tool).read_text(encoding="utf-8")
        assert script.startswith("#!/bin/sh\n")
        assert f'-I -m yordamchi.bots.cli.shim "${{0%/*}}" {tool} "$@"' in script
        assert (REPO_SHIMS / tool).stat().st_mode & 0o111


def test_refusals_before_asking_the_bot(tmp_path: Path) -> None:
    environ = {"YORDAMCHI_SOCK": str(tmp_path / "dev.sock"), "YORDAMCHI_SESSION_TOKEN": "tok", "PATH": "/usr/bin"}
    assert shim.decide([], environ=environ) == Refuse("usage")
    assert shim.decide([str(tmp_path), "curl", "x"], environ=environ) == Refuse("usage")
    assert shim.decide([str(tmp_path / "nope"), "ssh", "prod"], environ=environ) == Refuse("shim_dir_unknown")
    assert shim.decide([str(tmp_path), "ssh", "prod"], environ={"PATH": "/usr/bin"}) == Refuse("gate_unreachable")
    no_token = {**environ, "YORDAMCHI_SESSION_TOKEN": ""}
    assert shim.decide([str(tmp_path), "ssh", "prod"], environ=no_token) == Refuse("gate_unreachable")
    assert shim.decide([str(tmp_path), "ssh", "prod"], environ=environ) == Refuse("no_answer")  # no socket there
