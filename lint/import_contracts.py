"""Custom import-linter contract types, registered in `pyproject.toml` (`[tool.importlinter] contract_types`).

Copied from MegaPlus Platform `backend/lint/import_contracts.py` (CONVENTIONS §2, C5), with the field access typed
for mypy. `lint-imports` puts the working directory (the repo root) on `sys.path`, so this package is found without
installing it.
"""

from grimp import ImportGraph
from importlinter.contracts.protected import ProtectedContract
from importlinter.domain.contract import ContractCheck
from importlinter.domain.imports import ModuleExpression


class ConfinedContract(ProtectedContract):
    """`protected`, except that a protected package nothing imports yet is skipped instead of being an error.

    SDK confinement (C5) is declared before the SDKs are used: aiogram arrives with `adapters/telegram`, mcp with
    `adapters/mcp`. The built-in `protected` contract fails on a module that is not in the import graph; this one
    checks each protected package as soon as something imports it.
    """

    type_name = "confined"

    # import-linter reads a contract's fields from the class's own __dict__, so they are declared again here.
    protected_modules = ProtectedContract.protected_modules
    allowed_importers = ProtectedContract.allowed_importers
    ignore_imports = ProtectedContract.ignore_imports
    unmatched_ignore_imports_alerting = ProtectedContract.unmatched_ignore_imports_alerting
    as_packages = ProtectedContract.as_packages
    broken_contract_guidance = ProtectedContract.broken_contract_guidance

    def check(self, graph: ImportGraph, verbose: bool) -> ContractCheck:
        # Parsed field values live on the instance (Contract._populate_fields uses setattr), shadowing the class
        # field descriptors, so they are swapped through the instance dict.
        fields = vars(self)
        declared: list[ModuleExpression] = list(fields["protected_modules"])
        fields["protected_modules"] = [
            expression
            for expression in declared
            if expression.has_wildcard_expression() or expression.expression in graph.modules
        ]
        try:
            return super().check(graph, verbose)
        finally:
            fields["protected_modules"] = declared
