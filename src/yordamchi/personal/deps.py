"""`PersonalDeps`: the personal bot's dependencies.

It has no executor, git, GitHub or SSH field, by construction (UC-36; C7 forbids the imports too). Its web content
can therefore never reach the dev executor.
"""

from collections.abc import Mapping
from dataclasses import dataclass

from yordamchi.ports.media import MediaAnalyzer
from yordamchi.ports.web import WebTools
from yordamchi.shared.deps import SharedDeps


@dataclass(frozen=True, slots=True, kw_only=True)
class PersonalDeps(SharedDeps):
    media_analyzers: Mapping[str, MediaAnalyzer]  # provider name → video reader ("google", "openrouter")
    web: WebTools
