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


SREG_CHOICES = (
    "SR_LANEID",
    "SR_TID.X",
    "SR_TID.Y",
    "SR_TID.Z",
    "SR_NTID.X",
    "SR_NTID.Y",
    "SR_NTID.Z",
    "SR_CTAID.X",
    "SR_CTAID.Y",
    "SR_CTAID.Z",
    "SR_NCTAID.X",
    "SR_NCTAID.Y",
    "SR_NCTAID.Z",
    "SR_WARPID",
    "SR_NWARPID",
)

MEM_WIDTH_CHOICES = ("u8", "s8", "u16", "s16", "32", "64", "128", "256")
ATOMIC_OP_CHOICES = ("add", "min", "max", "inc", "dec", "and", "or", "xor", "exch", "cas")
BAR_MODE_CHOICES = ("sync", "arv")
MEM_SCOPE_CHOICES = ("cta", "sm", "gpu", "sys")
MEM_ORDER_CHOICES = ("sc", "acquire", "release", "acq_rel")
CVTA_DIRECTION_CHOICES = ("to_global", "to_shared", "to_local", "from_global", "from_shared", "from_local")


def _base_fields(extra: Sequence[FieldSchema]) -> tuple[FieldSchema, ...]:
    return (
        FieldSchema("guard_pred", 3, "predicate", default="PT"),
        FieldSchema("guard_neg", 1, "bool", default=0),
        *extra,
    )


def _address_fields() -> tuple[FieldSchema, ...]:
    return (
        FieldSchema("addr_base", 8, "register"),
        FieldSchema("addr_ur", 8, "uniform_register", default="URZ"),
        FieldSchema("addr_imm", 20, "immediate", signed=True, default=0),
    )


def _address_operand(*, address_bits: int) -> OperandSchema:
    return OperandSchema(
        "addr",
        "address",
        fields=("addr_base", "addr_ur", "addr_imm"),
        constraints=MappingProxyType({"address_bits": address_bits}),
    )


def _width_modifier() -> ModifierSchema:
    return ModifierSchema("width", "width", MEM_WIDTH_CHOICES, "32")


def _memory_load(name: str, semantics: str, *, address_bits: int = 32) -> InstructionSchema:
    return InstructionSchema(
        name=name,
        fields=_base_fields((FieldSchema("rd", 8, "register"), *_address_fields(), FieldSchema("width", 3, "modifier", default=4))),
        operands=(OperandSchema("rd", "register", "rd"), _address_operand(address_bits=address_bits)),
        modifiers=(_width_modifier(),),
        semantics=semantics,
    )


def _memory_store(name: str, semantics: str, *, address_bits: int = 32) -> InstructionSchema:
    return InstructionSchema(
        name=name,
        fields=_base_fields((FieldSchema("src", 8, "register"), *_address_fields(), FieldSchema("width", 3, "modifier", default=4))),
        operands=(OperandSchema("src", "register", "src"), _address_operand(address_bits=address_bits)),
        modifiers=(_width_modifier(),),
        semantics=semantics,
    )


def _atomic_instruction(name: str, semantics: str) -> InstructionSchema:
    return InstructionSchema(
        name=name,
        fields=_base_fields(
            (
                FieldSchema("rd", 8, "register", default="RZ"),
                FieldSchema("src", 8, "register"),
                FieldSchema("cmp", 8, "register", default="RZ"),
                *_address_fields(),
                FieldSchema("op", 4, "modifier", default=0),
            )
        ),
        operands=(
            OperandSchema("rd", "register", "rd", required=False, default="RZ"),
            _address_operand(address_bits=64 if name in {"ATOM", "ATOMG"} else 32),
            OperandSchema("src", "register", "src"),
            OperandSchema("cmp", "register", "cmp", required=False, default="RZ"),
        ),
        modifiers=(ModifierSchema("op", "op", ATOMIC_OP_CHOICES, "add"),),
        semantics=semantics,
    )


def _red_instruction(name: str, semantics: str) -> InstructionSchema:
    return InstructionSchema(
        name=name,
        fields=_base_fields(
            (
                FieldSchema("src", 8, "register"),
                FieldSchema("cmp", 8, "register", default="RZ"),
                *_address_fields(),
                FieldSchema("op", 4, "modifier", default=0),
            )
        ),
        operands=(
            _address_operand(address_bits=64 if name in {"RED", "REDG"} else 32),
            OperandSchema("src", "register", "src"),
            OperandSchema("cmp", "register", "cmp", required=False, default="RZ"),
        ),
        modifiers=(ModifierSchema("op", "op", ATOMIC_OP_CHOICES, "add"),),
        semantics=semantics,
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
                FieldSchema("sr", 8, "sreg", choices=SREG_CHOICES),
            )
        ),
        operands=(
            OperandSchema("rd", "register", "rd"),
            OperandSchema("sr", "sreg", "sr", choices=SREG_CHOICES),
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
    _memory_load("LDG", "ld_global", address_bits=64),
    _memory_store("STG", "st_global", address_bits=64),
    _memory_load("LDS", "ld_shared"),
    _memory_store("STS", "st_shared"),
    _memory_load("LDL", "ld_local"),
    _memory_store("STL", "st_local"),
    _memory_load("LD", "ld_generic", address_bits=64),
    _memory_store("ST", "st_generic", address_bits=64),
    InstructionSchema(
        name="LDC",
        fields=_base_fields(
            (
                FieldSchema("rd", 8, "register"),
                FieldSchema("bank", 8, "immediate"),
                *_address_fields(),
                FieldSchema("width", 3, "modifier", default=4),
            )
        ),
        operands=(
            OperandSchema("rd", "register", "rd"),
            OperandSchema("bank", "immediate", "bank"),
            _address_operand(address_bits=32),
        ),
        modifiers=(_width_modifier(),),
        semantics="ld_const",
    ),
    _atomic_instruction("ATOM", "atomic_generic"),
    _atomic_instruction("ATOMG", "atomic_global"),
    _atomic_instruction("ATOMS", "atomic_shared"),
    _red_instruction("RED", "red_generic"),
    _red_instruction("REDG", "red_global"),
    _red_instruction("REDS", "red_shared"),
    InstructionSchema(
        name="BAR",
        fields=_base_fields(
            (
                FieldSchema("bar", 4, "barrier"),
                FieldSchema("count", 16, "barrier_count", default=0),
                FieldSchema("mode", 1, "modifier", default=0),
            )
        ),
        operands=(
            OperandSchema("bar", "barrier", "bar"),
            OperandSchema("count", "barrier_count", "count", required=False, default=0),
        ),
        modifiers=(ModifierSchema("mode", "mode", BAR_MODE_CHOICES, "sync"),),
        semantics="bar",
    ),
    InstructionSchema(
        name="MEMBAR",
        fields=_base_fields(
            (
                FieldSchema("scope", 2, "modifier", default=2),
                FieldSchema("order", 2, "modifier", default=0),
            )
        ),
        operands=(),
        modifiers=(
            ModifierSchema("scope", "scope", MEM_SCOPE_CHOICES, "gpu"),
            ModifierSchema("order", "order", MEM_ORDER_CHOICES, "sc"),
        ),
        semantics="membar",
    ),
    InstructionSchema(
        name="FENCE",
        fields=_base_fields(
            (
                FieldSchema("scope", 2, "modifier", default=2),
                FieldSchema("order", 2, "modifier", default=0),
            )
        ),
        operands=(),
        modifiers=(
            ModifierSchema("scope", "scope", MEM_SCOPE_CHOICES, "gpu"),
            ModifierSchema("order", "order", MEM_ORDER_CHOICES, "sc"),
        ),
        semantics="fence",
    ),
    InstructionSchema(
        name="CVTA",
        fields=_base_fields(
            (
                FieldSchema("rd", 8, "register"),
                FieldSchema("src", 8, "register"),
                FieldSchema("direction", 3, "modifier", default=0),
            )
        ),
        operands=(
            OperandSchema("rd", "register", "rd", constraints=MappingProxyType({"aligned": 2})),
            OperandSchema("src", "register", "src", constraints=MappingProxyType({"aligned": 2})),
        ),
        modifiers=(ModifierSchema("direction", "direction", CVTA_DIRECTION_CHOICES, "to_global"),),
        semantics="cvta",
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
