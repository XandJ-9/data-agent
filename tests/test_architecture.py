"""AST-level architecture rules: runtime core independence and layer boundaries."""

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"

STDLIB_FORBIDDEN = {"openai", "pydantic", "sqlalchemy", "fastapi", "yaml", "lark_oapi", "pydantic_settings"}
FORBIDDEN_EXTERNAL = STDLIB_FORBIDDEN | {
    "api", "channels", "cli", "domains", "providers", "services", "settings", "worker"}


def py_files(package: str) -> list[Path]:
    return sorted(p for p in (SRC / package).rglob("*.py") if "__pycache__" not in p.parts)


def imports_of(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            modules.add(node.module)
    return modules


def local_roots(modules: set[str], *, package: str | None = None) -> set[str]:
    """Intra-project import roots (anything that is not a src/ top-level package
    is external); a package importing its own submodules is not a cross-layer dependency."""
    roots = {m.split(".")[0] for m in modules if (SRC / m.split(".")[0]).is_dir()}
    return roots - {package} if package else roots


def test_runtime_core_depends_only_on_the_neutral_tool_registry() -> None:
    for path in py_files("runtime"):
        modules = imports_of(path)
        assert local_roots(modules, package="runtime") <= {"tools"}, path
        assert not local_roots(modules) & FORBIDDEN_EXTERNAL, path


def test_tools_never_import_runtime_or_domains() -> None:
    for path in py_files("tools"):
        modules = imports_of(path)
        assert local_roots(modules, package="tools") <= {"tools"}, path
        assert not local_roots(modules) & STDLIB_FORBIDDEN, path


def test_providers_own_the_sdk_and_never_reach_domains_or_storage() -> None:
    for path in py_files("providers"):
        modules = imports_of(path)
        assert local_roots(modules, package="providers") <= {"runtime", "settings"}, path


def test_domains_compose_runtime_tools_providers_only() -> None:
    for path in py_files("domains"):
        modules = imports_of(path)
        assert local_roots(modules, package="domains") <= {"runtime", "tools", "providers"}, path
        assert not local_roots(modules) & STDLIB_FORBIDDEN - {"pydantic"}, path


def test_obsolete_agent_directory_is_gone() -> None:
    assert not (SRC / "agent").exists()
