"""Shared files stay mergeable and complete (TASKS R2; CONVENTIONS §4, §11).

- Block files carry one `# --- <feature> ---` block per P1 feature, in TASKS order, so parallel tasks each write
  inside their own block.
- Every port Protocol has a fake (bound as `_: Port = Fake()`, so mypy checks it) and a contract suite.
"""

import ast
import re
from pathlib import Path
from typing import Final

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "yordamchi"

# TASKS.md feature order (T08 → T32).
P1_FEATURES: Final = (
    "inbox",
    "audit",
    "vault",
    "cost",
    "access",
    "outbox",
    "burst",
    "projects",
    "gate",
    "voice",
    "routing",
    "settings",
    "turns",
    "questions",
    "digest",
    "servers",
    "health",
)
EXPECTED_BLOCKS: Final = ("core", *P1_FEATURES, "wiring (bots)")
BLOCK_FILES: Final = {
    "src/yordamchi/core/strings.py": 1,
    "src/yordamchi/core/errors.py": 2,  # once in ErrorCode, once for the classes
    "src/yordamchi/shared/settings/defaults.py": 1,
    "tests/factories.py": 1,
}
BLOCK = re.compile(r"^\s*# --- ([a-z][a-z0-9 ()]*) ---$", re.MULTILINE)


def test_block_files_carry_every_p1_block_in_order() -> None:
    for relative, times in BLOCK_FILES.items():
        found = BLOCK.findall((ROOT / relative).read_text(encoding="utf-8"))
        known = [block for block in found if block in EXPECTED_BLOCKS]
        assert known == list(EXPECTED_BLOCKS) * times, f"{relative}: blocks {known}"


PORT_FILE_NAMES: Final = {"GitHub": "github"}


def _snake(name: str) -> str:
    return PORT_FILE_NAMES.get(name) or re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def _port_protocols() -> list[str]:
    return [
        node.name
        for path in sorted((SRC / "ports").glob("*.py"))
        for node in ast.parse(path.read_text(encoding="utf-8")).body
        if isinstance(node, ast.ClassDef)
        and any(isinstance(base, ast.Name) and base.id == "Protocol" for base in node.bases)
    ]


def _fake_bindings() -> set[str]:
    """Protocol names annotated on a module-level binding in tests/fakes (`_: Clock = FakeClock()`)."""
    bound: set[str] = set()
    for path in sorted((ROOT / "tests" / "fakes").glob("*.py")):
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            if isinstance(node, ast.AnnAssign) and isinstance(node.annotation, ast.Name):
                bound.add(node.annotation.id)
    return bound


def test_every_port_has_a_fake_checked_by_mypy() -> None:
    missing = sorted(set(_port_protocols()) - _fake_bindings())
    assert missing == [], f"add a fake in tests/fakes/ and bind it as `_: <Port> = Fake<Port>()`: {missing}"


def test_every_port_has_a_contract_suite() -> None:
    contract = ROOT / "tests" / "contract"
    missing = [name for name in _port_protocols() if not (contract / f"test_{_snake(name)}.py").exists()]
    assert missing == [], f"add tests/contract/test_<port>.py for: {missing}"


def test_the_port_table_names_every_port() -> None:
    """CONVENTIONS §4: a Protocol missing from the port table is a review failure."""
    table = (ROOT / "docs" / "CONVENTIONS.md").read_text(encoding="utf-8")
    missing = [name for name in _port_protocols() if f"`{name}`" not in table and name not in table]
    assert missing == [], f"add to the CONVENTIONS §4 port table: {missing}"
