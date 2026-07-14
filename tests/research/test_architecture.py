from __future__ import annotations

import ast
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
RESEARCH_ROOT = REPO_ROOT / "research"

# Phases 1-2 are dependency-light and observational.  In particular, the
# research package must not statically import brokers, trading/runtime entry
# points, Redis publishing, recommendation contracts, or standard dynamic
# loaders before a later phase is separately designed and approved.
FORBIDDEN_DEPENDENCY_PREFIXES = (
    "backtest",
    "builtins.__import__",
    "ib_insync",
    "importlib",
    "redis",
    "runpy",
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
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for target in _import_targets(node):
                    if _is_forbidden(target):
                        violations.append(
                            f"{path.relative_to(package_root)}:{node.lineno}:{target}"
                        )
            elif isinstance(node, ast.Call):
                # Loader-module imports are rejected above.  Retain a direct
                # check for the builtin form, which has no import statement.
                function = node.func
                is_dynamic_import = (
                    isinstance(function, ast.Name)
                    and function.id == "__import__"
                )
                if is_dynamic_import:
                    violations.append(
                        f"{path.relative_to(package_root)}:{node.lineno}:dynamic-import"
                    )
    return violations


def test_research_package_has_no_prohibited_static_imports_or_loaders():
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
        ("import importlib\nimportlib.import_module('services.execution')\n", "importlib"),
        ("import importlib as loader\nload = loader.import_module\nload('services.execution')\n", "importlib"),
        ("from importlib import import_module\nimport_module('ib_insync')\n", "importlib"),
        ("from importlib import import_module as load\nload('ib_insync')\n", "importlib"),
        ("import runpy\nload = getattr(runpy, 'run_module')\nload('services.execution')\n", "runpy"),
        ("from runpy import run_module as load\nload('services.execution')\n", "runpy"),
        ("from builtins import __import__ as load\nload('ib_insync')\n", "builtins.__import__"),
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
