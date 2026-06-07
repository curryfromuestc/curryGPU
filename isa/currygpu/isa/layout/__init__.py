"""ISA layout selection helpers."""

from __future__ import annotations

import os
from importlib import import_module

from .sample import (
    FieldLayout,
    FixedBitLayout,
    InstructionLayout,
    Layout,
    ModifierLayout,
    ReservedLayout,
    SAMPLE_LAYOUT,
)


LAYOUT_ENV_VAR = "CURRYGPU_ISA_LAYOUT"
DEFAULT_LAYOUT = "sample"


class LayoutSelectionError(ValueError):
    """Raised when a requested ISA bit layout is unavailable or invalid."""


def load_layout(name: str | None = None) -> Layout:
    selected = os.environ.get(LAYOUT_ENV_VAR, DEFAULT_LAYOUT) if name is None else name
    normalized = selected.strip().lower()
    if not normalized:
        raise LayoutSelectionError("layout selection must not be empty")
    if normalized == "sample":
        return SAMPLE_LAYOUT
    if normalized == "production":
        return _load_production_layout()
    raise LayoutSelectionError(f"unknown layout selection: {selected}")


def _load_production_layout() -> Layout:
    try:
        module = import_module("currygpu.isa.layout.production")
    except ImportError as exc:
        raise LayoutSelectionError(
            "production layout is not available; inject currygpu.isa.layout.production "
            "before selecting it"
        ) from exc
    try:
        layout = module.PRODUCTION_LAYOUT
    except AttributeError as exc:
        raise LayoutSelectionError("production layout module must define PRODUCTION_LAYOUT") from exc
    if not isinstance(layout, Layout):
        raise LayoutSelectionError("production layout must be a currygpu.isa.layout.Layout")
    return layout


__all__ = [
    "DEFAULT_LAYOUT",
    "LAYOUT_ENV_VAR",
    "FieldLayout",
    "FixedBitLayout",
    "InstructionLayout",
    "Layout",
    "LayoutSelectionError",
    "ModifierLayout",
    "ReservedLayout",
    "SAMPLE_LAYOUT",
    "load_layout",
]
