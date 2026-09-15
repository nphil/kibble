"""Test bootstrap: makes `custom_components/kibble` importable as the `kibble` package
without executing its (Home-Assistant-runtime-dependent) `__init__.py`.

`custom_components/kibble/__init__.py` chains into `coordinator.py`, which uses a PEP 695
`type` alias (`type KibbleConfigEntry = ...`) and constructs a real `DataUpdateCoordinator` --
neither works without a full, matching Home Assistant runtime (and, for the `type` statement,
Python >= 3.12). The modules these tests target -- `frame.py`, `ble_fallback.py`, `ble.py`,
`api.py` -- have no such dependency. This registers `kibble` as a namespace package pointing at
the real directory so the relative imports inside them (`from .api import ...`) resolve
normally, while skipping `__init__.py` itself, which nothing here needs.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

_KIBBLE_DIR = Path(__file__).parent.parent / "custom_components" / "kibble"

if "kibble" not in sys.modules:
    _stub = types.ModuleType("kibble")
    _stub.__path__ = [str(_KIBBLE_DIR)]
    sys.modules["kibble"] = _stub
