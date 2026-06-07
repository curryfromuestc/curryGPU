"""Optional native coarse-boundary ISS bindings."""

from __future__ import annotations

try:
    from . import _native
except ImportError as exc:  # pragma: no cover - exercised when extension is not built
    _native = None
    _import_error = exc
else:
    _import_error = None


def available() -> bool:
    return _native is not None


def _require_native():
    if _native is None:
        raise RuntimeError("native ISS extension is not available") from _import_error
    return _native


def launch(program, *, num_gprs: int = 256):
    return _require_native().launch(program, num_gprs)


def launch_words(words, *, num_gprs: int = 256):
    return _require_native().launch_words(words, num_gprs)


def step(warp, max_steps: int):
    return _require_native().step(warp, max_steps)


def state_diff(left, right):
    return _require_native().state_diff(left, right)


def boundary_calls() -> int:
    return int(_require_native().boundary_calls())


def reset_boundary_calls() -> None:
    _require_native().reset_boundary_calls()
