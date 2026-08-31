"""Namespaced exports and import bridge for approved byte-identical modules."""

from __future__ import annotations

import importlib
import sys


# Approved spike modules retain their original absolute intra-spike imports.
# Install no flat modules: bridge those names only while loading the package,
# then restore the interpreter's previous module table exactly.
_names = (
    "locked_store",
    "journal",
    "cursor",
    "scrub",
    "jwt_verifier",
    "sqlite_store",
    "transactional_sqlite",
    "instance_lock",
)
_previous = {name: sys.modules.get(name) for name in _names}
try:
    for _name in _names:
        _module = importlib.import_module(f".{_name}", __name__)
        sys.modules[_name] = _module
    central = importlib.import_module(".central", __name__)
finally:
    for _name, _module in _previous.items():
        if _module is None:
            sys.modules.pop(_name, None)
        else:
            sys.modules[_name] = _module

build_server = central.build_server

__all__ = ["build_server", "central"]
