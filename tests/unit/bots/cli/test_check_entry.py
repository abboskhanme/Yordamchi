"""`python -m yordamchi.check` is the same program as `python -m yordamchi.bots.cli.check` (SPEC §9.3 step 8; T28)."""

import runpy
from collections.abc import Sequence

import pytest

from yordamchi.bots.cli import check


def test_the_short_entry_point_runs_the_cli_check_and_keeps_its_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[Sequence[str] | None] = []

    def fake_main(argv: Sequence[str] | None = None) -> int:
        calls.append(argv)
        return 3

    monkeypatch.setattr(check, "main", fake_main)
    with pytest.raises(SystemExit) as exited:
        runpy.run_module("yordamchi.check", run_name="__main__")
    assert exited.value.code == 3
    assert calls == [None]
