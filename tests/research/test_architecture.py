from __future__ import annotations

import ast
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
RESEARCH_ROOT = REPO_ROOT / "research"

# Phases 1-2 are dependency-light and observational.  In particular, the
# research package must not gain a path to brokers, trading/runtime entry
# points, Redis publishing, or recommendation contracts before a later phase
# is separately designed and approved.
FORBIDDEN_DEPENDENCY_PREFIXES = (
    "backtest",
    "ib_insync",
    "redis",
    "scripts",
    "services",
    "shared.redis_client",
    "shared.schemas",
)


def _import_targets(node: ast.Import | ast.ImportFrom) -> list[str]:
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]

    # Relative imports remain inside the research package.  For absolute
    # ``from x import y`` imports, inspect both x and x.y: checking only
    # node.module would miss ``from services import execution``.
    if node.level:
        return []
    module = node.module or ""
    targets = [module] if module else []
    targets.extend(
        f"{module}.{alias.name}" if module else alias.name
        for alias in node.names
        if alias.name != "*"
    )
    return targets


def _is_forbidden(target: str) -> bool:
    return any(
        target == prefix or target.startswith(f"{prefix}.")
        for prefix in FORBIDDEN_DEPENDENCY_PREFIXES
    )


def _boundary_violations(package_root: Path) -> list[str]:
    violations: list[str] = []
    for path in sorted(package_root.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        importlib_aliases: set[str] = set()
        import_module_aliases: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                importlib_aliases.update(
                    alias.asname or alias.name
                    for alias in node.names
                    if alias.name == "importlib"
                )
            elif isinstance(node, ast.ImportFrom) and node.module == "importlib":
                import_module_aliases.update(
                    alias.asname or alias.name
                    for alias in node.names
                    if alias.name == "import_module"
                )

        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for target in _import_targets(node):
                    if _is_forbidden(target):
                        violations.append(
                            f"{path.relative_to(package_root)}:{node.lineno}:{target}"
                        )
            elif isinstance(node, ast.Call):
                # Arbitrary/dynamic module loading is outside the reviewed
                # factor registry and could conceal a forbidden dependency.
                function = node.func
                is_dynamic_import = (
                    isinstance(function, ast.Name)
                    and (
                        function.id == "__import__"
                        or function.id in import_module_aliases
                    )
                ) or (
                    isinstance(function, ast.Attribute)
                    and isinstance(function.value, ast.Name)
                    and function.value.id in importlib_aliases
                    and function.attr == "import_module"
                )
                if is_dynamic_import:
                    violations.append(
                        f"{path.relative_to(package_root)}:{node.lineno}:dynamic-import"
                    )
    return violations


def test_research_package_cannot_depend_on_trading_runtime_surfaces():
    modules = sorted(RESEARCH_ROOT.rglob("*.py"))
    assert modules, "research package is missing; boundary scan would be vacuous"
    assert _boundary_violations(RESEARCH_ROOT) == []


@pytest.mark.parametrize(
    ("source", "expected_target"),
    [
        ("import ib_insync\n", "ib_insync"),
        ("from services import execution\n", "services"),
        ("from services import risk_management\n", "services"),
        ("from shared import redis_client\n", "shared.redis_client"),
        ("from shared.schemas import messages\n", "shared.schemas"),
        ("from scripts.run_paper import run_daily\n", "scripts.run_paper"),
        ("from backtest.runner import BacktestRunner\n", "backtest.runner"),
        ("import redis.asyncio\n", "redis.asyncio"),
        ("import importlib\nimportlib.import_module('services.execution')\n", "dynamic-import"),
        ("import importlib as loader\nloader.import_module('services.execution')\n", "dynamic-import"),
        ("from importlib import import_module\nimport_module('ib_insync')\n", "dynamic-import"),
        ("from importlib import import_module as load\nload('ib_insync')\n", "dynamic-import"),
        ("__import__('services.risk_management')\n", "dynamic-import"),
    ],
)
def test_boundary_scanner_detects_forbidden_dependency_shapes(
    tmp_path: Path,
    source: str,
    expected_target: str,
):
    package_root = tmp_path / "research"
    package_root.mkdir()
    (package_root / "example.py").write_text(source)

    violations = _boundary_violations(package_root)

    assert any(row.endswith(f":{expected_target}") for row in violations)


def test_boundary_scanner_ignores_comments_strings_and_allowed_dependencies(
    tmp_path: Path,
):
    package_root = tmp_path / "research"
    package_root.mkdir()
    (package_root / "example.py").write_text(
        "# import ib_insync\n"
        "description = 'from services import execution'\n"
        "from research.factors import contracts\n"
        "from shared.models import research\n"
        "import pandas as pd\n"
        "def import_module(name):\n"
        "    return name\n"
        "import_module('services.execution')\n"
    )

    assert _boundary_violations(package_root) == []
