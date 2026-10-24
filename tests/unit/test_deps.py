"""The deps dataclasses (CONVENTIONS §4): the personal bot holds no executor, git, GitHub or SSH handle (UC-36)."""

import dataclasses
import typing

from yordamchi.dev.deps import DevDeps
from yordamchi.personal.deps import PersonalDeps
from yordamchi.ports.agent import AgentRuntime
from yordamchi.ports.git import Git
from yordamchi.ports.github import GitHub
from yordamchi.ports.remote_shell import RemoteShell
from yordamchi.shared.deps import SharedDeps

DEV_ONLY = {AgentRuntime, Git, GitHub, RemoteShell}


def _field_types(cls: type) -> set[object]:
    hints = typing.get_type_hints(cls)
    return {hints[field.name] for field in dataclasses.fields(cls)}


def test_the_personal_bot_has_no_dev_only_port() -> None:
    assert _field_types(PersonalDeps).isdisjoint(DEV_ONLY)
    assert _field_types(SharedDeps).isdisjoint(DEV_ONLY)


def test_the_dev_bot_has_them() -> None:
    assert _field_types(DevDeps) >= DEV_ONLY


def test_deps_are_frozen() -> None:
    for cls in (SharedDeps, DevDeps, PersonalDeps):
        assert vars(cls)["__dataclass_params__"].frozen
