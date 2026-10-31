"""Architecture guard: nothing outside ``providers/`` knows a forge.

The core may use the generic provider modules (the ports, the registry and
the settings union), but never a forge's own package (``providers/<type>/``)
nor a forge SDK. A new forge is a new package under ``providers/``; if the
core had to import it, the plugin boundary would be broken (AGENTS.md
§ Provider plugins).
"""

from __future__ import annotations

import ast
from pathlib import Path

import issue_agent

SRC = Path(issue_agent.__file__).parent
PROVIDERS = SRC / "providers"

# Generic modules of the plugin layer the core is allowed to import.
GENERIC_PROVIDER_MODULES = frozenset(
    {
        "issue_agent.providers",
        "issue_agent.providers.base",
        "issue_agent.providers.registry",
        "issue_agent.providers.settings",
    }
)

# Forge SDKs: only a plugin may import them.
FORGE_SDKS = frozenset({"github", "gitlab", "gitea", "pyforgejo", "githubkit"})


def _imported_modules(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            modules.append(node.module)
            # `from issue_agent.providers import github` names a forge package.
            if node.module == "issue_agent.providers":
                modules.extend(f"{node.module}.{alias.name}" for alias in node.names)
    return modules


def _core_files() -> list[Path]:
    return [p for p in SRC.rglob("*.py") if PROVIDERS not in p.parents]


def test_the_scan_sees_the_core() -> None:
    names = {p.relative_to(SRC).as_posix() for p in _core_files()}
    assert {"main.py", "worker.py", "runner/executor.py"} <= names


def test_core_imports_only_the_generic_provider_modules() -> None:
    offenders = [
        f"{path.relative_to(SRC)}: {module}"
        for path in _core_files()
        for module in _imported_modules(path)
        if module.startswith("issue_agent.providers")
        and module not in GENERIC_PROVIDER_MODULES
    ]
    assert offenders == []


def test_core_imports_no_forge_sdk() -> None:
    offenders = [
        f"{path.relative_to(SRC)}: {module}"
        for path in _core_files()
        for module in _imported_modules(path)
        if module.split(".")[0] in FORGE_SDKS
    ]
    assert offenders == []


def test_the_ports_import_no_other_provider_module() -> None:
    # The registry wires the built-in types and the settings union lists
    # their settings models; the ports themselves stay forge-free.
    base = PROVIDERS / "base.py"
    assert not [m for m in _imported_modules(base) if m.startswith("issue_agent.providers.")]
