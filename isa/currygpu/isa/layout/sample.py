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
    ),
)


INSTRUCTION_LAYOUT_BY_NAME = MappingProxyType(
    {instruction.name: instruction for instruction in SAMPLE_LAYOUT.instructions}
)
