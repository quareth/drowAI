"""Temporary compatibility surface for the relocated LLMClient factory."""

from __future__ import annotations

import sys
from types import ModuleType

from . import client_factory as _client_factory
from .client_factory import *  # noqa: F401,F403
from .client_factory import __all__ as __all__


class _FactoryCompatibilityModule(ModuleType):
    """Forward observable old-path factory globals to the implementation."""

    def __getattr__(self, name: str):
        return getattr(_client_factory, name)

    def __setattr__(self, name: str, value):
        if name.startswith("__") or name in {"_client_factory"}:
            super().__setattr__(name, value)
            return
        setattr(_client_factory, name, value)
        super().__setattr__(name, value)


sys.modules[__name__].__class__ = _FactoryCompatibilityModule
