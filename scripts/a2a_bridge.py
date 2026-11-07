#!/usr/bin/env python3
"""Shim: the code lives in a2a_bridge.py at the repo root (console script: iac-bus-bridge).

Loads that file by path so this shim never shadows the root module.
"""
import importlib.util
import sys
from pathlib import Path

_target = Path(__file__).resolve().parents[1] / "a2a_bridge.py"
_spec = importlib.util.spec_from_file_location("a2a_bridge", _target)
_module = importlib.util.module_from_spec(_spec)
sys.modules["a2a_bridge"] = _module
_spec.loader.exec_module(_module)

if __name__ == "__main__":
    sys.exit(_module.main())
