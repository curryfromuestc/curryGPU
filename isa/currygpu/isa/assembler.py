"""Assembler entry points for the declarative curryGPU ISA subset."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from . import codegen, schema
from .layout.sample import InstructionLayout, Layout, SAMPLE_LAYOUT


class AssembleError(ValueError):
    """Raised when symbolic instruction input cannot be encoded."""


@dataclass(frozen=True)
class EncodedInstruction:
    word: int
    ir: Mapping[str, Any]


@dataclass(frozen=True)
class Control:
    stall: int = 0
    yield_: int = 0
    read_barrier: int = 0
    write_barrier: int = 0
    wait_mask: int = 0
    reuse: int = 0

    def as_fields(self) -> Mapping[str, int]:
        return {
            "stall": self.stall,
            "yield": self.yield_,
            "read_barrier": self.read_barrier,
            "write_barrier": self.write_barrier,
            "wait_mask": self.wait_mask,
            "reuse": self.reuse,
        }


@dataclass
class _AssembledInput:
    instruction: schema.InstructionSchema
    operands: dict[str, Any] = field(default_factory=dict)
    modifiers: dict[str, str] = field(default_factory=dict)


def emit(mnemonic: str, *operands: Any, layout: Layout = SAMPLE_LAYOUT, **kwargs: Any) -> int:
    return encode(mnemonic, *operands, layout=layout, **kwargs).word


def encode(mnemonic: str, *operands: Any, layout: Layout = SAMPLE_LAYOUT, **kwargs: Any) -> EncodedInstruction:
    codegen.validate_layout(layout)
    assembled = _normalize_input(mnemonic, operands, kwargs)
    inst_layout = {entry.name: entry for entry in layout.instructions}[assembled.instruction.name]
    control = _extract_control(kwargs)
    fields = _encode_fields(assembled, control, inst_layout)
    word = _write_match_bits(inst_layout)
    for field_name, value in fields.items():
        field_layout = inst_layout.fields[field_name]
        word = _insert_bits(word, field_layout.lsb, field_layout.width, _to_unsigned(value, field_layout.width))
    for reserved in inst_layout.reserved:
        word = _insert_bits(word, reserved.lsb, reserved.width, reserved.value)
    for name, value in control.as_fields().items():
        control_layout = layout.control_fields[name]
        word = _insert_bits(word, control_layout.lsb, control_layout.width, value)
    return EncodedInstruction(word=word, ir=decode_like_ir(word, layout=layout))


def decode_like_ir(word: int, layout: Layout = SAMPLE_LAYOUT) -> Mapping[str, Any]:
    codegen.validate_layout(layout)
    if word < 0 or word >= (1 << schema.WORD_BITS):
        raise AssembleError("encoded word must fit 128 bits")
    candidates = []
    for entry in sorted(layout.instructions, key=lambda item: item.name):
        mask = ((1 << entry.opcode_width) - 1) << entry.opcode_lsb
        if (word & mask) == (entry.opcode << entry.opcode_lsb):
            candidates.append(entry)
    if not candidates:
        raise AssembleError("encoded word does not match a known opcode")
    if len(candidates) > 1:
        names = ", ".join(entry.name for entry in candidates)
        raise AssembleError(f"encoded word matches multiple instructions: {names}")
    inst_layout = candidates[0]
    instruction = schema.INSTRUCTION_BY_NAME[inst_layout.name]
    _validate_reserved_bits(word, inst_layout)
    field_values = {
        field.name: _decode_field(word, inst_layout.fields[field.name], field.signed)
        for field in sorted(instruction.fields, key=lambda item: item.name)
    }
    operands = {}
    fields_by_name = schema.field_map(instruction)
    for operand in sorted(instruction.operands, key=lambda item: item.name):
        value = field_values[operand.field] if operand.field else None
        operands[operand.name] = _symbolize_operand(value, operand.kind, fields_by_name[operand.field])
    modifiers = {}
    for modifier in sorted(instruction.modifiers, key=lambda item: item.name):
        raw = field_values[modifier.field]
        modifiers[modifier.name] = _modifier_name(modifier, inst_layout.modifiers[modifier.name], raw)
    return {
        "name": instruction.name,
        "operands": operands,
        "modifiers": modifiers,
        "guard": {
            "predicate": _predicate_name(field_values["guard_pred"]),
            "negated": bool(field_values["guard_neg"]),
        },
        "control": {
            name: _extract_bits(word, field_layout.lsb, field_layout.width)
            for name, field_layout in sorted(layout.control_fields.items())
        },
    }


def roundtrip_ir(mnemonic: str, *operands: Any, layout: Layout = SAMPLE_LAYOUT, **kwargs: Any) -> Mapping[str, Any]:
    encoded = encode(mnemonic, *operands, layout=layout, **kwargs)
    return decode_like_ir(encoded.word, layout=layout)


def _normalize_input(mnemonic: str, operands: tuple[Any, ...], kwargs: Mapping[str, Any]) -> _AssembledInput:
    name = mnemonic.upper()
    if name in schema.ALIAS_BY_NAME:
        return _normalize_alias(schema.ALIAS_BY_NAME[name], operands, kwargs)
    if name not in schema.INSTRUCTION_BY_NAME:
        raise AssembleError(f"unknown instruction {mnemonic}")
    instruction = schema.INSTRUCTION_BY_NAME[name]
    _reject_unknown_kwargs(name, instruction, kwargs)
    inst_operands = {operand.name: None for operand in instruction.operands}
    if len(operands) > len(instruction.operands):
        raise AssembleError(f"too many operands for {name}")
    for operand_schema, value in zip(instruction.operands, operands):
        inst_operands[operand_schema.name] = value
    for operand_schema in instruction.operands:
        if operand_schema.name in kwargs:
            if inst_operands[operand_schema.name] is not None:
                raise AssembleError(f"operand {operand_schema.name} supplied twice")
            inst_operands[operand_schema.name] = kwargs[operand_schema.name]
        if inst_operands[operand_schema.name] is None:
            if operand_schema.default is not None:
                inst_operands[operand_schema.name] = operand_schema.default
            elif operand_schema.required:
                raise AssembleError(f"missing operand {operand_schema.name}")
    modifiers = {}
    for modifier in instruction.modifiers:
        value = kwargs.get(modifier.name, modifier.default)
        if not isinstance(value, str):
            raise AssembleError(f"modifier {modifier.name} must be symbolic")
        modifiers[modifier.name] = value
    inst_operands["guard"] = kwargs.get("guard", "PT")
    inst_operands["guard_neg"] = kwargs.get("guard_neg", False)
    return _AssembledInput(instruction=instruction, operands=inst_operands, modifiers=modifiers)


def _normalize_alias(alias: schema.AliasSchema, operands: tuple[Any, ...], kwargs: Mapping[str, Any]) -> _AssembledInput:
    target = schema.INSTRUCTION_BY_NAME[alias.target]
    _reject_unknown_alias_kwargs(alias.name, alias, kwargs)
    alias_operand_names = list(alias.operand_map)
    if len(operands) > len(alias_operand_names):
        raise AssembleError(f"too many operands for {alias.name}")
    mapped_kwargs: dict[str, Any] = {}
    for alias_name, value in zip(alias_operand_names, operands):
        mapped_kwargs[alias.operand_map[alias_name]] = value
    for alias_name, target_name in alias.operand_map.items():
        if alias_name in kwargs:
            if target_name in mapped_kwargs:
                raise AssembleError(f"operand {alias_name} supplied twice")
            mapped_kwargs[target_name] = kwargs[alias_name]
    for common_name in ("guard", "guard_neg"):
        if common_name in kwargs:
            mapped_kwargs[common_name] = kwargs[common_name]
    for key in _control_kwarg_names():
        if key in kwargs:
            mapped_kwargs[key] = kwargs[key]
    mapped_kwargs.update(alias.fixed_operands)
    mapped_kwargs.update(alias.fixed_modifiers)
    return _normalize_input(target.name, (), mapped_kwargs)


def _extract_control(kwargs: Mapping[str, Any]) -> Control:
    fields = {}
    aliases = {"yield": "yield_"}
    for name, width in schema.CONTROL_FIELDS.items():
        key = aliases.get(name, name)
        value = kwargs.get(name, kwargs.get(key, 0))
        if not isinstance(value, int):
            raise AssembleError(f"control field {name} must be an integer")
        if value < 0 or value >= (1 << width):
            raise AssembleError(f"control field {name} does not fit {width} bits")
        fields[key] = value
    return Control(**fields)


def _encode_fields(assembled: _AssembledInput, control: Control, layout: InstructionLayout) -> Mapping[str, int]:
    del control
    instruction = assembled.instruction
    field_values: dict[str, int] = {}
    fields_by_name = schema.field_map(instruction)
    field_values["guard_pred"] = _parse_predicate(assembled.operands.pop("guard", "PT"))
    field_values["guard_neg"] = _parse_bool(assembled.operands.pop("guard_neg", False))
    for operand in instruction.operands:
        field = fields_by_name[operand.field]
        value = _parse_operand(assembled.operands[operand.name], operand, field)
        field_values[operand.field] = value
    for modifier in instruction.modifiers:
        modifier_values = layout.modifiers[modifier.name].values
        if assembled.modifiers[modifier.name] not in modifier_values:
            raise AssembleError(f"invalid modifier {modifier.name}={assembled.modifiers[modifier.name]}")
        field_values[modifier.field] = modifier_values[assembled.modifiers[modifier.name]]
    for field in instruction.fields:
        if field.name not in field_values:
            if field.default is None:
                raise AssembleError(f"missing field {field.name}")
            field_values[field.name] = _parse_field_default(field.default, field)
        _validate_integer_range(instruction.name, field.name, field_values[field.name], field)
    return field_values


def _parse_operand(value: Any, operand: schema.OperandSchema, field: schema.FieldSchema) -> int:
    if operand.kind == "register":
        return _parse_register(value)
    if operand.kind == "predicate":
        return _parse_predicate(value)
    if operand.kind == "immediate":
        parsed = _parse_immediate(value)
        aligned = operand.constraints.get("aligned")
        if isinstance(aligned, int) and parsed % aligned != 0:
            raise AssembleError(f"operand {operand.name} must be aligned to {aligned} bytes")
        _validate_integer_range("operand", operand.name, parsed, field)
        return parsed
    raise AssembleError(f"unsupported operand kind {operand.kind}")


def _parse_field_default(value: int | str, field: schema.FieldSchema) -> int:
    if field.kind == "register":
        return _parse_register(value)
    if field.kind == "predicate":
        return _parse_predicate(value)
    if field.kind == "bool":
        return _parse_bool(value)
    return _parse_immediate(value)


def _parse_register(value: Any) -> int:
    if isinstance(value, int):
        if 0 <= value <= 254:
            return value
        raise AssembleError("register index out of range")
    if not isinstance(value, str):
        raise AssembleError("register must be an integer or symbolic name")
    name = value.upper()
    if name == "RZ":
        return 255
    if name.startswith("R") and name[1:].isdigit():
        parsed = int(name[1:])
        if 0 <= parsed <= 254:
            return parsed
    raise AssembleError(f"invalid register {value}")


def _parse_predicate(value: Any) -> int:
    if isinstance(value, int):
        if 0 <= value <= 6:
            return value
        raise AssembleError("predicate index out of range")
    if not isinstance(value, str):
        raise AssembleError("predicate must be an integer or symbolic name")
    name = value.upper()
    if name == "PT":
        return 7
    if name.startswith("P") and name[1:].isdigit():
        parsed = int(name[1:])
        if 0 <= parsed <= 6:
            return parsed
    raise AssembleError(f"invalid predicate {value}")


def _parse_bool(value: Any) -> int:
    if isinstance(value, bool):
        return int(value)
    if value in (0, 1):
        return int(value)
    raise AssembleError("boolean field must be true or false")


def _parse_immediate(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise AssembleError("immediate must be an integer")
    return value


def _validate_integer_range(owner: str, name: str, value: int, field: schema.FieldSchema) -> None:
    if field.signed:
        lo = -(1 << (field.width - 1))
        hi = (1 << (field.width - 1)) - 1
    else:
        lo = 0
        hi = (1 << field.width) - 1
    if value < lo or value > hi:
        raise AssembleError(f"{owner}.{name} value {value} outside range [{lo}, {hi}]")


def _to_unsigned(value: int, width: int) -> int:
    return value & ((1 << width) - 1)


def _write_match_bits(layout: InstructionLayout) -> int:
    return layout.opcode << layout.opcode_lsb


def _insert_bits(word: int, lsb: int, width: int, value: int) -> int:
    mask = ((1 << width) - 1) << lsb
    return (word & ~mask) | ((value << lsb) & mask)


def _extract_bits(word: int, lsb: int, width: int) -> int:
    return (word >> lsb) & ((1 << width) - 1)


def _decode_field(word: int, layout, signed: bool) -> int:
    value = _extract_bits(word, layout.lsb, layout.width)
    if signed and value & (1 << (layout.width - 1)):
        return value - (1 << layout.width)
    return value


def _validate_reserved_bits(word: int, layout: InstructionLayout) -> None:
    for reserved in layout.reserved:
        value = _extract_bits(word, reserved.lsb, reserved.width)
        if value != reserved.value:
            raise AssembleError(f"reserved field {reserved.name} has invalid value")


def _symbolize_operand(value: int, kind: str, field: schema.FieldSchema) -> int | str:
    if kind == "register":
        return _register_name(value)
    if kind == "predicate":
        return _predicate_name(value)
    if field.signed:
        return value
    return value


def _register_name(value: int) -> str:
    if value == 255:
        return "RZ"
    if 0 <= value <= 254:
        return f"R{value}"
    raise AssembleError("decoded register index is invalid")


def _predicate_name(value: int) -> str:
    if value == 7:
        return "PT"
    if 0 <= value <= 6:
        return f"P{value}"
    raise AssembleError("decoded predicate index is invalid")


def _modifier_name(modifier: schema.ModifierSchema, modifier_layout, raw: int) -> str:
    for name, value in sorted(modifier_layout.values.items()):
        if value == raw:
            return name
    raise AssembleError(f"decoded modifier {modifier.name} has invalid value")


def _control_kwarg_names() -> set[str]:
    return set(schema.CONTROL_FIELDS) | {"yield_"}


def _reject_unknown_kwargs(name: str, instruction: schema.InstructionSchema, kwargs: Mapping[str, Any]) -> None:
    allowed = {operand.name for operand in instruction.operands}
    allowed.update(modifier.name for modifier in instruction.modifiers)
    allowed.update({"guard", "guard_neg"})
    allowed.update(_control_kwarg_names())
    unknown = sorted(set(kwargs) - allowed)
    if unknown:
        raise AssembleError(f"unknown argument for {name}: {unknown[0]}")


def _reject_unknown_alias_kwargs(name: str, alias: schema.AliasSchema, kwargs: Mapping[str, Any]) -> None:
    allowed = set(alias.operand_map)
    allowed.update({"guard", "guard_neg"})
    allowed.update(_control_kwarg_names())
    unknown = sorted(set(kwargs) - allowed)
    if unknown:
        raise AssembleError(f"unknown argument for {name}: {unknown[0]}")
