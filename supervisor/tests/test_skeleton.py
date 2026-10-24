"""Skeleton checks for the supervisor package, run under its own interpreter with `unittest` (no pytest)."""

import ast
import sys
import tomllib
import unittest
from pathlib import Path

import yordamchi_supervisor

ROOT = Path(__file__).resolve().parents[1]


class SkeletonTest(unittest.TestCase):
    def test_runs_on_the_pinned_python(self) -> None:
        self.assertEqual(sys.version_info[:2], (3, 12))

    def test_declares_no_dependencies(self) -> None:
        project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
        self.assertEqual(project["dependencies"], [])

    def test_imports_only_the_standard_library(self) -> None:
        allowed = set(sys.stdlib_module_names) | {"yordamchi_supervisor", "tests"}
        for path in sorted((ROOT / "src").rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                names: list[str] = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                    names = [node.module]
                for name in names:
                    self.assertIn(name.split(".")[0], allowed, f"{path.name} imports {name}")

    def test_package_has_a_version(self) -> None:
        self.assertTrue(yordamchi_supervisor.__version__)


if __name__ == "__main__":
    unittest.main()
