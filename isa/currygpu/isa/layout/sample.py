"""Sample public bit layout for the curryGPU ISA schema."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping


@dataclass(frozen=True)
class FieldLayout:
    lsb: int
    width: int


@dataclass(frozen=True)
class ReservedLayout:
    name: str
    lsb: int
    width: int
    value: int = 0


@dataclass(frozen=True)
class ModifierLayout:
    field: str
    values: Mapping[str, int]


@dataclass(frozen=True)
class FixedBitLayout:
    name: str
    lsb: int
    width: int
    value: int


@dataclass(frozen=True)
class InstructionLayout:
    name: str
    opcode: int
    opcode_lsb: int
    opcode_width: int
    fields: Mapping[str, FieldLayout]
    reserved: tuple[ReservedLayout, ...] = ()
    modifiers: Mapping[str, ModifierLayout] = MappingProxyType({})
    fixed_bits: tuple[FixedBitLayout, ...] = ()
    ignored: tuple[ReservedLayout, ...] = ()


@dataclass(frozen=True)
class Layout:
    name: str
    control_lsb: int
    control_fields: Mapping[str, FieldLayout]
    instructions: tuple[InstructionLayout, ...]


CONTROL_LAYOUT = MappingProxyType(
    {
        "stall": FieldLayout(107, 4),
        "yield": FieldLayout(111, 1),
        "read_barrier": FieldLayout(112, 3),
        "write_barrier": FieldLayout(115, 3),
        "wait_mask": FieldLayout(118, 6),
        "reuse": FieldLayout(124, 4),
    }
)


WIDTH_VALUES = MappingProxyType({"u8": 0, "s8": 1, "u16": 2, "s16": 3, "32": 4, "64": 5, "128": 6, "256": 7})
ATOMIC_OP_VALUES = MappingProxyType(
    {"add": 0, "min": 1, "max": 2, "inc": 3, "dec": 4, "and": 5, "or": 6, "xor": 7, "exch": 8, "cas": 9}
)
BAR_MODE_VALUES = MappingProxyType({"sync": 0, "arv": 1})
MEM_SCOPE_VALUES = MappingProxyType({"cta": 0, "sm": 1, "gpu": 2, "sys": 3})
MEM_ORDER_VALUES = MappingProxyType({"sc": 0, "acquire": 1, "release": 2, "acq_rel": 3})
CVTA_DIRECTION_VALUES = MappingProxyType(
    {"to_global": 0, "to_shared": 1, "to_local": 2, "from_global": 3, "from_shared": 4, "from_local": 5}
)


def _memory_layout(name: str, opcode: int, data_field: str) -> InstructionLayout:
    return InstructionLayout(
        name=name,
        opcode=opcode,
        opcode_lsb=0,
        opcode_width=8,
        fields=MappingProxyType(
            {
                "guard_pred": FieldLayout(8, 3),
                "guard_neg": FieldLayout(11, 1),
                data_field: FieldLayout(12, 8),
                "addr_base": FieldLayout(20, 8),
                "addr_ur": FieldLayout(28, 8),
                "addr_imm": FieldLayout(36, 20),
                "width": FieldLayout(56, 3),
            }
        ),
        reserved=(ReservedLayout(f"{name.lower()}_reserved", 59, 48),),
        modifiers=MappingProxyType({"width": ModifierLayout("width", WIDTH_VALUES)}),
    )


def _atomic_layout(name: str, opcode: int, has_rd: bool) -> InstructionLayout:
    field_map = {
        "guard_pred": FieldLayout(8, 3),
        "guard_neg": FieldLayout(11, 1),
    }
    if has_rd:
        field_map.update(
            {
                "rd": FieldLayout(12, 8),
                "src": FieldLayout(20, 8),
                "cmp": FieldLayout(28, 8),
                "addr_base": FieldLayout(36, 8),
                "addr_ur": FieldLayout(44, 8),
                "addr_imm": FieldLayout(52, 20),
                "op": FieldLayout(72, 4),
            }
        )
        reserved = (ReservedLayout(f"{name.lower()}_reserved", 76, 31),)
    else:
        field_map.update(
            {
                "src": FieldLayout(12, 8),
                "cmp": FieldLayout(20, 8),
                "addr_base": FieldLayout(28, 8),
                "addr_ur": FieldLayout(36, 8),
                "addr_imm": FieldLayout(44, 20),
                "op": FieldLayout(64, 4),
            }
        )
        reserved = (ReservedLayout(f"{name.lower()}_reserved", 68, 39),)
    return InstructionLayout(
        name=name,
        opcode=opcode,
        opcode_lsb=0,
        opcode_width=8,
        fields=MappingProxyType(field_map),
        reserved=reserved,
        modifiers=MappingProxyType({"op": ModifierLayout("op", ATOMIC_OP_VALUES)}),
    )


SAMPLE_LAYOUT = Layout(
    name="sample",
    control_lsb=107,
    control_fields=CONTROL_LAYOUT,
    instructions=(
        InstructionLayout(
            name="IADD3",
            opcode=0x11,
            opcode_lsb=0,
            opcode_width=8,
            fields=MappingProxyType(
                {
                    "guard_pred": FieldLayout(8, 3),
                    "guard_neg": FieldLayout(11, 1),
                    "rd": FieldLayout(12, 8),
                    "src_a": FieldLayout(20, 8),
                    "src_b": FieldLayout(28, 8),
                    "src_c": FieldLayout(36, 20),
                    "sat": FieldLayout(56, 1),
                }
            ),
            reserved=(ReservedLayout("iadd3_reserved", 57, 50),),
            modifiers=MappingProxyType({"sat": ModifierLayout("sat", MappingProxyType({"none": 0, "sat": 1}))}),
        ),
        InstructionLayout(
            name="ISETP",
            opcode=0x23,
            opcode_lsb=0,
            opcode_width=8,
            fields=MappingProxyType(
                {
                    "guard_pred": FieldLayout(8, 3),
                    "guard_neg": FieldLayout(11, 1),
                    "pd": FieldLayout(12, 3),
                    "src_a": FieldLayout(20, 8),
                    "src_b": FieldLayout(28, 8),
                    "cmp": FieldLayout(36, 3),
                }
            ),
            reserved=(ReservedLayout("isetp_reserved_low", 15, 5), ReservedLayout("isetp_reserved_high", 39, 68)),
            modifiers=MappingProxyType(
                {
                    "cmp": ModifierLayout(
                        "cmp",
                        MappingProxyType({"eq": 0, "ne": 1, "lt": 2, "le": 3, "gt": 4, "ge": 5}),
                    )
                }
            ),
        ),
        InstructionLayout(
            name="LOP3",
            opcode=0x34,
            opcode_lsb=0,
            opcode_width=8,
            fields=MappingProxyType(
                {
                    "guard_pred": FieldLayout(8, 3),
                    "guard_neg": FieldLayout(11, 1),
                    "rd": FieldLayout(12, 8),
                    "src_a": FieldLayout(20, 8),
                    "src_b": FieldLayout(28, 8),
                    "src_c": FieldLayout(36, 8),
                    "lut": FieldLayout(44, 8),
                }
            ),
            reserved=(ReservedLayout("lop3_reserved", 52, 55),),
        ),
        InstructionLayout(
            name="BRA",
            opcode=0x45,
            opcode_lsb=0,
            opcode_width=8,
            fields=MappingProxyType(
                {
                    "guard_pred": FieldLayout(8, 3),
                    "guard_neg": FieldLayout(11, 1),
                    "target": FieldLayout(12, 24),
                }
            ),
            reserved=(ReservedLayout("bra_reserved", 36, 71),),
        ),
        InstructionLayout(
            name="S2R",
            opcode=0x49,
            opcode_lsb=0,
            opcode_width=8,
            fields=MappingProxyType(
                {
                    "guard_pred": FieldLayout(8, 3),
                    "guard_neg": FieldLayout(11, 1),
                    "rd": FieldLayout(12, 8),
                    "sr": FieldLayout(20, 8),
                }
            ),
            reserved=(ReservedLayout("s2r_reserved", 28, 79),),
        ),
        InstructionLayout(
            name="BSSY",
            opcode=0x41,
            opcode_lsb=0,
            opcode_width=8,
            fields=MappingProxyType(
                {
                    "guard_pred": FieldLayout(8, 3),
                    "guard_neg": FieldLayout(11, 1),
                    "bar": FieldLayout(12, 4),
                    "target": FieldLayout(16, 24),
                }
            ),
            reserved=(ReservedLayout("bssy_reserved", 40, 67),),
        ),
        InstructionLayout(
            name="BSYNC",
            opcode=0x42,
            opcode_lsb=0,
            opcode_width=8,
            fields=MappingProxyType(
                {
                    "guard_pred": FieldLayout(8, 3),
                    "guard_neg": FieldLayout(11, 1),
                    "bar": FieldLayout(12, 4),
                }
            ),
            reserved=(ReservedLayout("bsync_reserved", 16, 91),),
        ),
        InstructionLayout(
            name="BREAK",
            opcode=0x46,
            opcode_lsb=0,
            opcode_width=8,
            fields=MappingProxyType(
                {
                    "guard_pred": FieldLayout(8, 3),
                    "guard_neg": FieldLayout(11, 1),
                    "bar": FieldLayout(12, 4),
                }
            ),
            reserved=(ReservedLayout("break_reserved", 16, 91),),
        ),
        InstructionLayout(
            name="YIELD",
            opcode=0x48,
            opcode_lsb=0,
            opcode_width=8,
            fields=MappingProxyType(
                {
                    "guard_pred": FieldLayout(8, 3),
                    "guard_neg": FieldLayout(11, 1),
                }
            ),
            reserved=(ReservedLayout("yield_reserved", 12, 95),),
        ),
        InstructionLayout(
            name="ELECT",
            opcode=0x4A,
            opcode_lsb=0,
            opcode_width=8,
            fields=MappingProxyType(
                {
                    "guard_pred": FieldLayout(8, 3),
                    "guard_neg": FieldLayout(11, 1),
                    "pd": FieldLayout(12, 3),
                    "membermask": FieldLayout(15, 32),
                }
            ),
            reserved=(ReservedLayout("elect_reserved", 47, 60),),
        ),
        InstructionLayout(
            name="VOTE",
            opcode=0x4B,
            opcode_lsb=0,
            opcode_width=8,
            fields=MappingProxyType(
                {
                    "guard_pred": FieldLayout(8, 3),
                    "guard_neg": FieldLayout(11, 1),
                    "pd": FieldLayout(12, 3),
                    "src": FieldLayout(15, 3),
                    "rd": FieldLayout(18, 8),
                    "membermask": FieldLayout(26, 32),
                    "mode": FieldLayout(58, 2),
                }
            ),
            reserved=(ReservedLayout("vote_reserved", 60, 47),),
            modifiers=MappingProxyType(
                {
                    "mode": ModifierLayout(
                        "mode",
                        MappingProxyType({"any": 0, "all": 1, "eq": 2, "ballot": 3}),
                    )
                }
            ),
        ),
        InstructionLayout(
            name="EXIT",
            opcode=0x7F,
            opcode_lsb=0,
            opcode_width=8,
            fields=MappingProxyType(
                {
                    "guard_pred": FieldLayout(8, 3),
                    "guard_neg": FieldLayout(11, 1),
                }
            ),
            reserved=(ReservedLayout("exit_reserved", 12, 95),),
        ),
        _memory_layout("LDG", 0x50, "rd"),
        _memory_layout("STG", 0x51, "src"),
        _memory_layout("LDS", 0x52, "rd"),
        _memory_layout("STS", 0x53, "src"),
        _memory_layout("LDL", 0x54, "rd"),
        _memory_layout("STL", 0x55, "src"),
        _memory_layout("LD", 0x56, "rd"),
        _memory_layout("ST", 0x57, "src"),
        InstructionLayout(
            name="LDC",
            opcode=0x58,
            opcode_lsb=0,
            opcode_width=8,
            fields=MappingProxyType(
                {
                    "guard_pred": FieldLayout(8, 3),
                    "guard_neg": FieldLayout(11, 1),
                    "rd": FieldLayout(12, 8),
                    "bank": FieldLayout(20, 8),
                    "addr_base": FieldLayout(28, 8),
                    "addr_ur": FieldLayout(36, 8),
                    "addr_imm": FieldLayout(44, 20),
                    "width": FieldLayout(64, 3),
                }
            ),
            reserved=(ReservedLayout("ldc_reserved", 67, 40),),
            modifiers=MappingProxyType({"width": ModifierLayout("width", WIDTH_VALUES)}),
        ),
        _atomic_layout("ATOM", 0x60, True),
        _atomic_layout("ATOMG", 0x61, True),
        _atomic_layout("ATOMS", 0x62, True),
        _atomic_layout("RED", 0x63, False),
        _atomic_layout("REDG", 0x64, False),
        _atomic_layout("REDS", 0x65, False),
        InstructionLayout(
            name="BAR",
            opcode=0x66,
            opcode_lsb=0,
            opcode_width=8,
            fields=MappingProxyType(
                {
                    "guard_pred": FieldLayout(8, 3),
                    "guard_neg": FieldLayout(11, 1),
                    "bar": FieldLayout(12, 4),
                    "count": FieldLayout(16, 16),
                    "mode": FieldLayout(32, 1),
                }
            ),
            reserved=(ReservedLayout("bar_reserved", 33, 74),),
            modifiers=MappingProxyType({"mode": ModifierLayout("mode", BAR_MODE_VALUES)}),
        ),
        InstructionLayout(
            name="MEMBAR",
            opcode=0x67,
            opcode_lsb=0,
            opcode_width=8,
            fields=MappingProxyType(
                {
                    "guard_pred": FieldLayout(8, 3),
                    "guard_neg": FieldLayout(11, 1),
                    "scope": FieldLayout(12, 2),
                    "order": FieldLayout(14, 2),
                }
            ),
            reserved=(ReservedLayout("membar_reserved", 16, 91),),
            modifiers=MappingProxyType(
                {"scope": ModifierLayout("scope", MEM_SCOPE_VALUES), "order": ModifierLayout("order", MEM_ORDER_VALUES)}
            ),
        ),
        InstructionLayout(
            name="FENCE",
            opcode=0x68,
            opcode_lsb=0,
            opcode_width=8,
            fields=MappingProxyType(
                {
                    "guard_pred": FieldLayout(8, 3),
                    "guard_neg": FieldLayout(11, 1),
                    "scope": FieldLayout(12, 2),
                    "order": FieldLayout(14, 2),
                }
            ),
            reserved=(ReservedLayout("fence_reserved", 16, 91),),
            modifiers=MappingProxyType(
                {"scope": ModifierLayout("scope", MEM_SCOPE_VALUES), "order": ModifierLayout("order", MEM_ORDER_VALUES)}
            ),
        ),
        InstructionLayout(
            name="CVTA",
            opcode=0x69,
            opcode_lsb=0,
            opcode_width=8,
            fields=MappingProxyType(
                {
                    "guard_pred": FieldLayout(8, 3),
                    "guard_neg": FieldLayout(11, 1),
                    "rd": FieldLayout(12, 8),
                    "src": FieldLayout(20, 8),
                    "direction": FieldLayout(28, 3),
                }
            ),
            reserved=(ReservedLayout("cvta_reserved", 31, 76),),
            modifiers=MappingProxyType({"direction": ModifierLayout("direction", CVTA_DIRECTION_VALUES)}),
        ),
    ),
)


INSTRUCTION_LAYOUT_BY_NAME = MappingProxyType(
    {instruction.name: instruction for instruction in SAMPLE_LAYOUT.instructions}
)
