#!/usr/bin/env python3
"""Build synthetic payloads from the committed shape baselines.

The live captures under ``tests/fixtures/`` stay local: they hold real listing
data. CI still needs a payload to run the drift check against, so this script
turns each committed shape (``tests/shapes/<name>.txt``) back into a payload
with exactly that shape under ``tests/fixtures/synthetic/``. The payloads are
synthetic: every leaf is ``null`` and every array holds one item.

Usage:
    python scripts/build_synthetic_fixtures.py [--check]

``--check`` exits non-zero when the committed payloads are out of date, which
is what a contributor should run after changing a shape baseline.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

_SHAPES = Path("tests/shapes")
_OUT = Path("tests/fixtures/synthetic")


def _assign(node: dict[str, Any], segments: list[str]) -> None:
    """Add one dotted shape path to a payload tree."""
    key, *rest = segments
    is_list = key.endswith("[]")
    name = key[:-2] if is_list else key
    if is_list:
        container = node.get(name)
        if not isinstance(container, list):
            container = []
            node[name] = container
        if not rest:
            if not container:
                container.append(None)
            return
        if not container or not isinstance(container[0], dict):
            container[:] = [{}]
        _assign(container[0], rest)
        return
    if not rest:
        node.setdefault(name, None)
        return
    child = node.get(name)
    if not isinstance(child, dict):
        child = {}
        node[name] = child
    _assign(child, rest)


def payload_for(shape_path: Path) -> dict[str, Any]:
    """Return a payload whose shape is exactly the recorded paths."""
    payload: dict[str, Any] = {}
    for line in shape_path.read_text(encoding="utf-8").splitlines():
        if line:
            _assign(payload, line.split("."))
    return payload


def _rendered(shape_path: Path) -> str:
    return json.dumps(payload_for(shape_path), indent=2) + "\n"


def build(*, shapes: Path = _SHAPES, out: Path = _OUT) -> list[Path]:
    """Write every synthetic payload and return the paths written."""
    out.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for shape_path in sorted(shapes.glob("*.txt")):
        target = out / f"{shape_path.stem}.json"
        target.write_text(_rendered(shape_path), encoding="utf-8")
        written.append(target)
    return written


def stale_payloads(*, shapes: Path = _SHAPES, out: Path = _OUT) -> list[Path]:
    """Return the committed payloads that no longer match their shape."""
    stale: list[Path] = []
    for shape_path in sorted(shapes.glob("*.txt")):
        target = out / f"{shape_path.stem}.json"
        if not target.exists() or target.read_text(encoding="utf-8") != _rendered(shape_path):
            stale.append(target)
    return stale


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="Fail if the committed payloads are out of date")
    args = parser.parse_args()

    if args.check:
        stale = stale_payloads()
        if stale:
            print(f"Out of date: {', '.join(str(path) for path in stale)}", file=sys.stderr)
            return 1
        return 0

    for path in build():
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
