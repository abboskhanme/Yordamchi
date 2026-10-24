"""`SharedDeps`: what every `shared/*` service receives (CONVENTIONS §4). Built only in `bots/<bot>/app.py`.

A frozen dataclass of ports, `db` and `clock` — no DI framework. `DevDeps` and `PersonalDeps` extend it; a shared
service takes `deps: SharedDeps` and accepts either.
"""

from collections.abc import Mapping
from dataclasses import dataclass

from yordamchi.core.paths import BotName, Paths
from yordamchi.persistence.uow import Database
from yordamchi.ports.assistant import Assistant
from yordamchi.ports.clock import Clock
from yordamchi.ports.keystore import Keystore
from yordamchi.ports.mac_host import MacHost
from yordamchi.ports.mcp import McpClient
from yordamchi.ports.messenger import Messenger
from yordamchi.ports.speech import Synthesizer, Transcriber
from yordamchi.shared.settings.registry import SettingValue


@dataclass(frozen=True, slots=True, kw_only=True)
class SharedDeps:
    bot: BotName
    db: Database  # this bot's own DB file, read-write
    # The other bot's DB file, read-only (`mode=ro`, `query_only`): the personal bot reads shared settings and the
    # P3 bridge tables of dev.db; the dev bot reads numbers and secret names of personal.db (SPEC §2.3).
    peer_db: Database | None
    clock: Clock
    paths: Paths
    bootstrap: Mapping[str, SettingValue]  # FROM_BOOTSTRAP defaults from the env file
    messenger: Messenger
    keystore: Keystore
    assistant: Assistant
    transcribers: Mapping[str, Transcriber]  # provider name → adapter ("google", "openrouter")
    synthesizers: Mapping[str, Synthesizer]  # provider name → adapter ("google", "openrouter", "edge")
    mac_host: MacHost
    mcp: McpClient
