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


def launch(
    program,
    *,
    num_gprs: int = 256,
    sched_order: str = "min_pc_first",
    debug_checks: bool = False,
    shared_mem_bytes: int = 49152,
    local_mem_bytes: int = 16384,
    global_allocations=None,
    const_banks=None,
    num_warps: int = 1,
    warp_sched_order: str = "warp_round_robin",
    ntid=None,
    nctaid=None,
    race_check: bool = False,
):
    return _require_native().launch(
        program,
        num_gprs,
        sched_order,
        debug_checks,
        shared_mem_bytes,
        local_mem_bytes,
        global_allocations,
        const_banks,
        num_warps,
        warp_sched_order,
        ntid,
        nctaid,
        race_check,
    )


def launch_words(
    words,
    *,
    num_gprs: int = 256,
    sched_order: str = "min_pc_first",
    debug_checks: bool = False,
    shared_mem_bytes: int = 49152,
    local_mem_bytes: int = 16384,
    global_allocations=None,
    const_banks=None,
    num_warps: int = 1,
    warp_sched_order: str = "warp_round_robin",
    ntid=None,
    nctaid=None,
    race_check: bool = False,
):
    return _require_native().launch_words(
        words,
        num_gprs,
        sched_order,
        debug_checks,
        shared_mem_bytes,
        local_mem_bytes,
        global_allocations,
        const_banks,
        num_warps,
        warp_sched_order,
        ntid,
        nctaid,
        race_check,
    )


def step(warp, max_steps: int):
    return _require_native().step(warp, max_steps)


def state_diff(left, right):
    return _require_native().state_diff(left, right)


def boundary_calls() -> int:
    return int(_require_native().boundary_calls())


def reset_boundary_calls() -> None:
    _require_native().reset_boundary_calls()
