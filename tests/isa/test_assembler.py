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
        ("S2R", ("R9", "SR_LANEID"), {"guard": "PT"}),
        ("BSSY", ("B3", 64), {"guard": "PT"}),
        ("BSYNC", ("B3",), {"guard": "PT"}),
        ("BREAK", ("B3",), {"guard": "PT"}),
        ("YIELD", (), {"guard": "PT"}),
        ("ELECT", ("P2", 0x0000FFFF), {"guard": "PT"}),
        ("VOTE", ("P2", "P1", 0x0000FFFF), {"mode": "all", "guard": "PT"}),
        ("VOTE", ("P2", "P1", 0x0000FFFF, "R7"), {"mode": "ballot", "guard": "PT"}),
        ("EXIT", (), {}),
        ("LDG", ("R8", {"base": "R2", "ur": "UR3", "imm": 16}), {"width": "64", "guard": "PT"}),
        ("STG", ("R8", ("R2", "UR3", 16)), {"width": "64", "guard": "PT"}),
        ("LDS", ("R4", ("R1", 8)), {"width": "u16", "guard": "PT"}),
        ("STS", ("R4", "R1"), {"width": "u16", "guard": "PT"}),
        ("LDL", ("R4", ("R1", "URZ", -8)), {"width": "s16", "guard": "PT"}),
        ("STL", ("R4", ("R1", "UR0", -8)), {"width": "u8", "guard": "PT"}),
        ("LD", ("R16", ("R2", "UR1", 0)), {"width": "128", "guard": "PT"}),
        ("ST", ("R16", ("R2", "UR1", 0)), {"width": "128", "guard": "PT"}),
        ("LDC", ("R32", 3, ("R1", "UR2", 12)), {"width": "256", "guard": "PT"}),
        ("ATOM", ("R0", ("R2", 0), "R4"), {"op": "add", "guard": "PT"}),
        ("ATOMG", ("R0", ("R2", 0), "R4"), {"op": "cas", "cmp": "R6", "guard": "PT"}),
        ("ATOMS", ("R0", ("R1", 0), "R4"), {"op": "xor", "guard": "PT"}),
        ("RED", (("R2", 0), "R4"), {"op": "or", "guard": "PT"}),
        ("REDG", (("R2", 0), "R4"), {"op": "min", "guard": "PT"}),
        ("REDS", (("R1", 0), "R4"), {"op": "max", "guard": "PT"}),
        ("BAR", ("B0",), {"mode": "sync", "guard": "PT"}),
        ("BAR", ("B0", 64), {"mode": "sync", "guard": "PT"}),
        ("BAR", ("B0", 32), {"mode": "arv", "guard": "PT"}),
        ("MEMBAR", (), {"scope": "sys", "order": "release", "guard": "PT"}),
        ("FENCE", (), {"scope": "cta", "order": "acquire", "guard": "PT"}),
        ("CVTA", ("R2", "R4"), {"direction": "to_shared", "guard": "PT"}),
    ]

    for mnemonic, operands, kwargs in cases:
        encoded = assembler.encode(mnemonic, *operands, **kwargs)
        assert assembler.decode_like_ir(encoded.word) == encoded.ir
        assert encoded.ir["name"] in {
            "IADD3",
            "ISETP",
            "LOP3",
            "BRA",
            "S2R",
            "BSSY",
            "BSYNC",
            "BREAK",
            "YIELD",
            "ELECT",
            "VOTE",
            "EXIT",
            "LDG",
            "STG",
            "LDS",
            "STS",
            "LDL",
            "STL",
            "LD",
            "ST",
            "LDC",
            "ATOM",
            "ATOMG",
            "ATOMS",
            "RED",
            "REDG",
            "REDS",
            "BAR",
            "MEMBAR",
            "FENCE",
            "CVTA",
        }


def test_barrier_roundtrip_preserves_symbolic_operand() -> None:
    encoded = assembler.encode("BSSY", "B3", 64)

    assert encoded.ir["name"] == "BSSY"
    assert encoded.ir["operands"]["bar"] == "B3"
    assert encoded.ir["operands"]["target"] == 64


def test_s2r_roundtrip_preserves_laneid_selector() -> None:
    encoded = assembler.encode("S2R", "R3", "SR_LANEID", guard="P0", guard_neg=True)

    assert encoded.ir["name"] == "S2R"
    assert encoded.ir["operands"] == {"rd": "R3", "sr": "SR_LANEID"}
    assert encoded.ir["guard"] == {"predicate": "P0", "negated": True}
    assert assembler.decode_like_ir(encoded.word) == encoded.ir


def test_s2r_roundtrips_all_phase3_selectors() -> None:
    for selector in schema.SREG_CHOICES:
        encoded = assembler.encode("S2R", "R3", selector)
        assert encoded.ir["operands"] == {"rd": "R3", "sr": selector}
        assert assembler.decode_like_ir(encoded.word) == encoded.ir


def test_memory_address_roundtrip_preserves_base_uniform_and_offset() -> None:
    encoded = assembler.encode("LDG", "R8", {"base": "R2", "ur": "UR7", "imm": -17}, width="64")

    assert encoded.ir["name"] == "LDG"
    assert encoded.ir["operands"]["rd"] == "R8"
    assert encoded.ir["operands"]["addr"] == {"base": "R2", "ur": "UR7", "imm": -17}
    assert encoded.ir["modifiers"] == {"width": "64"}
    assert assembler.decode_like_ir(encoded.word) == encoded.ir


def test_bar_membar_fence_cvta_roundtrips() -> None:
    cases = [
        ("BAR", ("B0",), {"mode": "sync"}),
        ("BAR", ("B0", 64), {"mode": "sync"}),
        ("BAR", ("B0", 32), {"mode": "arv"}),
        ("MEMBAR", (), {"scope": "cta", "order": "sc"}),
        ("FENCE", (), {"scope": "sys", "order": "acq_rel"}),
        ("CVTA", ("R2", "R4"), {"direction": "from_local"}),
    ]
    for mnemonic, operands, kwargs in cases:
        encoded = assembler.encode(mnemonic, *operands, **kwargs)
        assert assembler.decode_like_ir(encoded.word) == encoded.ir


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
        ("S2R", ("RZ", "SR_LANEID"), {"guard": "PT"}),
        ("S2R", ("R0", "SR_LANEID"), {"guard": "P0", "guard_neg": True}),
        ("S2R", ("R127", "SR_LANEID"), {"guard": "P3"}),
        ("S2R", ("R254", "SR_LANEID"), {"guard": "P6"}),
        ("BSSY", ("B0", 0), {"guard": "PT"}),
        ("BSSY", ("B15", 16), {"guard": "P0", "guard_neg": True}),
        ("BSYNC", ("B0",), {"guard": "PT"}),
        ("BSYNC", ("B15",), {"guard": "P6"}),
        ("BREAK", ("B0",), {"guard": "PT"}),
        ("BREAK", ("B15",), {"guard": "P6"}),
        ("YIELD", (), {"guard": "PT"}),
        ("ELECT", ("P0", 0), {"guard": "PT"}),
        ("ELECT", ("P6", 0xFFFFFFFF), {"guard": "P0", "guard_neg": True}),
        ("VOTE", ("P0", "PT", 0), {"mode": "any", "guard": "PT"}),
        ("VOTE", ("P6", "P0", 0xFFFFFFFF, "R254"), {"mode": "ballot", "guard": "P6"}),
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
            field = schema.field_map(instruction)[operand.field] if operand.field else None
            if operand.kind == "register":
                assert field is not None
                aligned = operand.constraints.get("aligned")
                if isinstance(aligned, int):
                    candidates = ["RZ", "R0", f"R{aligned}", "R254"]
                else:
                    candidates = register_values
            elif operand.kind == "predicate":
                assert field is not None
                candidates = predicate_values[:-1]
            elif operand.kind == "sreg":
                candidates = ["SR_LANEID", "SR_TID.X", "SR_NWARPID", 0]
            elif operand.kind == "uniform_register":
                candidates = ["URZ", "UR0", "UR63", 0, 63]
            elif operand.kind == "barrier":
                candidates = ["B0", "B15", 0, 15]
            elif operand.kind == "barrier_count":
                assert field is not None
                candidates = [0, 32, 64, (1 << field.width) - 32]
            elif operand.kind == "membermask":
                assert field is not None
                candidates = _membermask_candidates(field)
            elif operand.kind == "immediate":
                assert field is not None
                candidates = _immediate_candidates(field, operand.constraints.get("aligned"))
            elif operand.kind == "address":
                candidates = _address_candidates(operand)
            else:
                raise AssertionError(f"unhandled operand kind {operand.kind}")

            base_operands[operand.name] = candidates[0]
            for value in candidates:
                operands = dict(base_operands)
                operands[operand.name] = value
                for other in instruction.operands:
                    if other.name in operands:
                        continue
                    operands[other.name] = _default_operand_for_instruction(instruction, other)
                kwargs = _default_modifiers(instruction)
                encoded = assembler.encode(instruction.name, **operands, **kwargs)
                assert assembler.decode_like_ir(encoded.word) == encoded.ir

        for modifier in instruction.modifiers:
            operands = {
                operand.name: _default_operand_for_instruction(instruction, operand)
                for operand in instruction.operands
            }
            for choice in modifier.choices:
                kwargs = _default_modifiers(instruction)
                kwargs[modifier.name] = choice
                if instruction.name == "BAR" and modifier.name == "mode" and choice == "arv":
                    operands["count"] = 32
                if instruction.name in {"ATOM", "ATOMG", "ATOMS", "RED", "REDG", "REDS"} and modifier.name == "op" and choice == "cas":
                    operands["cmp"] = "R6"
                encoded = assembler.encode(instruction.name, **operands, **kwargs)
                assert encoded.ir["modifiers"][modifier.name] == choice

        for guard in predicate_values:
            operands = {
                operand.name: _default_operand_for_instruction(instruction, operand)
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


def test_bssy_alignment_is_rejected() -> None:
    with pytest.raises(assembler.AssembleError, match="aligned"):
        assembler.encode("BSSY", "B0", 12)


def test_barrier_index_is_rejected() -> None:
    with pytest.raises(assembler.AssembleError, match="barrier"):
        assembler.encode("BSYNC", "B16")


def test_membermask_width_is_rejected() -> None:
    with pytest.raises(assembler.AssembleError, match="outside range"):
        assembler.encode("ELECT", "P0", 1 << 32)


def test_s2r_unknown_selector_is_rejected() -> None:
    with pytest.raises(assembler.AssembleError, match="invalid special register"):
        assembler.encode("S2R", "R0", "SR_TID_X")


def test_s2r_predicate_as_destination_is_rejected() -> None:
    with pytest.raises(assembler.AssembleError, match="invalid register"):
        assembler.encode("S2R", "P0", "SR_LANEID")


def test_s2r_selector_reserved_values_are_rejected_on_decode() -> None:
    word = assembler.emit("S2R", "R0", "SR_LANEID") | (15 << 20)

    with pytest.raises(assembler.AssembleError, match="special register"):
        assembler.decode_like_ir(word)


def test_s2r_reserved_bits_are_rejected_on_decode() -> None:
    word = assembler.emit("S2R", "R0", "SR_LANEID") | (1 << 28)

    with pytest.raises(assembler.AssembleError, match="reserved field"):
        assembler.decode_like_ir(word)


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


def test_memory_width_register_group_alignment_is_rejected() -> None:
    cases = [
        ("LDG", ("R1", ("R2", 0)), {"width": "64"}),
        ("STG", ("R1", ("R2", 0)), {"width": "64"}),
        ("LD", ("R2", ("R2", 0)), {"width": "128"}),
        ("LDC", ("R4", 0, ("R1", 0)), {"width": "256"}),
    ]
    for mnemonic, operands, kwargs in cases:
        with pytest.raises(assembler.AssembleError, match="aligned"):
            assembler.encode(mnemonic, *operands, **kwargs)


def test_64_bit_address_base_register_alignment_is_rejected() -> None:
    cases = [
        ("LDG", ("R0", ("R1", 0))),
        ("STG", ("R0", ("R1", 0))),
        ("LD", ("R0", ("R1", 0))),
        ("ST", ("R0", ("R1", 0))),
        ("ATOM", ("R0", ("R1", 0), "R2")),
        ("ATOMG", ("R0", ("R1", 0), "R2")),
        ("RED", (("R1", 0), "R2")),
        ("REDG", (("R1", 0), "R2")),
    ]
    for mnemonic, operands in cases:
        with pytest.raises(assembler.AssembleError, match="aligned"):
            assembler.encode(mnemonic, *operands)


def test_address_immediate_range_is_rejected() -> None:
    with pytest.raises(assembler.AssembleError, match="outside range"):
        assembler.encode("LDS", "R0", ("R1", 1 << 19))


def test_unknown_memory_width_is_rejected() -> None:
    with pytest.raises(assembler.AssembleError, match="invalid modifier"):
        assembler.encode("LDG", "R0", ("R2", 0), width="512")


def test_bar_arv_requires_explicit_nonzero_count() -> None:
    with pytest.raises(assembler.AssembleError, match="BAR.ARV"):
        assembler.encode("BAR", "B0", mode="arv")


def test_bar_count_must_be_warp_multiple() -> None:
    with pytest.raises(assembler.AssembleError, match="multiple of 32"):
        assembler.encode("BAR", "B0", 33)


def test_atomic_cas_requires_compare_register() -> None:
    with pytest.raises(assembler.AssembleError, match="CAS requires"):
        assembler.encode("ATOMG", "R0", ("R2", 0), "R4", op="cas")


def test_red_rejects_destination_operand() -> None:
    with pytest.raises(assembler.AssembleError, match="unknown argument"):
        assembler.encode("REDG", ("R2", 0), "R4", rd="R0")


def test_new_instruction_reserved_bits_are_rejected_on_decode() -> None:
    word = assembler.emit("LDG", "R0", ("R2", 0)) | (1 << 59)

    with pytest.raises(assembler.AssembleError, match="reserved field"):
        assembler.decode_like_ir(word)


def test_invalid_raw_atomic_op_is_rejected_on_decode() -> None:
    word = assembler.emit("ATOMG", "R0", ("R2", 0), "R4") | (0xF << 72)

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


def _membermask_candidates(field: schema.FieldSchema) -> list[int]:
    return [0, (1 << field.width) - 1, 1, 0x80000000]


def _address_candidates(operand: schema.OperandSchema) -> list[dict[str, int | str]]:
    base_values = ["R2", "R254"] if operand.constraints.get("address_bits") == 64 else ["R0", "R1", "R254"]
    return [{"base": base, "ur": "UR0", "imm": 0} for base in base_values] + [
        {"base": base_values[0], "ur": "UR63", "imm": -(1 << 19)},
        {"base": base_values[0], "ur": "URZ", "imm": (1 << 19) - 1},
    ]


def _default_operand_for_instruction(instruction: schema.InstructionSchema, operand: schema.OperandSchema):
    if operand.field:
        return _default_operand(operand, schema.field_map(instruction)[operand.field])
    if operand.kind == "address":
        if operand.constraints.get("address_bits") == 64:
            return {"base": "R2", "ur": "URZ", "imm": 0}
        return {"base": "R0", "ur": "URZ", "imm": 0}
    raise AssertionError(f"unhandled operand kind {operand.kind}")


def _default_operand(operand: schema.OperandSchema, field: schema.FieldSchema):
    if operand.kind == "register":
        aligned = operand.constraints.get("aligned")
        if isinstance(aligned, int):
            return f"R{aligned}"
        return "R0"
    if operand.kind == "uniform_register":
        return "UR0"
    if operand.kind == "predicate":
        return "P0"
    if operand.kind == "sreg":
        return "SR_LANEID"
    if operand.kind == "barrier":
        return "B0"
    if operand.kind == "barrier_count":
        return 0
    if operand.kind == "membermask":
        return 1
    if operand.kind == "immediate":
        aligned = operand.constraints.get("aligned")
        if isinstance(aligned, int):
            return 0
        return 0 if not field.signed else min(0, (1 << (field.width - 1)) - 1)
    raise AssertionError(f"unhandled operand kind {operand.kind}")


def _default_modifiers(instruction: schema.InstructionSchema) -> dict[str, str]:
    return {modifier.name: modifier.default for modifier in instruction.modifiers}
