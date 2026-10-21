"""The import direction must follow the layer map.

Every module belongs to a layer, and a module may only import modules in
its own layer or in a lower one. The map below is the architecture of the
package: core, transport, data, request/response, client, convenience API,
database, services, CLI.

The test is offline and uses only the standard library's ``ast`` module. It
also forbids function-local imports of pyzill modules: a lazy import is how
an import cycle hides.
"""

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "pyzill"

# The layer of every module. Higher numbers may only import lower ones.
LAYERS: dict[str, int] = {
    # core
    "pyzill._kinds": 0,
    "pyzill.bbox": 0,
    "pyzill.config": 0,
    "pyzill.exceptions": 0,
    "pyzill.proxy": 0,
    "pyzill.urls": 0,
    "pyzill.utils": 0,
    # transport
    "pyzill.http": 1,
    "pyzill.http_headers": 1,
    "pyzill.ratelimit": 1,
    "pyzill.responses": 1,
    # pure data
    "pyzill.normalize": 2,
    "pyzill.parse": 2,
    "pyzill.property_detail": 2,
    "pyzill.values": 2,
    # request/response
    "pyzill.detail_request": 3,
    "pyzill.geocode": 3,
    "pyzill.search_request": 3,
    "pyzill.tiling": 3,
    # client
    "pyzill.client": 4,
    # the convenience API above the client
    "pyzill.api": 5,
    "pyzill.details": 5,
    "pyzill.search": 5,
    # database
    "pyzill.db": 6,
    "pyzill.query": 6,
    # services
    "pyzill.workflows": 7,
    # cli
    "pyzill.cli": 8,
    # the package facade may only be imported by the outside world
    "pyzill": 9,
}


def _module_name(path: Path) -> str:
    """Return the dotted module name of a source file."""
    parts = list(path.relative_to(SRC).with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(["pyzill", *parts])


def _module_names() -> set[str]:
    """Return the dotted names of every module in the package."""
    names = {"pyzill"}
    for path in SRC.rglob("*.py"):
        names.add(_module_name(path))
    return names


def _imported_modules(tree: ast.Module, package: str) -> set[str]:
    """Return the pyzill modules imported by one module.

    ``from pyzill import http`` imports the ``http`` submodule; a name that
    is not a module comes from the package facade, which only the outside
    world may depend on.
    """
    names = _module_names()
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("pyzill"):
                    imported.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package.split(".")
                if node.level > 1:
                    base = base[: -(node.level - 1)]
                module = ".".join([*base, node.module] if node.module else base)
            elif node.module and node.module.startswith("pyzill"):
                module = node.module
            else:
                continue
            submodules = [f"{module}.{alias.name}" for alias in node.names if f"{module}.{alias.name}" in names]
            if submodules:
                imported.update(submodules)
            else:
                imported.add(module)
    return imported


def _local_imports(tree: ast.Module) -> list[str]:
    """Return the pyzill imports that happen inside a function or class body."""
    local: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        for child in ast.walk(node):
            if isinstance(child, ast.Import):
                local.extend(alias.name for alias in child.names if alias.name.startswith("pyzill"))
            elif isinstance(child, ast.ImportFrom):
                module = child.module or ""
                if module.startswith("pyzill"):
                    local.append(module)
    return local


def _layer_of(module: str) -> int:
    """Return the layer of a module (its own, or its package's)."""
    if module in LAYERS:
        return LAYERS[module]
    parts = module.split(".")
    for index in range(len(parts) - 1, 0, -1):
        prefix = ".".join(parts[:index])
        if prefix in LAYERS:
            return LAYERS[prefix]
    raise KeyError(module)


def test_every_module_has_a_layer() -> None:
    missing = sorted(name for name in _module_names() if name not in LAYERS and "." not in name)
    assert not missing, f"modules without a layer: {missing}"


def test_modules_only_import_lower_layers() -> None:
    violations: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        module = _module_name(path)
        layer = _layer_of(module)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for imported in sorted(_imported_modules(tree, module)):
            try:
                imported_layer = _layer_of(imported)
            except KeyError:
                violations.append(f"{module} imports unknown module {imported}")
                continue
            if imported_layer > layer:
                violations.append(f"{module} (layer {layer}) imports {imported} (layer {imported_layer})")
    assert not violations, "\n".join(violations)


def test_no_function_local_pyzill_imports() -> None:
    violations: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        module = _module_name(path)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        violations.extend(
            f"{module} imports {imported} inside a function or class body" for imported in _local_imports(tree)
        )
    assert not violations, "\n".join(violations)
