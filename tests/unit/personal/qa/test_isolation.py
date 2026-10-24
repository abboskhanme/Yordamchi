"""UC-36 / NFR-SEC-09 for the personal brain (T70, T72): the assistant and the bridge hold no path to the dev bot.

C7 (import-linter) forbids `bots.personal` from reaching `dev`, the executor, git, GitHub or SSH, even indirectly. This
checks the same for the modules `bots.personal` will wire (`personal.*`, `shared.bridge.*`), on the real import graph,
so the guarantee holds before T77 adds the bot itself.
"""

import grimp
import pytest

FORBIDDEN = (
    "yordamchi.dev",
    "yordamchi.adapters.claude.executor",
    "yordamchi.adapters.codex",
    "yordamchi.adapters.ssh",
    "yordamchi.adapters.git",
    "yordamchi.adapters.github",
    "yordamchi.ports.remote_shell",
    "yordamchi.ports.git",
    "yordamchi.ports.github",
)
SOURCES = (
    "yordamchi.personal.qa.service",
    "yordamchi.personal.qa.domain",
    "yordamchi.personal.deps",
    "yordamchi.shared.bridge.service",
    "yordamchi.shared.bridge.store",
    "yordamchi.adapters.claude.assistant",
    "yordamchi.adapters.gemini.assistant",
)


@pytest.fixture(scope="module")
def graph() -> grimp.ImportGraph:
    return grimp.build_graph("yordamchi")


@pytest.mark.parametrize("source", SOURCES)
def test_no_import_chain_reaches_the_dev_side(graph: grimp.ImportGraph, source: str) -> None:
    chains: dict[str, tuple[str, ...]] = {}
    for target in FORBIDDEN:
        candidates = [module for module in graph.modules if module == target or module.startswith(f"{target}.")]
        for module in candidates:
            chain = graph.find_shortest_chain(importer=source, imported=module)
            if chain is not None:
                chains[module] = chain
    assert chains == {}


def test_the_graph_sees_the_modules(graph: grimp.ImportGraph) -> None:
    """Not blind: the dev side exists in the graph, and the dev deps do reach the executor port."""
    assert set(SOURCES) <= graph.modules
    assert graph.find_shortest_chain(importer="yordamchi.dev.deps", imported="yordamchi.ports.agent") is not None
