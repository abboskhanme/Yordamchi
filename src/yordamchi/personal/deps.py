"""`PersonalDeps`: the personal bot's dependencies.

It has no executor, git, GitHub or SSH field, by construction (UC-36; C7 forbids the imports too). Its web content
can therefore never reach the dev executor. `peer_db` (from `SharedDeps`) is the read-only handle on dev.db that only
`shared/bridge` reads project status through.

`fallback_assistant` is the "zaxira model" (Q4): the Gemini (or OpenRouter) `Assistant` that answers plain questions
while Claude is unavailable. None when no speech key is configured; the owner is then told Claude's own error.
"""

from collections.abc import Mapping
from dataclasses import dataclass

from yordamchi.ports.assistant import Assistant
from yordamchi.ports.media import MediaAnalyzer
from yordamchi.ports.web import WebTools
from yordamchi.shared.deps import SharedDeps


@dataclass(frozen=True, slots=True, kw_only=True)
class PersonalDeps(SharedDeps):
    media_analyzers: Mapping[str, MediaAnalyzer]  # provider name → video reader ("google", "openrouter")
    web: WebTools
    fallback_assistant: Assistant | None = None
