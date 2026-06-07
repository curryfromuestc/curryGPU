from __future__ import annotations

import pytest

from currygpu.isa import assembler, schema


def test_iadd3_roundtrip_preserves_symbolic_fields() -> None:
    encoded = assembler.encode(
        "IADD3",
        "R1",
        "R2",
        "RZ",
        -7,
        sat="sat",
        guard="P0",
        guard_neg=True,
        stall=3,
        wait_mask=17,
    )

    assert 0 <= encoded.word < (1 << 128)
    assert encoded.ir == assembler.decode_like_ir(encoded.word)
    assert encoded.ir["name"] == "IADD3"
    assert encoded.ir["operands"] == {"rd": "R1", "src_a": "R2", "src_b": "RZ", "src_c": -7}
    assert encoded.ir["modifiers"] == {"sat": "sat"}
    assert encoded.ir["guard"] == {"predicate": "P0", "negated": True}
    assert encoded.ir["control"]["stall"] == 3
    assert encoded.ir["control"]["wait_mask"] == 17


def test_mov_alias_encodes_to_iadd3_shape() -> None:
    ir = assembler.roundtrip_ir("MOV", "R4", "R9")

    assert ir["name"] == "IADD3"
    assert ir["operands"] == {"rd": "R4", "src_a": "R9", "src_b": "RZ", "src_c": 0}
    assert ir["modifiers"] == {"sat": "none"}


def test_sample_subset_roundtrips() -> None:
    cases = [
        ("IADD3", ("R0", "R1", "R2", 0), {}),
        ("ISETP", ("P1", "R2", "R3"), {"cmp": "ge"}),
        ("LOP3", ("R5", "R6", "R7", "R8", 0xCA), {}),
        ("BRA", (32,), {"guard": "PT"}),
        ("EXIT", (), {}),
    ]

    for mnemonic, operands, kwargs in cases:
        encoded = assembler.encode(mnemonic, *operands, **kwargs)
        assert assembler.decode_like_ir(encoded.word) == encoded.ir
        assert encoded.ir["name"] in {"IADD3", "ISETP", "LOP3", "BRA", "EXIT"}


def test_reproducible_boundary_matrix_roundtrips() -> None:
    cases = [
        ("IADD3", ("R0", "RZ", "R254", -(1 << 19)), {"sat": "none", "guard": "PT", "guard_neg": False}),
        ("IADD3", ("R127", "R0", "R127", 0), {"sat": "sat", "guard": "P0", "guard_neg": True}),
        ("IADD3", ("R254", "R127", "R0", (1 << 19) - 1), {"sat": "none", "guard": "P6"}),
        ("ISETP", ("P0", "R0", "RZ"), {"cmp": "eq", "guard": "PT"}),
        ("ISETP", ("P6", "R254", "R127"), {"cmp": "ge", "guard": "P0", "guard_neg": True}),
        ("LOP3", ("R0", "RZ", "R0", "R254", 0), {"guard": "PT"}),
        ("LOP3", ("R127", "R0", "R127", "RZ", 0xA5), {"guard": "P0", "guard_neg": True}),
        ("LOP3", ("R254", "R254", "RZ", "R0", 0xFF), {"guard": "P6"}),
        ("BRA", (0,), {"guard": "PT"}),
        ("BRA", (16,), {"guard": "P0", "guard_neg": True}),
        ("EXIT", (), {"guard": "PT"}),
        ("EXIT", (), {"guard": "P6", "guard_neg": True}),
    ]
    control_sets = [
        {},
        {name: 0 for name in schema.CONTROL_FIELDS},
        {name: (1 << width) - 1 for name, width in schema.CONTROL_FIELDS.items()},
        {name: (1 << width) // 2 for name, width in schema.CONTROL_FIELDS.items()},
    ]

    for mnemonic, operands, kwargs in cases:
        for control in control_sets:
            encoded = assembler.encode(mnemonic, *operands, **kwargs, **control)
            decoded = assembler.decode_like_ir(encoded.word)
            assert decoded == encoded.ir
            assert decoded["control"] == {name: control.get(name, 0) for name in sorted(schema.CONTROL_FIELDS)}


def test_schema_driven_boundary_matrix_roundtrips() -> None:
    register_values = ["RZ", "R0", "R254", "R127"]
    predicate_values = ["PT", "P0", "P6", "P3"]

    for instruction in schema.INSTRUCTIONS:
        base_operands = {}
        for operand in instruction.operands:
            field = schema.field_map(instruction)[operand.field]
            if operand.kind == "register":
                candidates = register_values
            elif operand.kind == "predicate":
                candidates = predicate_values[:-1]
            elif operand.kind == "immediate":
                candidates = _immediate_candidates(field, operand.constraints.get("aligned"))
            else:
                raise AssertionError(f"unhandled operand kind {operand.kind}")

            base_operands[operand.name] = candidates[0]
            for value in candidates:
                operands = dict(base_operands)
                operands[operand.name] = value
                for other in instruction.operands:
                    if other.name in operands:
                        continue
                    other_field = schema.field_map(instruction)[other.field]
                    operands[other.name] = _default_operand(other, other_field)
                kwargs = _default_modifiers(instruction)
                encoded = assembler.encode(instruction.name, **operands, **kwargs)
                assert assembler.decode_like_ir(encoded.word) == encoded.ir

        for modifier in instruction.modifiers:
            operands = {
                operand.name: _default_operand(operand, schema.field_map(instruction)[operand.field])
                for operand in instruction.operands
            }
            for choice in modifier.choices:
                kwargs = _default_modifiers(instruction)
                kwargs[modifier.name] = choice
                encoded = assembler.encode(instruction.name, **operands, **kwargs)
                assert encoded.ir["modifiers"][modifier.name] == choice

        for guard in predicate_values:
            operands = {
                operand.name: _default_operand(operand, schema.field_map(instruction)[operand.field])
                for operand in instruction.operands
            }
            encoded = assembler.encode(instruction.name, **operands, **_default_modifiers(instruction), guard=guard, guard_neg=True)
            assert encoded.ir["guard"] == {"predicate": guard, "negated": True}


@pytest.mark.parametrize(
    ("args", "kwargs", "message"),
    [
        (("R255", "R0", "R0", 0), {}, "invalid register"),
        (("R0", "R0", "R0", 1 << 19), {}, "outside range"),
        (("R0", "R0", "R0", 0), {"sat": "wide"}, "invalid modifier"),
        (("R0", "R0", "R0", 0), {"wait_mask": 64}, "control field wait_mask"),
    ],
)
def test_iadd3_invalid_inputs_are_rejected(args, kwargs, message) -> None:
    with pytest.raises(assembler.AssembleError, match=message):
        assembler.encode("IADD3", *args, **kwargs)


def test_branch_alignment_is_rejected() -> None:
    with pytest.raises(assembler.AssembleError, match="aligned"):
        assembler.encode("BRA", 12)


def test_reserved_bits_are_rejected_on_decode() -> None:
    word = assembler.emit("EXIT") | (1 << 12)

    with pytest.raises(assembler.AssembleError, match="reserved field"):
        assembler.decode_like_ir(word)


def test_unknown_opcode_is_rejected_on_decode() -> None:
    with pytest.raises(assembler.AssembleError, match="known opcode"):
        assembler.decode_like_ir(0xFE)


def test_invalid_raw_modifier_value_is_rejected_on_decode() -> None:
    word = assembler.emit("ISETP", "P0", "R0", "R0") | (0b110 << 36)

    with pytest.raises(assembler.AssembleError, match="decoded modifier"):
        assembler.decode_like_ir(word)


def test_illegal_predicate_is_rejected() -> None:
    with pytest.raises(assembler.AssembleError, match="invalid predicate"):
        assembler.encode("EXIT", guard="P7")


def test_immediate_must_be_integer() -> None:
    with pytest.raises(assembler.AssembleError, match="immediate must be an integer"):
        assembler.encode("LOP3", "R0", "R0", "R0", "R0", True)


def test_unknown_kwargs_are_rejected() -> None:
    with pytest.raises(assembler.AssembleError, match="unknown argument"):
        assembler.encode("EXIT", typo=1)


def test_alias_unknown_kwargs_are_rejected() -> None:
    with pytest.raises(assembler.AssembleError, match="unknown argument"):
        assembler.encode("MOV", "R1", "R2", typo=1)


def _immediate_candidates(field: schema.FieldSchema, aligned) -> list[int]:
    if field.signed:
        lo = -(1 << (field.width - 1))
        hi = (1 << (field.width - 1)) - 1
    else:
        lo = 0
        hi = (1 << field.width) - 1
    candidates = [lo, hi, 0, (lo + hi) // 2]
    if isinstance(aligned, int):
        candidates = [value - (value % aligned) for value in candidates]
        candidates = [value for value in candidates if lo <= value <= hi]
    return list(dict.fromkeys(candidates))


def _default_operand(operand: schema.OperandSchema, field: schema.FieldSchema):
    if operand.kind == "register":
        return "R0"
    if operand.kind == "predicate":
        return "P0"
    if operand.kind == "immediate":
        aligned = operand.constraints.get("aligned")
        if isinstance(aligned, int):
            return 0
        return 0 if not field.signed else min(0, (1 << (field.width - 1)) - 1)
    raise AssertionError(f"unhandled operand kind {operand.kind}")


def _default_modifiers(instruction: schema.InstructionSchema) -> dict[str, str]:
    return {modifier.name: modifier.default for modifier in instruction.modifiers}
