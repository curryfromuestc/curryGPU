from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from currygpu.isa import codegen, schema
from currygpu.isa.layout import LAYOUT_ENV_VAR, LayoutSelectionError, load_layout
from currygpu.isa.layout.sample import FieldLayout, ModifierLayout, ReservedLayout, SAMPLE_LAYOUT


def test_generate_all_outputs_stable_ir_and_sources() -> None:
    first = codegen.generate_all()
    second = codegen.generate_all()

    assert first.ir_json == second.ir_json
    assert first.python_emit == second.python_emit
    assert first.cpp_decoder == second.cpp_decoder

    ir = json.loads(first.ir_json)
    assert ir["word_bits"] == 128
    assert "layout" not in ir
    assert [item["name"] for item in ir["instructions"]] == ["BRA", "EXIT", "IADD3", "ISETP", "LOP3"]
    assert {item["name"] for item in ir["aliases"]} == {"MOV"}
    assert ir["control"]["width"] == 21
    assert "lsb" not in ir["control"]
    assert all("opcode" not in item for item in ir["instructions"])
    assert all("reserved" not in item for item in ir["instructions"])
    assert "decode_once" in first.cpp_decoder
    assert "def emit" in first.python_emit


def test_cpp_header_exposes_decoder_interface() -> None:
    header = codegen.generate_cpp_header()
    decoder = codegen.generate_cpp_decoder()

    assert "struct decoded_inst { bool ok;" in header
    assert "decoded_inst decode_once(unsigned __int128 word);" in header
    assert '#include "decoded_inst.gen.h"' in decoder
    assert "decoded_inst decode_once(unsigned __int128 word)" in decoder


def test_schema_modifier_choices_do_not_store_encoding_values() -> None:
    for instruction in schema.INSTRUCTIONS:
        for modifier in instruction.modifiers:
            assert isinstance(modifier.choices, tuple)
            assert all(isinstance(choice, str) for choice in modifier.choices)


def test_codegen_cli_writes_expected_files(tmp_path: Path) -> None:
    out_dir = tmp_path / "generated"
    subprocess.run(
        [sys.executable, "-m", "currygpu.isa.codegen", "--out", str(out_dir)],
        check=True,
        env={**os.environ, "PYTHONPATH": "isa"},
    )

    assert sorted(path.name for path in out_dir.iterdir()) == [
        "decode.gen.cpp",
        "decoded_inst.gen.h",
        "emit.gen.py",
        "isa_ir.json",
    ]
    written_ir = json.loads((out_dir / "isa_ir.json").read_text(encoding="utf-8"))
    assert "layout" not in written_ir
    assert written_ir["word_bits"] == 128


def test_layout_selection_defaults_to_sample_and_rejects_missing_production(monkeypatch) -> None:
    monkeypatch.delenv(LAYOUT_ENV_VAR, raising=False)

    assert load_layout() is SAMPLE_LAYOUT
    assert load_layout("sample") is SAMPLE_LAYOUT

    monkeypatch.setenv(LAYOUT_ENV_VAR, "production")
    with pytest.raises(LayoutSelectionError, match="production layout is not available"):
        load_layout()

    assert load_layout("sample") is SAMPLE_LAYOUT


def test_codegen_cli_rejects_missing_production_layout(tmp_path: Path) -> None:
    out_dir = tmp_path / "generated"
    proc = subprocess.run(
        [sys.executable, "-m", "currygpu.isa.codegen", "--layout", "production", "--out", str(out_dir)],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={**os.environ, "PYTHONPATH": "isa"},
    )

    assert proc.returncode == 1
    assert "production layout is not available" in proc.stderr
    assert not out_dir.exists()


def test_codegen_cli_is_deterministic_across_hash_seeds(tmp_path: Path) -> None:
    outputs = []
    for seed in ("1", "314159"):
        out_dir = tmp_path / f"generated-{seed}"
        env = {**os.environ, "PYTHONPATH": "isa", "PYTHONHASHSEED": seed, "LC_ALL": "C", "TZ": "UTC"}
        subprocess.run(
            [sys.executable, "-m", "currygpu.isa.codegen", "--out", str(out_dir)],
            check=True,
            env=env,
        )
        outputs.append(
            {
                path.name: path.read_bytes()
                for path in sorted(out_dir.iterdir(), key=lambda item: item.name)
                if path.is_file()
            }
        )

    assert outputs[0] == outputs[1]


def test_cpp_decoder_uses_selected_control_layout() -> None:
    control_fields = {
        "reuse": FieldLayout(107, 4),
        "wait_mask": FieldLayout(111, 6),
        "write_barrier": FieldLayout(117, 3),
        "read_barrier": FieldLayout(120, 3),
        "yield": FieldLayout(123, 1),
        "stall": FieldLayout(124, 4),
    }
    layout = replace(SAMPLE_LAYOUT, control_fields=control_fields)

    decoder = codegen.generate_cpp_decoder(layout=layout)

    assert "extract_u64(word, 124, 4)" in decoder
    assert "extract_u64(word, 111, 6)" in decoder
    assert "extract_u64(word, 123, 1)" in decoder


def test_missing_instruction_binding_is_rejected() -> None:
    layout = replace(
        SAMPLE_LAYOUT,
        instructions=tuple(item for item in SAMPLE_LAYOUT.instructions if item.name != "EXIT"),
    )

    with pytest.raises(codegen.CodegenError, match="missing binding.*EXIT"):
        codegen.build_ir(layout)


def test_opcode_overlap_is_rejected_symbolically() -> None:
    iadd3 = next(item for item in SAMPLE_LAYOUT.instructions if item.name == "IADD3")
    isetp = next(item for item in SAMPLE_LAYOUT.instructions if item.name == "ISETP")
    overlapping = replace(isetp, opcode=iadd3.opcode)
    layout = replace(
        SAMPLE_LAYOUT,
        instructions=tuple(overlapping if item.name == "ISETP" else item for item in SAMPLE_LAYOUT.instructions),
    )

    with pytest.raises(codegen.CodegenError, match="overlap"):
        codegen.build_ir(layout)


def test_field_bit_conflict_is_rejected() -> None:
    iadd3 = next(item for item in SAMPLE_LAYOUT.instructions if item.name == "IADD3")
    bad_fields = dict(iadd3.fields)
    bad_fields["rd"] = FieldLayout(iadd3.opcode_lsb, bad_fields["rd"].width)
    bad_iadd3 = replace(iadd3, fields=bad_fields)
    layout = replace(
        SAMPLE_LAYOUT,
        instructions=tuple(bad_iadd3 if item.name == "IADD3" else item for item in SAMPLE_LAYOUT.instructions),
    )

    with pytest.raises(codegen.CodegenError, match="claimed by both"):
        codegen.build_ir(layout)


def test_control_coverage_is_required() -> None:
    control_fields = dict(SAMPLE_LAYOUT.control_fields)
    control_fields.pop("reuse")
    layout = replace(SAMPLE_LAYOUT, control_fields=control_fields)

    with pytest.raises(codegen.CodegenError, match="control layout missing"):
        codegen.build_ir(layout)


def test_control_segment_must_be_contiguous() -> None:
    control_fields = dict(SAMPLE_LAYOUT.control_fields)
    control_fields["yield"] = FieldLayout(106, 1)
    layout = replace(SAMPLE_LAYOUT, control_fields=control_fields)

    with pytest.raises(codegen.CodegenError, match="contiguous"):
        codegen.build_ir(layout)


def test_modifier_layout_binding_is_required() -> None:
    iadd3 = next(item for item in SAMPLE_LAYOUT.instructions if item.name == "IADD3")
    bad_iadd3 = replace(iadd3, modifiers={})
    layout = replace(
        SAMPLE_LAYOUT,
        instructions=tuple(bad_iadd3 if item.name == "IADD3" else item for item in SAMPLE_LAYOUT.instructions),
    )

    with pytest.raises(codegen.CodegenError, match="missing modifier binding"):
        codegen.build_ir(layout)


def test_modifier_layout_choices_must_match_schema() -> None:
    isetp = next(item for item in SAMPLE_LAYOUT.instructions if item.name == "ISETP")
    bad_isetp = replace(isetp, modifiers={"cmp": ModifierLayout("cmp", {"eq": 0})})
    layout = replace(
        SAMPLE_LAYOUT,
        instructions=tuple(bad_isetp if item.name == "ISETP" else item for item in SAMPLE_LAYOUT.instructions),
    )

    with pytest.raises(codegen.CodegenError, match="modifier values mismatch"):
        codegen.build_ir(layout)


def test_instruction_bits_must_be_fully_declared() -> None:
    exit_layout = next(item for item in SAMPLE_LAYOUT.instructions if item.name == "EXIT")
    bad_exit = replace(exit_layout, reserved=(ReservedLayout("exit_reserved", 13, 94),))
    layout = replace(
        SAMPLE_LAYOUT,
        instructions=tuple(bad_exit if item.name == "EXIT" else item for item in SAMPLE_LAYOUT.instructions),
    )

    with pytest.raises(codegen.CodegenError, match="bit 12 is not declared"):
        codegen.build_ir(layout)
