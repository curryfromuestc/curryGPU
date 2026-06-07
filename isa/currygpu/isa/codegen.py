"""Deterministic generation and validation for declarative ISA encodings."""

from __future__ import annotations

import json
import shutil
import sys
from argparse import ArgumentParser
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from . import schema
from .layout import (
    DEFAULT_LAYOUT,
    FixedBitLayout,
    InstructionLayout,
    Layout,
    LayoutSelectionError,
    ReservedLayout,
    SAMPLE_LAYOUT,
    load_layout,
)


class CodegenError(ValueError):
    """Raised when schema and layout cannot form a valid encoding contract."""


def resolve_layout(name: str | None = None) -> Layout:
    try:
        return load_layout(name)
    except LayoutSelectionError as exc:
        raise CodegenError(str(exc)) from exc


@dataclass(frozen=True)
class GeneratedArtifacts:
    ir_json: str
    python_emit: str
    cpp_decoder: str
    ir: Mapping[str, Any]
    decoder_metadata: Mapping[str, Any]


def generate_all(layout: Layout = SAMPLE_LAYOUT) -> GeneratedArtifacts:
    ir = build_ir(layout)
    decoder_metadata = build_decoder_metadata(layout)
    return GeneratedArtifacts(
        ir_json=generate_json_ir(ir),
        python_emit=generate_python_emit(decoder_metadata),
        cpp_decoder=generate_cpp_decoder(decoder_metadata),
        ir=ir,
        decoder_metadata=decoder_metadata,
    )


def write_generated(out_dir: Path, layout: Layout = SAMPLE_LAYOUT, *, clean: bool = True) -> GeneratedArtifacts:
    artifacts = generate_all(layout)
    if clean and out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    files = {
        "isa_ir.json": artifacts.ir_json,
        "emit.gen.py": artifacts.python_emit,
        "decode.gen.cpp": artifacts.cpp_decoder,
        "decoded_inst.gen.h": generate_cpp_header(artifacts.decoder_metadata),
    }
    for name, content in sorted(files.items()):
        (out_dir / name).write_text(content, encoding="utf-8", newline="\n")
    return artifacts


def build_ir(layout: Layout = SAMPLE_LAYOUT) -> Mapping[str, Any]:
    validate_layout(layout)
    instructions = []
    for instruction in sorted(schema.INSTRUCTIONS, key=lambda item: item.name):
        instructions.append(_public_instruction_ir(instruction))
    aliases = [_alias_ir(alias) for alias in sorted(schema.ALIASES, key=lambda item: item.name)]
    return {
        "schema_version": 1,
        "word_bits": schema.WORD_BITS,
        "control": _public_control_ir(),
        "instructions": instructions,
        "aliases": aliases,
    }


def build_decoder_metadata(layout: Layout = SAMPLE_LAYOUT) -> Mapping[str, Any]:
    validate_layout(layout)
    layout_by_name = {entry.name: entry for entry in layout.instructions}
    instructions = []
    for instruction in sorted(schema.INSTRUCTIONS, key=lambda item: item.name):
        inst_layout = layout_by_name[instruction.name]
        instructions.append(_decoder_instruction_ir(instruction, inst_layout))
    return {
        "schema_version": 1,
        "word_bits": schema.WORD_BITS,
        "layout": layout.name,
        "control": _decoder_control_ir(layout),
        "instructions": instructions,
        "aliases": [_alias_ir(alias) for alias in sorted(schema.ALIASES, key=lambda item: item.name)],
    }


def validate_layout(layout: Layout) -> None:
    if layout is None:
        raise CodegenError("layout is required")
    validate_schema_contract()
    layout_by_name = {entry.name: entry for entry in layout.instructions}
    if len(layout_by_name) != len(layout.instructions):
        raise CodegenError("layout contains duplicate instruction bindings")
    for instruction in schema.INSTRUCTIONS:
        if instruction.name not in layout_by_name:
            raise CodegenError(f"layout missing binding for instruction {instruction.name}")
    _validate_control_layout(layout)
    for entry in layout.instructions:
        if entry.name not in schema.INSTRUCTION_BY_NAME:
            raise CodegenError(f"layout binds unknown instruction {entry.name}")
        _validate_instruction_layout(schema.INSTRUCTION_BY_NAME[entry.name], entry, layout)
    _validate_no_overlap(layout.instructions)


def generate_json_ir(ir: Mapping[str, Any] | None = None, layout: Layout = SAMPLE_LAYOUT) -> str:
    value = build_ir(layout) if ir is None else ir
    return json.dumps(value, indent=2, sort_keys=True, separators=(",", ": ")) + "\n"


def generate_python_emit(ir: Mapping[str, Any] | None = None, layout: Layout = SAMPLE_LAYOUT) -> str:
    value = build_decoder_metadata(layout) if ir is None else ir
    ir_text = json.dumps(value, sort_keys=True, separators=(",", ":"))
    layout_name = value["layout"]
    return "\n".join(
        (
            '"""Generated curryGPU ISA emit metadata."""',
            "",
            "from __future__ import annotations",
            "",
            "import json",
            "",
            f"_IR_TEXT = {ir_text!r}",
            "",
            "",
            "def ir():",
            "    return json.loads(_IR_TEXT)",
            "",
            "",
            "def emit(mnemonic, *operands, **kwargs):",
            "    from currygpu.isa.assembler import emit as _emit",
            "    from currygpu.isa.codegen import resolve_layout",
            f"    return _emit(mnemonic, *operands, layout=resolve_layout({layout_name!r}), **kwargs)",
            "",
        )
    )


def generate_cpp_decoder(ir: Mapping[str, Any] | None = None, layout: Layout = SAMPLE_LAYOUT) -> str:
    value = build_decoder_metadata(layout) if ir is None else ir
    control = value["control"]["fields"]
    lines = [
        "// Generated curryGPU decoder metadata.",
        '#include "decoded_inst.gen.h"',
        "",
        "",
        "namespace currygpu::isa {",
        "static inline std::uint64_t extract_u64(unsigned __int128 word, unsigned lsb, unsigned width) {",
        "    const unsigned __int128 mask = (width == 128) ? ~static_cast<unsigned __int128>(0) : ((static_cast<unsigned __int128>(1) << width) - 1);",
        "    return static_cast<std::uint64_t>((word >> lsb) & mask);",
        "}",
        "",
        "static inline std::int64_t sign_extend(std::uint64_t value, unsigned width) {",
        "    if (width == 0 || width >= 64) return static_cast<std::int64_t>(value);",
        "    const std::uint64_t sign = std::uint64_t{1} << (width - 1);",
        "    return static_cast<std::int64_t>((value ^ sign) - sign);",
        "}",
        "",
        "static inline control_fields decode_control(unsigned __int128 word) {",
        "    return control_fields{",
        "        static_cast<std::uint32_t>(extract_u64(word, %d, %d))," % (control["read_barrier"]["lsb"], control["read_barrier"]["width"]),
        "        static_cast<std::uint32_t>(extract_u64(word, %d, %d))," % (control["reuse"]["lsb"], control["reuse"]["width"]),
        "        static_cast<std::uint32_t>(extract_u64(word, %d, %d))," % (control["stall"]["lsb"], control["stall"]["width"]),
        "        static_cast<std::uint32_t>(extract_u64(word, %d, %d))," % (control["wait_mask"]["lsb"], control["wait_mask"]["width"]),
        "        static_cast<std::uint32_t>(extract_u64(word, %d, %d))," % (control["write_barrier"]["lsb"], control["write_barrier"]["width"]),
        "        static_cast<std::uint32_t>(extract_u64(word, %d, %d))," % (control["yield"]["lsb"], control["yield"]["width"]),
        "    };",
        "}",
        "",
        "decoded_inst decode_trap(const char* trap) {",
        "    return decoded_inst{false, trap, \"\", 0, {}, control_fields{0, 0, 0, 0, 0, 0}};",
        "}",
        "",
        "decoded_inst decode_once(unsigned __int128 word) {",
    ]
    for index, instruction in enumerate(value["instructions"]):
        mask = instruction["match"]["mask"]
        match = instruction["match"]["value"]
        lines.append("    if ((word & %s) == %s) {" % (_cpp_u128(mask), _cpp_u128(match)))
        for reserved in instruction["reserved"]:
            reserved_mask = _bitmask(reserved["lsb"], reserved["width"])
            reserved_value = reserved["value"] << reserved["lsb"]
            lines.append(
                "        if ((word & %s) != %s) return decode_trap(\"reserved\");"
                % (_cpp_u128(reserved_mask), _cpp_u128(reserved_value))
            )
        lines.append(
            "        decoded_inst decoded = decoded_inst{true, \"\", \"%s\", %d, {}, decode_control(word)};"
            % (instruction["name"], len(instruction["fields"]))
        )
        for field_index, field in enumerate(instruction["fields"]):
            extract = "extract_u64(word, %d, %d)" % (field["lsb"], field["width"])
            if field["signed"]:
                value_expr = "sign_extend(%s, %d)" % (extract, field["width"])
            else:
                value_expr = "static_cast<std::int64_t>(%s)" % extract
            lines.append(
                "        decoded.fields[%d] = decoded_field{\"%s\", %s};"
                % (field_index, field["name"], value_expr)
            )
        lines.append("        return decoded;")
        lines.append("    }")
    lines.extend(
        [
            "    return decode_trap(\"unknown\");",
            "}",
            "} // namespace currygpu::isa",
            "",
        ]
    )
    return "\n".join(lines)


def generate_cpp_header(ir: Mapping[str, Any] | None = None, layout: Layout = SAMPLE_LAYOUT) -> str:
    value = build_decoder_metadata(layout) if ir is None else ir
    max_fields = max(len(instruction["fields"]) for instruction in value["instructions"])
    lines = [
        "// Generated curryGPU decoded instruction declarations.",
        "#pragma once",
        "#include <array>",
        "#include <cstdint>",
        "",
        "namespace currygpu::isa {",
        "struct decoded_field { const char* name; std::int64_t value; };",
        "struct control_fields { std::uint32_t read_barrier; std::uint32_t reuse; std::uint32_t stall; std::uint32_t wait_mask; std::uint32_t write_barrier; std::uint32_t yield; };",
        "struct decoded_inst { bool ok; const char* trap; const char* name; std::uint8_t field_count; std::array<decoded_field, %d> fields; control_fields control; };" % max_fields,
        "decoded_inst decode_once(unsigned __int128 word);",
        "} // namespace currygpu::isa",
        "",
    ]
    return "\n".join(lines)


def _public_instruction_ir(instruction: schema.InstructionSchema) -> Mapping[str, Any]:
    fields = []
    for field in sorted(instruction.fields, key=lambda item: item.name):
        fields.append(
            {
                "name": field.name,
                "kind": field.kind,
                "width": field.width,
                "signed": field.signed,
                "required": field.required,
                "default": field.default,
                "choices": list(field.choices),
            }
        )
    operands = []
    for operand in sorted(instruction.operands, key=lambda item: item.name):
        operands.append(
            {
                "name": operand.name,
                "kind": operand.kind,
                "field": operand.field,
                "fields": list(operand.fields),
                "required": operand.required,
                "default": operand.default,
                "constraints": dict(sorted(operand.constraints.items())),
            }
        )
    modifiers = []
    for modifier in sorted(instruction.modifiers, key=lambda item: item.name):
        modifiers.append(
            {
                "name": modifier.name,
                "field": modifier.field,
                "choices": list(modifier.choices),
                "default": modifier.default,
            }
        )
    return {
        "name": instruction.name,
        "semantics": instruction.semantics,
        "fields": fields,
        "operands": operands,
        "modifiers": modifiers,
    }


def _decoder_instruction_ir(instruction: schema.InstructionSchema, layout: InstructionLayout) -> Mapping[str, Any]:
    fields = []
    fields_by_name = schema.field_map(instruction)
    for field in sorted(instruction.fields, key=lambda item: item.name):
        field_layout = layout.fields[field.name]
        fields.append(
            {
                "name": field.name,
                "kind": field.kind,
                "width": field.width,
                "lsb": field_layout.lsb,
                "signed": field.signed,
                "required": field.required,
                "default": field.default,
                "choices": list(field.choices),
            }
        )
    operands = []
    for operand in sorted(instruction.operands, key=lambda item: item.name):
        operands.append(
            {
                "name": operand.name,
                "kind": operand.kind,
                "field": operand.field,
                "fields": list(operand.fields),
                "required": operand.required,
                "default": operand.default,
                "constraints": dict(sorted(operand.constraints.items())),
            }
        )
    modifiers = []
    for modifier in sorted(instruction.modifiers, key=lambda item: item.name):
        if modifier.field not in fields_by_name:
            raise CodegenError(f"modifier {instruction.name}.{modifier.name} references unknown field")
        modifier_layout = layout.modifiers[modifier.name]
        modifiers.append(
            {
                "name": modifier.name,
                "field": modifier.field,
                "values": dict(sorted(modifier_layout.values.items())),
                "choices": list(modifier.choices),
                "default": modifier.default,
            }
        )
    match_mask, match_value = _match_cube(layout)
    return {
        "name": instruction.name,
        "semantics": instruction.semantics,
        "match": {"mask": match_mask, "value": match_value},
        "opcode": {"value": layout.opcode, "lsb": layout.opcode_lsb, "width": layout.opcode_width},
        "fields": fields,
        "operands": operands,
        "modifiers": modifiers,
        "reserved": [
            {"name": item.name, "lsb": item.lsb, "width": item.width, "value": item.value}
            for item in sorted(layout.reserved, key=lambda item: (item.lsb, item.name))
        ],
    }


def _alias_ir(alias: schema.AliasSchema) -> Mapping[str, Any]:
    return {
        "name": alias.name,
        "target": alias.target,
        "operand_map": dict(sorted(alias.operand_map.items())),
        "fixed_operands": dict(sorted(alias.fixed_operands.items())),
        "fixed_modifiers": dict(sorted(alias.fixed_modifiers.items())),
    }


def _public_control_ir() -> Mapping[str, Any]:
    return {
        "width": schema.CONTROL_BITS,
        "fields": {
            name: {"width": schema.CONTROL_FIELDS[name]}
            for name in sorted(schema.CONTROL_FIELDS)
        },
    }


def _decoder_control_ir(layout: Layout) -> Mapping[str, Any]:
    fields = {}
    for name in sorted(schema.CONTROL_FIELDS):
        field_layout = layout.control_fields[name]
        fields[name] = {
            "width": schema.CONTROL_FIELDS[name],
            "lsb": field_layout.lsb,
        }
    return {"lsb": layout.control_lsb, "width": schema.CONTROL_BITS, "fields": fields}


def validate_schema_contract() -> None:
    instruction_names = {instruction.name for instruction in schema.INSTRUCTIONS}
    for instruction in schema.INSTRUCTIONS:
        fields = schema.field_map(instruction)
        for operand in instruction.operands:
            if operand.field and operand.field not in fields:
                raise CodegenError(f"operand {instruction.name}.{operand.name} references unknown field")
            for field_name in operand.fields:
                if field_name not in fields:
                    raise CodegenError(f"operand {instruction.name}.{operand.name} references unknown field")
        for modifier in instruction.modifiers:
            if modifier.field not in fields:
                raise CodegenError(f"modifier {instruction.name}.{modifier.name} references unknown field")
            if modifier.default not in modifier.choices:
                raise CodegenError(f"modifier {instruction.name}.{modifier.name} has invalid default")
    for alias in schema.ALIASES:
        if alias.target not in instruction_names:
            raise CodegenError(f"alias {alias.name} targets unknown instruction")
        target = schema.INSTRUCTION_BY_NAME[alias.target]
        target_operands = schema.operand_map(target)
        target_modifiers = schema.modifier_map(target)
        for target_name in alias.operand_map.values():
            if target_name not in target_operands:
                raise CodegenError(f"alias {alias.name} maps to unknown operand {target_name}")
        for target_name in alias.fixed_operands:
            if target_name not in target_operands:
                raise CodegenError(f"alias {alias.name} fixes unknown operand {target_name}")
        for modifier_name, value in alias.fixed_modifiers.items():
            if modifier_name not in target_modifiers:
                raise CodegenError(f"alias {alias.name} fixes unknown modifier {modifier_name}")
            if value not in target_modifiers[modifier_name].choices:
                raise CodegenError(f"alias {alias.name} fixes invalid modifier {modifier_name}")


def _validate_instruction_layout(instruction: schema.InstructionSchema, inst_layout: InstructionLayout, layout: Layout) -> None:
    if inst_layout.opcode_width <= 0:
        raise CodegenError(f"{inst_layout.name} opcode width must be positive")
    _validate_range(inst_layout.name, "opcode", inst_layout.opcode_lsb, inst_layout.opcode_width)
    if inst_layout.opcode >= (1 << inst_layout.opcode_width):
        raise CodegenError(f"{inst_layout.name} opcode does not fit declared width")
    schema_fields = schema.field_map(instruction)
    schema_modifiers = schema.modifier_map(instruction)
    for field in instruction.fields:
        if field.name not in inst_layout.fields:
            raise CodegenError(f"layout missing field {instruction.name}.{field.name}")
    for field_name, field_layout in inst_layout.fields.items():
        if field_name not in schema_fields:
            raise CodegenError(f"layout defines unknown field {instruction.name}.{field_name}")
        expected = schema_fields[field_name]
        if field_layout.width != expected.width:
            raise CodegenError(f"field width mismatch for {instruction.name}.{field_name}")
        _validate_range(instruction.name, field_name, field_layout.lsb, field_layout.width)
    for modifier in instruction.modifiers:
        if modifier.name not in inst_layout.modifiers:
            raise CodegenError(f"layout missing modifier binding {instruction.name}.{modifier.name}")
        modifier_layout = inst_layout.modifiers[modifier.name]
        if modifier_layout.field != modifier.field:
            raise CodegenError(f"modifier field mismatch for {instruction.name}.{modifier.name}")
        if set(modifier_layout.values) != set(modifier.choices):
            raise CodegenError(f"modifier values mismatch for {instruction.name}.{modifier.name}")
        field = schema_fields[modifier.field]
        for choice, raw in modifier_layout.values.items():
            if raw < 0 or raw >= (1 << field.width):
                raise CodegenError(f"modifier value {instruction.name}.{modifier.name}.{choice} does not fit field")
    for modifier_name in inst_layout.modifiers:
        if modifier_name not in schema_modifiers:
            raise CodegenError(f"layout defines unknown modifier {instruction.name}.{modifier_name}")
    claimed = [(inst_layout.opcode_lsb, inst_layout.opcode_width, "opcode")]
    claimed.extend((item.lsb, item.width, f"field {name}") for name, item in inst_layout.fields.items())
    claimed.extend((item.lsb, item.width, f"reserved {item.name}") for item in inst_layout.reserved)
    claimed.extend((item.lsb, item.width, f"fixed {item.name}") for item in inst_layout.fixed_bits)
    claimed.extend((item.lsb, item.width, f"ignored {item.name}") for item in inst_layout.ignored)
    claimed.extend((item.lsb, item.width, f"control {name}") for name, item in layout.control_fields.items())
    _validate_disjoint(instruction.name, claimed)
    for item in inst_layout.reserved:
        _validate_reserved(instruction.name, item)
    for item in inst_layout.fixed_bits:
        _validate_fixed(instruction.name, item)
    _validate_full_instruction_coverage(instruction.name, claimed)


def _validate_control_layout(layout: Layout) -> None:
    expected = set(schema.CONTROL_FIELDS)
    actual = set(layout.control_fields)
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    if missing:
        raise CodegenError(f"control layout missing fields: {', '.join(missing)}")
    if extra:
        raise CodegenError(f"control layout defines unknown fields: {', '.join(extra)}")
    claimed = []
    total = 0
    for name, width in schema.CONTROL_FIELDS.items():
        field_layout = layout.control_fields[name]
        if field_layout.width != width:
            raise CodegenError(f"control field width mismatch for {name}")
        _validate_range("control", name, field_layout.lsb, field_layout.width)
        claimed.append((field_layout.lsb, field_layout.width, name))
        total += field_layout.width
    if total != schema.CONTROL_BITS:
        raise CodegenError("control layout does not cover the declared control segment")
    _validate_disjoint("control", claimed)
    expected_bits = set(range(layout.control_lsb, layout.control_lsb + schema.CONTROL_BITS))
    actual_bits = {bit for lsb, width, _ in claimed for bit in range(lsb, lsb + width)}
    if actual_bits != expected_bits:
        raise CodegenError("control layout must cover one contiguous segment")


def _validate_no_overlap(instructions: Iterable[InstructionLayout]) -> None:
    entries = sorted(instructions, key=lambda item: item.name)
    for left_index, left in enumerate(entries):
        left_mask, left_value = _match_cube(left)
        for right in entries[left_index + 1 :]:
            right_mask, right_value = _match_cube(right)
            shared = left_mask & right_mask
            if (left_value & shared) == (right_value & shared):
                raise CodegenError(f"layout cubes overlap: {left.name} and {right.name}")


def _validate_disjoint(owner: str, ranges: Iterable[tuple[int, int, str]]) -> None:
    used: dict[int, str] = {}
    for lsb, width, label in ranges:
        _validate_range(owner, label, lsb, width)
        for bit in range(lsb, lsb + width):
            if bit in used:
                raise CodegenError(f"{owner} bit {bit} is claimed by both {used[bit]} and {label}")
            used[bit] = label


def _validate_range(owner: str, label: str, lsb: int, width: int) -> None:
    if lsb < 0 or width <= 0:
        raise CodegenError(f"{owner} {label} has invalid bit range")
    if lsb + width > schema.WORD_BITS:
        raise CodegenError(f"{owner} {label} overflows {schema.WORD_BITS}-bit instruction word")


def _validate_reserved(owner: str, reserved: ReservedLayout) -> None:
    if reserved.value < 0 or reserved.value >= (1 << reserved.width):
        raise CodegenError(f"{owner} reserved field {reserved.name} value does not fit width")


def _validate_fixed(owner: str, fixed: FixedBitLayout) -> None:
    if fixed.value < 0 or fixed.value >= (1 << fixed.width):
        raise CodegenError(f"{owner} fixed field {fixed.name} value does not fit width")


def _validate_full_instruction_coverage(owner: str, ranges: Iterable[tuple[int, int, str]]) -> None:
    covered = {bit for lsb, width, _ in ranges for bit in range(lsb, lsb + width)}
    missing = sorted(set(range(schema.WORD_BITS)) - covered)
    if missing:
        first = missing[0]
        raise CodegenError(f"{owner} bit {first} is not declared as field, reserved, fixed, or ignored")


def _match_cube(layout: InstructionLayout) -> tuple[int, int]:
    mask = _bitmask(layout.opcode_lsb, layout.opcode_width)
    value = layout.opcode << layout.opcode_lsb
    for item in layout.fixed_bits:
        item_mask = _bitmask(item.lsb, item.width)
        mask |= item_mask
        value |= item.value << item.lsb
    return mask, value


def _cpp_u128(value: int) -> str:
    low = value & ((1 << 64) - 1)
    high = value >> 64
    if high == 0:
        return f"0x{low:X}ull"
    return f"((static_cast<unsigned __int128>(0x{high:X}ull) << 64) | 0x{low:X}ull)"


def _bitmask(lsb: int, width: int) -> int:
    return ((1 << width) - 1) << lsb


def main(argv: list[str] | None = None) -> int:
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--layout", default=None, help=f"ISA layout selection, default {DEFAULT_LAYOUT!r} or environment override")
    parser.add_argument("--keep", action="store_true", help="do not clean the output directory first")
    args = parser.parse_args(argv)
    try:
        write_generated(args.out, layout=resolve_layout(args.layout), clean=not args.keep)
    except CodegenError as exc:
        print(f"codegen error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
