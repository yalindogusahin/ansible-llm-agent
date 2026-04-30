"""pytest bootstrap for ansible_ai unit tests.

Registers a synthetic ansible_collections.yalindogusahin.ansible_ai.plugins.module_utils
namespace package pointing at the in-repo source so tests can import the
collection's modules without `ansible-galaxy collection install`.
"""

from __future__ import annotations

import pathlib
import sys
import types

ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULE_UTILS = ROOT / "plugins" / "module_utils"


def _register(fqn: str, path: pathlib.Path | None) -> None:
    if fqn in sys.modules:
        return
    mod = types.ModuleType(fqn)
    mod.__path__ = [str(path)] if path else []
    sys.modules[fqn] = mod


_register("ansible_collections", None)
_register("ansible_collections.yalindogusahin", None)
_register("ansible_collections.yalindogusahin.ansible_ai", None)
_register("ansible_collections.yalindogusahin.ansible_ai.plugins", None)
_register("ansible_collections.yalindogusahin.ansible_ai.plugins.module_utils", MODULE_UTILS)
