"""`DevDeps`: the dev bot's dependencies — the shared ones plus the executor, git, GitHub and SSH."""

from dataclasses import dataclass

from yordamchi.ports.agent import AgentRuntime
from yordamchi.ports.git import Git
from yordamchi.ports.github import GitHub
from yordamchi.ports.remote_shell import RemoteShell
from yordamchi.shared.deps import SharedDeps


@dataclass(frozen=True, slots=True, kw_only=True)
class DevDeps(SharedDeps):
    agents: AgentRuntime
    git: Git
    github: GitHub
    remote_shell: RemoteShell
