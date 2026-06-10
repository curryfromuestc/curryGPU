"""Public instruction schema for the sample curryGPU ISA subset."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping, Sequence


WORD_BITS = 128
CONTROL_BITS = 21
CONTROL_FIELDS = MappingProxyType(
    {
        "stall": 4,
        "yield": 1,
        "read_barrier": 3,
        "write_barrier": 3,
        "wait_mask": 6,
        "reuse": 4,
    }
)


@dataclass(frozen=True)
class FieldSchema:
    name: str
    width: int
    kind: str
    signed: bool = False
    required: bool = True
    default: int | str | None = None
    choices: tuple[str, ...] = ()


@dataclass(frozen=True)
class OperandSchema:
    name: str
    kind: str
    field: str | None = None
    fields: tuple[str, ...] = ()
    required: bool = True
    default: int | str | None = None
    constraints: Mapping[str, int | bool] = MappingProxyType({})
    choices: tuple[str, ...] = ()


@dataclass(frozen=True)
class ModifierSchema:
    name: str
    field: str
    choices: tuple[str, ...]
    default: str


@dataclass(frozen=True)
class InstructionSchema:
    name: str
    fields: tuple[FieldSchema, ...]
    operands: tuple[OperandSchema, ...]
    modifiers: tuple[ModifierSchema, ...] = ()
    semantics: str = ""


@dataclass(frozen=True)
class AliasSchema:
    name: str
    target: str
    operand_map: Mapping[str, str]
    fixed_operands: Mapping[str, int | str]
    fixed_modifiers: Mapping[str, str]


def field_map(instruction: InstructionSchema) -> Mapping[str, FieldSchema]:
    return MappingProxyType({field.name: field for field in instruction.fields})


def operand_map(instruction: InstructionSchema) -> Mapping[str, OperandSchema]:
    return MappingProxyType({operand.name: operand for operand in instruction.operands})


def modifier_map(instruction: InstructionSchema) -> Mapping[str, ModifierSchema]:
    return MappingProxyType({modifier.name: modifier for modifier in instruction.modifiers})


def _base_fields(extra: Sequence[FieldSchema]) -> tuple[FieldSchema, ...]:
    return (
        FieldSchema("guard_pred", 3, "predicate", default="PT"),
        FieldSchema("guard_neg", 1, "bool", default=0),
        *extra,
    )


INSTRUCTIONS = (
    InstructionSchema(
        name="IADD3",
        fields=_base_fields(
            (
                FieldSchema("rd", 8, "register"),
                FieldSchema("src_a", 8, "register"),
                FieldSchema("src_b", 8, "register"),
                FieldSchema("src_c", 20, "immediate", signed=True),
                FieldSchema("sat", 1, "modifier", default=0),
            )
        ),
        operands=(
            OperandSchema("rd", "register", "rd"),
            OperandSchema("src_a", "register", "src_a"),
            OperandSchema("src_b", "register", "src_b"),
            OperandSchema("src_c", "immediate", "src_c", default=0),
        ),
        modifiers=(
            ModifierSchema("sat", "sat", ("none", "sat"), "none"),
        ),
        semantics="iadd3",
    ),
    InstructionSchema(
        name="ISETP",
        fields=_base_fields(
            (
                FieldSchema("pd", 3, "predicate"),
                FieldSchema("src_a", 8, "register"),
                FieldSchema("src_b", 8, "register"),
                FieldSchema("cmp", 3, "modifier", default=0),
            )
        ),
        operands=(
            OperandSchema("pd", "predicate", "pd"),
            OperandSchema("src_a", "register", "src_a"),
            OperandSchema("src_b", "register", "src_b"),
        ),
        modifiers=(
            ModifierSchema(
                "cmp",
                "cmp",
                ("eq", "ne", "lt", "le", "gt", "ge"),
                "eq",
            ),
        ),
        semantics="isetp",
    ),
    InstructionSchema(
        name="LOP3",
        fields=_base_fields(
            (
                FieldSchema("rd", 8, "register"),
                FieldSchema("src_a", 8, "register"),
                FieldSchema("src_b", 8, "register"),
                FieldSchema("src_c", 8, "register"),
                FieldSchema("lut", 8, "immediate"),
            )
        ),
        operands=(
            OperandSchema("rd", "register", "rd"),
            OperandSchema("src_a", "register", "src_a"),
            OperandSchema("src_b", "register", "src_b"),
            OperandSchema("src_c", "register", "src_c"),
            OperandSchema("lut", "immediate", "lut"),
        ),
        semantics="lop3",
    ),
    InstructionSchema(
        name="BRA",
        fields=_base_fields(
            (
                FieldSchema("target", 24, "immediate", signed=True),
            )
        ),
        operands=(
            OperandSchema(
                "target",
                "immediate",
                "target",
                constraints=MappingProxyType({"aligned": 16}),
            ),
        ),
        semantics="bra_uniform",
    ),
    InstructionSchema(
        name="S2R",
        fields=_base_fields(
            (
                FieldSchema("rd", 8, "register"),
                FieldSchema("sr", 8, "sreg", choices=("SR_LANEID",)),
            )
        ),
        operands=(
            OperandSchema("rd", "register", "rd"),
            OperandSchema("sr", "sreg", "sr", choices=("SR_LANEID",)),
        ),
        semantics="s2r",
    ),
    InstructionSchema(
        name="BSSY",
        fields=_base_fields(
            (
                FieldSchema("bar", 4, "barrier"),
                FieldSchema("target", 24, "immediate", signed=True),
            )
        ),
        operands=(
            OperandSchema("bar", "barrier", "bar"),
            OperandSchema(
                "target",
                "immediate",
                "target",
                constraints=MappingProxyType({"aligned": 16}),
            ),
        ),
        semantics="bssy",
    ),
    InstructionSchema(
        name="BSYNC",
        fields=_base_fields(
            (
                FieldSchema("bar", 4, "barrier"),
            )
        ),
        operands=(OperandSchema("bar", "barrier", "bar"),),
        semantics="bsync",
    ),
    InstructionSchema(
        name="BREAK",
        fields=_base_fields(
            (
                FieldSchema("bar", 4, "barrier"),
            )
        ),
        operands=(OperandSchema("bar", "barrier", "bar"),),
        semantics="break",
    ),
    InstructionSchema(
        name="YIELD",
        fields=_base_fields(()),
        operands=(),
        semantics="yield",
    ),
    InstructionSchema(
        name="ELECT",
        fields=_base_fields(
            (
                FieldSchema("pd", 3, "predicate"),
                FieldSchema("membermask", 32, "membermask"),
            )
        ),
        operands=(
            OperandSchema("pd", "predicate", "pd"),
            OperandSchema("membermask", "membermask", "membermask"),
        ),
        semantics="elect",
    ),
    InstructionSchema(
        name="VOTE",
        fields=_base_fields(
            (
                FieldSchema("pd", 3, "predicate"),
                FieldSchema("src", 3, "predicate"),
                FieldSchema("rd", 8, "register", default="RZ"),
                FieldSchema("membermask", 32, "membermask"),
                FieldSchema("mode", 2, "modifier", default=0),
            )
        ),
        operands=(
            OperandSchema("pd", "predicate", "pd"),
            OperandSchema("src", "predicate", "src"),
            OperandSchema("membermask", "membermask", "membermask"),
            OperandSchema("rd", "register", "rd", required=False, default="RZ"),
        ),
        modifiers=(
            ModifierSchema("mode", "mode", ("any", "all", "eq", "ballot"), "any"),
        ),
        semantics="vote",
    ),
    InstructionSchema(
        name="EXIT",
        fields=_base_fields(()),
        operands=(),
        semantics="exit",
    ),
)

ALIASES = (
    AliasSchema(
        name="MOV",
        target="IADD3",
        operand_map=MappingProxyType({"rd": "rd", "src": "src_a"}),
        fixed_operands=MappingProxyType({"src_b": "RZ", "src_c": 0}),
        fixed_modifiers=MappingProxyType({"sat": "none"}),
    ),
)

INSTRUCTION_BY_NAME = MappingProxyType({instruction.name: instruction for instruction in INSTRUCTIONS})
ALIAS_BY_NAME = MappingProxyType({alias.name: alias for alias in ALIASES})
