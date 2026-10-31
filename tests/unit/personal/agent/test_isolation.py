"""REQUIREMENTS D15 / SPEC §3.2.18: the personal agent's guard takes the pure gate classifier from `dev`, and nothing
else. C1 and C7-personal exempt exactly the edge `personal.agent.domain -> dev.gate.domain`; this pins what that edge
reaches on the real import graph, so a later import in the classifier cannot carry the dev side into the personal
bot unseen.
"""

import grimp
import pytest

# The classifier and what it needs; `yordamchi.dev` and `yordamchi.dev.gate` / `.projects` are their packages.
ALLOWED_DEV = frozenset(
    {
        "yordamchi.dev",
        "yordamchi.dev.gate",
        "yordamchi.dev.gate.domain",
        "yordamchi.dev.projects",
        "yordamchi.dev.projects.domain",
    }
)
FORBIDDEN = (
    "yordamchi.adapters",
    "yordamchi.ports.remote_shell",
    "yordamchi.ports.git",
    "yordamchi.ports.github",
    "yordamchi.dev.deps",
    "yordamchi.persistence",
    "sqlalchemy",
)
SOURCES = ("yordamchi.personal.agent.service", "yordamchi.bots.personal.pipeline", "yordamchi.bots.personal.runtime")


@pytest.fixture(scope="module")
def graph() -> grimp.ImportGraph:
    return grimp.build_graph("yordamchi", include_external_packages=True)


@pytest.mark.parametrize("source", SOURCES)
def test_only_the_gate_classifier_is_reached_from_dev(graph: grimp.ImportGraph, source: str) -> None:
    reached = graph.find_upstream_modules(source)
    assert "yordamchi.dev.gate.domain" in reached  # not blind: the guard does run the classifier
    assert {module for module in reached if module.startswith("yordamchi.dev")} <= ALLOWED_DEV


def test_the_classifier_reaches_no_executor_git_github_ssh_or_database(graph: grimp.ImportGraph) -> None:
    reached = graph.find_upstream_modules("yordamchi.dev.gate.domain") | {"yordamchi.dev.gate.domain"}
    hits = sorted(module for module in reached if module.startswith(FORBIDDEN))
    assert hits == []
    assert {module for module in reached if module.startswith("yordamchi.dev")} <= ALLOWED_DEV


def test_the_one_exempt_edge_is_the_agent_domain(graph: grimp.ImportGraph) -> None:
    """Not blind: the edge exists, and no other personal module imports from `dev`."""
    importers = graph.find_modules_that_directly_import("yordamchi.dev.gate.domain")
    personal_side = {
        module for module in importers if module.startswith(("yordamchi.personal", "yordamchi.bots.personal"))
    }
    assert personal_side == {"yordamchi.personal.agent.domain"}
    for module in graph.modules:
        if module.startswith(("yordamchi.personal", "yordamchi.bots.personal")):
            imported = graph.find_modules_directly_imported_by(module)
            dev = {name for name in imported if name.startswith("yordamchi.dev")}
            assert dev <= ({"yordamchi.dev.gate.domain"} if module == "yordamchi.personal.agent.domain" else set()), (
                module
            )
