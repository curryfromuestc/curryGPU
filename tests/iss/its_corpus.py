from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from currygpu.isa import assembler


ARCH_STATE_KEYS = (
    "active_mask",
    "pc",
    "lane_state",
    "vgpr",
    "predicates",
    "uniform_registers",
    "memory",
    "bx",
    "trap",
)

SCHED_ORDERS = ("min_pc_first", "max_pc_first", "round_robin", "oldest_group_first")


@dataclass(frozen=True)
class CorpusCase:
    name: str
    words: tuple[int, ...]
    expected_status: str = "well_formed"
    expected_divergent: bool = True
    tags: tuple[str, ...] = ()


class KernelBuilder:
    def __init__(self) -> None:
        self._items: list[tuple[str | None, str | None, tuple[Any, ...], dict[str, Any]]] = []

    def label(self, name: str) -> None:
        self._items.append((name, None, (), {}))

    def emit(self, mnemonic: str, *operands: Any, **kwargs: Any) -> None:
        self._items.append((None, mnemonic, operands, kwargs))

    def words(self) -> tuple[int, ...]:
        labels: dict[str, int] = {}
        pc = 0
        for label, mnemonic, _, _ in self._items:
            if label is not None:
                if label in labels:
                    raise ValueError(f"duplicate label {label}")
                labels[label] = pc
            elif mnemonic is not None:
                pc += 1

        words: list[int] = []
        for _, mnemonic, operands, kwargs in self._items:
            if mnemonic is None:
                continue
            resolved_operands = tuple(self._resolve_operand(value, labels) for value in operands)
            resolved_kwargs = {key: self._resolve_operand(value, labels) for key, value in kwargs.items()}
            words.append(assembler.emit(mnemonic, *resolved_operands, **resolved_kwargs))
        return tuple(words)

    def _resolve_operand(self, value: Any, labels: dict[str, int]) -> Any:
        if isinstance(value, str) and value.startswith("@"):
            return labels[value[1:]] * 16
        return value


def architectural_subset(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {key: snapshot[key] for key in ARCH_STATE_KEYS}


def pre_screen(case: CorpusCase) -> tuple[bool, str]:
    if case.expected_status == "reject_static":
        return False, "collective_placement"
    return True, ""


def corpus_cases() -> tuple[CorpusCase, ...]:
    return (
        _if_else_case(),
        _nested_case(),
        _loop_break_continue_case(),
        _early_exit_case(),
        _subwarp_collective_case(),
        _variable_reduction_case(),
        _causal_mask_control_divergent_case(),
        _yield_arrival_case(),
        _collective_placement_negative_case(),
    )


def _if_else_case() -> CorpusCase:
    kb = KernelBuilder()
    kb.emit("ELECT", "P0", 0xFFFFFFFF)
    kb.emit("BSSY", "B0", "@join")
    kb.emit("BRA", "@else", guard="P0")
    kb.emit("IADD3", "R2", "RZ", "RZ", 20)
    kb.emit("BRA", "@join")
    kb.label("else")
    kb.emit("IADD3", "R2", "RZ", "RZ", 10)
    kb.label("join")
    kb.emit("BSYNC", "B0")
    kb.emit("IADD3", "R3", "R2", "RZ", 1)
    kb.emit("EXIT")
    return CorpusCase("if_else", kb.words(), tags=("base",))


def _nested_case() -> CorpusCase:
    kb = KernelBuilder()
    kb.emit("ELECT", "P0", 0xFFFFFFFF)
    kb.emit("BSSY", "B0", "@outer_join")
    kb.emit("BSSY", "B1", "@inner_join")
    kb.emit("BRA", "@then", guard="P0")
    kb.emit("IADD3", "R2", "RZ", "RZ", 20)
    kb.emit("BRA", "@inner_join")
    kb.label("then")
    kb.emit("IADD3", "R2", "RZ", "RZ", 10)
    kb.label("inner_join")
    kb.emit("BSYNC", "B1")
    kb.emit("IADD3", "R3", "R2", "RZ", 1)
    kb.emit("BRA", "@outer_join")
    kb.label("outer_dead")
    kb.emit("IADD3", "R4", "RZ", "RZ", 99)
    kb.label("outer_join")
    kb.emit("BSYNC", "B0")
    kb.emit("IADD3", "R5", "R3", "RZ", 1)
    kb.emit("EXIT")
    return CorpusCase("nested", kb.words(), tags=("base",))


def _loop_break_continue_case() -> CorpusCase:
    kb = KernelBuilder()
    kb.emit("ELECT", "P0", 0xFFFFFFFF)
    kb.emit("BSSY", "B0", "@loop_join")
    kb.emit("BREAK", "B0", guard="P0")
    kb.emit("BRA", "@done", guard="P0")
    kb.label("loop_body")
    kb.emit("IADD3", "R2", "R2", "RZ", 3)
    kb.emit("BRA", "@loop_join")
    kb.label("loop_join")
    kb.emit("BSYNC", "B0")
    kb.label("done")
    kb.emit("EXIT")
    return CorpusCase("loop_break_continue", kb.words(), tags=("base",))


def _early_exit_case() -> CorpusCase:
    kb = KernelBuilder()
    kb.emit("ELECT", "P0", 0xFFFFFFFF)
    kb.emit("BSSY", "B0", "@join")
    kb.emit("EXIT", guard="P0")
    kb.emit("IADD3", "R2", "RZ", "RZ", 7)
    kb.label("join")
    kb.emit("BSYNC", "B0")
    kb.emit("EXIT")
    return CorpusCase("early_exit", kb.words(), expected_divergent=False, tags=("base", "early_exit"))


def _subwarp_collective_case() -> CorpusCase:
    kb = KernelBuilder()
    kb.emit("ISETP", "P0", "RZ", "RZ", cmp="eq")
    kb.emit("ELECT", "P1", 0x0000000F)
    kb.emit("VOTE", "P2", "P0", 0x0000000F, mode="all")
    kb.emit("EXIT")
    return CorpusCase("subwarp_collective", kb.words(), expected_divergent=False, tags=("base",))


def _variable_reduction_case() -> CorpusCase:
    kb = KernelBuilder()
    kb.emit("S2R", "R1", "SR_LANEID")
    kb.emit("IADD3", "R2", "RZ", "RZ", 0)
    kb.emit("IADD3", "R3", "RZ", "RZ", 8)
    kb.emit("IADD3", "R4", "RZ", "RZ", 16)
    kb.emit("IADD3", "R5", "RZ", "RZ", 24)
    kb.emit("IADD3", "R7", "RZ", "RZ", 1)
    kb.emit("ISETP", "P0", "R1", "R3", cmp="ge")
    kb.emit("IADD3", "R7", "RZ", "RZ", 2, guard="P0")
    kb.emit("ISETP", "P0", "R1", "R4", cmp="ge")
    kb.emit("IADD3", "R7", "RZ", "RZ", 3, guard="P0")
    kb.emit("ISETP", "P0", "R1", "R5", cmp="ge")
    kb.emit("IADD3", "R7", "RZ", "RZ", 4, guard="P0")
    kb.label("loop_head")
    kb.emit("BSSY", "B0", "@join")
    kb.emit("ISETP", "P1", "R2", "R7", cmp="ge")
    kb.emit("BREAK", "B0", guard="P1")
    kb.emit("BRA", "@done", guard="P1")
    kb.emit("IADD3", "R2", "R2", "RZ", 1)
    kb.emit("BRA", "@join")
    kb.label("join")
    kb.emit("BSYNC", "B0")
    kb.emit("BRA", "@loop_head")
    kb.label("done")
    kb.emit("EXIT")
    return CorpusCase("variable_reduction_loop", kb.words(), tags=("north_star", "loop_carried_barrier"))


def _causal_mask_control_divergent_case() -> CorpusCase:
    # Causal masking: a key position beyond the query row is masked out. The
    # divergence is data-dependent on each lane's own position (lane index), so
    # the masked-out set is a contiguous laneid range rather than a single leader.
    kb = KernelBuilder()
    kb.emit("S2R", "R1", "SR_LANEID")
    kb.emit("IADD3", "R6", "RZ", "RZ", 15)
    kb.emit("ISETP", "P0", "R1", "R6", cmp="gt")
    kb.emit("BSSY", "B0", "@join")
    kb.emit("BRA", "@masked_out", guard="P0")
    kb.emit("IADD3", "R2", "R1", "RZ", 0)
    kb.emit("BRA", "@join")
    kb.label("masked_out")
    kb.emit("BREAK", "B0")
    kb.emit("IADD3", "R2", "RZ", "RZ", 99)
    kb.emit("BRA", "@done")
    kb.label("join")
    kb.emit("BSYNC", "B0")
    kb.label("done")
    kb.emit("EXIT")
    return CorpusCase("causal_mask_control_divergent", kb.words(), tags=("north_star", "control_divergent"))


def _yield_arrival_case() -> CorpusCase:
    kb = KernelBuilder()
    kb.emit("YIELD")
    kb.emit("BSSY", "B0", "@join")
    kb.emit("IADD3", "R1", "RZ", "RZ", 1)
    kb.label("join")
    kb.emit("BSYNC", "B0")
    kb.emit("EXIT")
    return CorpusCase("yield_arrival", kb.words(), expected_divergent=False, tags=("negative_control", "k1"))


def _collective_placement_negative_case() -> CorpusCase:
    kb = KernelBuilder()
    kb.emit("ELECT", "P1", 0xFFFFFFFF)
    kb.emit("EXIT")
    return CorpusCase(
        "collective_placement_k2",
        kb.words(),
        expected_status="reject_static",
        expected_divergent=False,
        tags=("negative_control", "k2"),
    )
