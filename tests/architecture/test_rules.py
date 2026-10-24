"""A1–A9: the rules import-linter cannot express, checked on the AST (CONVENTIONS §2; BonviCall `test_layering.py`).

Each test names its rule. A failure message says which file broke it and what to do instead.
"""

import ast
import fnmatch
import re
import sys
from collections.abc import Iterator
from functools import cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "yordamchi"
FEATURE_GROUPS = ("shared", "dev", "personal")
NON_FEATURE_MODULES = ("deps",)


@cache
def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _src_files() -> list[Path]:
    return sorted(SRC.rglob("*.py"))


def _rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def _module(path: Path) -> str:
    parts = path.relative_to(SRC.parent).with_suffix("").parts
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def _feature(module: str) -> tuple[str, str] | None:
    """`yordamchi.shared.settings.store` → (`shared`, `settings`)."""
    parts = module.split(".")
    if len(parts) >= 3 and parts[1] in FEATURE_GROUPS and parts[2] not in NON_FEATURE_MODULES:
        return parts[1], parts[2]
    return None


def _imported(tree: ast.Module) -> Iterator[tuple[str, ast.stmt]]:
    """Every imported module name; for `from a import b` both `a` and `a.b` (b may be a submodule)."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name, node
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            yield node.module, node
            for alias in node.names:
                yield f"{node.module}.{alias.name}", node


def _aliases(tree: ast.Module) -> dict[str, str]:
    """Local name → module path, for names bound by imports."""
    names: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names[alias.asname or alias.name.split(".")[0]] = alias.name
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            for alias in node.names:
                names[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return names


def _calls(tree: ast.AST, name: str) -> list[ast.Call]:
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if (isinstance(func, ast.Name) and func.id == name) or (
                isinstance(func, ast.Attribute) and func.attr == name
            ):
                found.append(node)
    return found


def _root_name(expr: ast.expr) -> str | None:
    while isinstance(expr, (ast.Attribute, ast.Call, ast.Subscript)):
        expr = expr.func if isinstance(expr, ast.Call) else expr.value
    return expr.id if isinstance(expr, ast.Name) else None


# --- A1 -------------------------------------------------------------------------------------------------------------
A1_FORBIDDEN = {"asyncio", "os", "subprocess", "socket", "shutil", "time", "logging"}
# The settings declarations are imported by every domain, so they are held to the same purity (C6 lists them too).
A1_EXTRA_PURE = ("shared/settings/registry.py", "shared/settings/defaults.py")


def _pure_modules() -> list[Path]:
    domains = [path for path in _src_files() if path.name == "domain.py" and _feature(_module(path))]
    return domains + [SRC / extra for extra in A1_EXTRA_PURE]


def test_a1_domain_modules_are_pure() -> None:
    problems: list[str] = []
    for path in _pure_modules():
        tree = _tree(path)
        forbidden = sorted({name.split(".")[0] for name, _ in _imported(tree)} & A1_FORBIDDEN)
        problems.extend(f"{_rel(path)} imports {name}" for name in forbidden)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "pathlib":
                problems.extend(
                    f"{_rel(path)} imports pathlib.{alias.name} (use PurePosixPath)"
                    for alias in node.names
                    if alias.name == "Path"
                )
            if isinstance(node, ast.Attribute) and node.attr == "Path" and _root_name(node) == "pathlib":
                problems.append(f"{_rel(path)} uses pathlib.Path (use PurePosixPath)")
        problems.extend(
            f"{_rel(path)}:{call.lineno} calls open()"
            for call in _calls(tree, "open")
            if isinstance(call.func, ast.Name)
        )
    assert problems == [], "A1: domain code is pure — inputs are arguments, no I/O:\n  " + "\n  ".join(problems)


def test_a1_checks_at_least_the_settings_declarations() -> None:
    assert all(path.exists() for path in _pure_modules())


# --- A2 -------------------------------------------------------------------------------------------------------------
def test_a2_features_use_each_others_service_or_domain_only() -> None:
    problems: list[str] = []
    for path in _src_files():
        own = _feature(_module(path))
        for name, node in _imported(_tree(path)):
            target = _feature(name)
            if target is None or target == own:
                continue
            leaf = name.split(".")[3] if len(name.split(".")) > 3 else ""
            if leaf in {"store", "models"}:
                problems.append(f"{_rel(path)}:{node.lineno} imports {name}")
    assert problems == [], "A2: call the other feature's service instead:\n  " + "\n  ".join(problems)


def test_a2_foreign_keys_are_strings_and_relationships_stay_in_the_feature() -> None:
    problems: list[str] = []
    for path in _src_files():
        if path.name != "models.py":
            continue
        tree = _tree(path)
        local_classes = {node.name for node in tree.body if isinstance(node, ast.ClassDef)}
        for call in _calls(tree, "ForeignKey"):
            first = call.args[0] if call.args else None
            if not (isinstance(first, ast.Constant) and isinstance(first.value, str)):
                problems.append(f"{_rel(path)}:{call.lineno} ForeignKey target must be a 'table.id' string")
        for call in _calls(tree, "relationship"):
            first = call.args[0] if call.args else None
            target = first.value if isinstance(first, ast.Constant) else getattr(first, "id", None)
            if target not in local_classes:
                problems.append(f"{_rel(path)}:{call.lineno} relationship() to {target!r} outside this feature")
    assert problems == [], "A2:\n  " + "\n  ".join(problems)


# --- A3 -------------------------------------------------------------------------------------------------------------
# Public helpers in a service that take neither: pure conversions only.
A3_PURE_HELPERS: dict[str, tuple[str, ...]] = {"src/yordamchi/shared/settings/service.py": ("as_json",)}


def _service_files() -> list[Path]:
    return [path for path in _src_files() if path.name == "service.py" and _feature(_module(path))]


def test_a3_service_functions_take_deps_or_a_session_first() -> None:
    problems: list[str] = []
    for path in _service_files():
        for node in _tree(path).body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("_"):
                params = [*node.args.posonlyargs, *node.args.args]
                first = params[0].arg if params else None
                if first not in {"deps", "s"} and node.name not in A3_PURE_HELPERS.get(_rel(path), ()):
                    problems.append(f"{_rel(path)}:{node.lineno} {node.name}({first or ''}…)")
    assert problems == [], "A3: a service's first parameter is `deps` or `s`:\n  " + "\n  ".join(problems)


def _allowed_in_uow(call: ast.Call, session: str, aliases: dict[str, str], local_functions: set[str]) -> bool:
    func = call.func
    first_arg = call.args[0] if call.args else None
    passes_session = isinstance(first_arg, ast.Name) and first_arg.id == session
    if isinstance(func, ast.Name):
        return func.id in local_functions and passes_session
    root = _root_name(func)
    if root == session:
        return True
    module = aliases.get(root or "", "")
    if module.endswith(".store") or root == "store":
        return True
    return module.endswith((".service", "_service")) and passes_session


def test_a3_no_port_call_inside_a_write_transaction() -> None:
    problems: list[str] = []
    for path in _service_files():
        tree = _tree(path)
        aliases = _aliases(tree)
        local_functions = {node.name for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncWith):
                continue
            for item in node.items:
                expr = item.context_expr
                is_uow = isinstance(expr, ast.Call) and isinstance(expr.func, ast.Attribute) and expr.func.attr == "uow"
                if not is_uow or not isinstance(item.optional_vars, ast.Name):
                    continue
                session = item.optional_vars.id
                for inner in (child for statement in node.body for child in ast.walk(statement)):
                    if isinstance(inner, ast.Await):
                        value = inner.value
                        if not (
                            isinstance(value, ast.Call) and _allowed_in_uow(value, session, aliases, local_functions)
                        ):
                            problems.append(f"{_rel(path)}:{inner.lineno} awaits {ast.unparse(value)[:60]}")
                    elif isinstance(inner, (ast.AsyncWith, ast.AsyncFor)):
                        exprs = (
                            [i.context_expr for i in inner.items] if isinstance(inner, ast.AsyncWith) else [inner.iter]
                        )
                        problems.extend(
                            f"{_rel(path)}:{inner.lineno} async block on {ast.unparse(e)[:60]}"
                            for e in exprs
                            if _root_name(e) != session
                        )
    assert problems == [], (
        "A3: inside `async with deps.db.uow() as s:` await only store.*, s.* or <feature>.service.*(s, …); "
        "call ports before or after the transaction:\n  " + "\n  ".join(problems)
    )


# --- A4 -------------------------------------------------------------------------------------------------------------
A4_EXEMPT = {("Clock", "sleep")}  # the duration is the bound


def _is_protocol(node: ast.ClassDef) -> bool:
    return any(
        (isinstance(base, ast.Name) and base.id == "Protocol")
        or (isinstance(base, ast.Attribute) and base.attr == "Protocol")
        for base in node.bases
    )


def _protocols() -> Iterator[tuple[Path, ast.ClassDef]]:
    for path in sorted((SRC / "ports").glob("*.py")):
        for node in _tree(path).body:
            if isinstance(node, ast.ClassDef) and _is_protocol(node):
                yield path, node


def _kwonly_float(function: ast.FunctionDef | ast.AsyncFunctionDef, name: str) -> bool:
    return any(
        arg.arg == name and isinstance(arg.annotation, ast.Name) and arg.annotation.id == "float"
        for arg in function.args.kwonlyargs
    )


def test_a4_every_port_call_is_bounded() -> None:
    problems: list[str] = []
    for path, protocol in _protocols():
        for method in protocol.body:
            if isinstance(method, ast.AsyncFunctionDef):
                if (protocol.name, method.name) not in A4_EXEMPT and not _kwonly_float(method, "timeout"):
                    problems.append(f"{_rel(path)} {protocol.name}.{method.name} lacks `*, timeout: float`")
            elif isinstance(method, ast.FunctionDef) and method.returns is not None:
                returns = ast.unparse(method.returns)
                if re.match(r"Async(Iterator|Iterable|Generator)\b", returns) and not _kwonly_float(
                    method, "idle_timeout"
                ):
                    problems.append(f"{_rel(path)} {protocol.name}.{method.name} lacks `*, idle_timeout: float`")
    assert problems == [], "A4:\n  " + "\n  ".join(problems)


def test_a4_finds_the_ports() -> None:
    assert {node.name for _, node in _protocols()} >= {"AgentRuntime", "Messenger", "Clock", "Keystore"}


# --- A5 -------------------------------------------------------------------------------------------------------------
A5_HOMES = {
    "Msg": ("src/yordamchi/core/strings.py",),
    "BotText": ("src/yordamchi/core/text.py", "src/yordamchi/core/redact.py"),
    "OwnerOrigin": ("src/yordamchi/shared/access/*",),
}


def test_a5_bot_text_and_owner_origin_have_one_home() -> None:
    problems: list[str] = []
    for path in _src_files():
        rel = _rel(path)
        for name, homes in A5_HOMES.items():
            calls = [call for call in _calls(_tree(path), name) if isinstance(call.func, ast.Name)]
            if calls and not any(fnmatch.fnmatch(rel, home) for home in homes):
                problems.append(f"{rel}:{calls[0].lineno} calls {name}(")
    assert problems == [], "A5:\n  " + "\n  ".join(problems)


# --- A6 -------------------------------------------------------------------------------------------------------------
def test_a6_supervisor_imports_nothing_from_the_app() -> None:
    problems: list[str] = []
    for path in sorted((ROOT / "supervisor").rglob("*.py")):
        if ".venv" in path.parts:
            continue
        for name, node in _imported(_tree(path)):
            if name == "yordamchi" or name.startswith("yordamchi."):
                problems.append(f"{_rel(path)}:{node.lineno} imports {name}")
    assert problems == [], "A6: the supervisor must survive a broken app:\n  " + "\n  ".join(problems)


# --- A7 -------------------------------------------------------------------------------------------------------------
def test_a7_telegram_handlers_await_exactly_one_service_call() -> None:
    problems: list[str] = []
    for path in sorted(SRC.glob("bots/*/telegram.py")):
        for node in ast.walk(_tree(path)):
            if isinstance(node, ast.AsyncFunctionDef) and node.decorator_list:
                awaits = [child for child in ast.walk(node) if isinstance(child, ast.Await)]
                if len(awaits) != 1:
                    problems.append(f"{_rel(path)}:{node.lineno} {node.name} awaits {len(awaits)} times")
    assert problems == [], "A7: handlers translate, persist through one service call, and return:\n  " + "\n  ".join(
        problems
    )


# --- A8 -------------------------------------------------------------------------------------------------------------
LOG_METHODS = {"debug", "info", "warning", "error", "exception", "critical"}
EVENT = re.compile(r"[a-z][a-z0-9_]*")
SLEEP_HOMES = ("src/yordamchi/core/aio.py", "src/yordamchi/core/retry.py", "src/yordamchi/adapters/system_clock.py")


def test_a8_log_events_are_snake_case_literals() -> None:
    problems: list[str] = []
    for path in _src_files():
        for node in ast.walk(_tree(path)):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in LOG_METHODS
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in {"log", "logger"}
            ):
                first = node.args[0] if node.args else None
                event = first.value if isinstance(first, ast.Constant) else None
                if not (isinstance(event, str) and EVENT.fullmatch(event)):
                    problems.append(f"{_rel(path)}:{node.lineno} {ast.unparse(node)[:70]}")
    assert problems == [], 'A8: log.<level>("snake_case_event", field=…):\n  ' + "\n  ".join(problems)


def test_a8_system_prompt_is_never_a_literal() -> None:
    problems = [
        f"{_rel(path)}:{node.value.lineno}"
        for path in _src_files()
        for node in ast.walk(_tree(path))
        if isinstance(node, ast.keyword)
        and node.arg == "system_prompt"
        and isinstance(node.value, (ast.Constant, ast.JoinedStr))
    ]
    assert problems == [], "A8: system prompts come from prompts/** via prompts.render():\n  " + "\n  ".join(problems)


def test_a8_only_the_clock_and_loop_helpers_sleep() -> None:
    problems: list[str] = []
    for path in _src_files():
        rel = _rel(path)
        if rel in SLEEP_HOMES:
            continue
        for call in _calls(_tree(path), "sleep"):
            is_asyncio = isinstance(call.func, ast.Attribute) and _root_name(call.func) == "asyncio"
            zero = bool(call.args) and isinstance(call.args[0], ast.Constant) and call.args[0].value == 0
            if is_asyncio and not zero:
                problems.append(f"{rel}:{call.lineno}")
    assert problems == [], (
        "A8: wait with deps.clock.sleep(); asyncio.sleep(n>0) only in "
        + ", ".join(SLEEP_HOMES)
        + (":\n  " + "\n  ".join(problems))
    )


def _protected_lines() -> tuple[list[str], list[str]]:
    active: list[str] = []
    pending: list[str] = []
    for raw in (ROOT / ".protected-core").read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line.startswith("# pending:"):
            pending.append(line.removeprefix("# pending:").strip())
        elif line and not line.startswith("#"):
            active.append(line)
    return active, pending


def _matches(glob: str) -> list[Path]:
    return [path for path in ROOT.glob(glob) if ".venv" not in path.parts and "__pycache__" not in path.parts]


def test_a8_every_protected_core_glob_matches_a_file() -> None:
    active, _ = _protected_lines()
    assert active, ".protected-core lists nothing"
    unmatched = [glob for glob in active if not _matches(glob)]
    assert unmatched == [], f"A8: .protected-core globs matching nothing: {unmatched}"


def test_pending_protected_globs_are_promoted_once_their_file_exists() -> None:
    _, pending = _protected_lines()
    ready = [glob for glob in pending if _matches(glob)]
    assert ready == [], f"move these from '# pending:' to active lines in .protected-core: {ready}"


# --- A9 (SPEC §3.1: the CLI entry points start fast) ----------------------------------------------------------------
def test_a9_cli_modules_import_only_core_and_the_standard_library() -> None:
    problems: list[str] = []
    allowed_stdlib = set(sys.stdlib_module_names)
    for path in sorted(SRC.glob("bots/cli/*.py")):
        for node in ast.walk(_tree(path)):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                names = [node.module]
            for name in names:
                top = name.split(".")[0]
                if top in allowed_stdlib or name == "yordamchi.core" or name.startswith("yordamchi.core."):
                    continue
                problems.append(f"{_rel(path)} imports {name}")
    assert problems == [], "A9: bots/cli/* imports only yordamchi.core and the stdlib:\n  " + "\n  ".join(problems)


# --- greps CONVENTIONS names as checks ------------------------------------------------------------------------------
def _grep(pattern: str) -> list[str]:
    regex = re.compile(pattern)
    return [
        f"{_rel(path)}:{number}"
        for path in _src_files()
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if regex.search(line)
    ]


def test_no_reply_markup_anywhere_in_src() -> None:
    """D8, UC-12: no inline buttons, reply keyboards or menus."""
    assert _grep(r"reply_markup") == []


def test_no_inline_prompt_text_in_src() -> None:
    """CONVENTIONS §3: prompt text lives in prompts/**/*.md."""
    assert _grep(r"You are\b") == []


def test_only_the_unit_of_work_commits() -> None:
    assert [hit for hit in _grep(r"\.commit\(") if not hit.startswith("src/yordamchi/persistence/uow.py")] == []


def test_refused_credential_names_live_only_in_core_proc() -> None:
    """CONVENTIONS §13: setting or passing these is forbidden; only the refuse-list names them."""
    hits = _grep(r"ANTHROPIC_API_KEY|CLAUDE_CODE_OAUTH_TOKEN|ANTHROPIC_AUTH_TOKEN|CLAUDE_CONFIG_DIR")
    assert [hit for hit in hits if not hit.startswith("src/yordamchi/core/proc.py")] == []
