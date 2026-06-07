from __future__ import annotations

import pytest

from currygpu.iss import native
from currygpu.isa import assembler


pytestmark = pytest.mark.skipif(not native.available(), reason="native extension is not built")


def test_native_step_runs_inside_coarse_boundary() -> None:
    program = [
        {"op": "IADD3", "operands": {"dst": "R1", "src0": 1, "src1": 2, "src2": 3}},
        {"op": "IADD3", "operands": {"dst": "R2", "src0": "R1", "src1": 4, "src2": 0}},
        {"op": "EXIT"},
    ]

    native.reset_boundary_calls()
    warp = native.launch(program)
    before_step = native.boundary_calls()
    snapshot = native.step(warp, 10)
    after_step = native.boundary_calls()

    assert before_step == 1
    assert after_step == 2
    assert snapshot["trap"]["kind"] == "none"
    assert snapshot["vgpr"]["1"] == [6] * 32
    assert snapshot["vgpr"]["2"] == [10] * 32
    assert snapshot["counters"]["warp_instructions"] == 3


def test_native_state_diff_is_localized() -> None:
    left = {"vgpr": {"1": [1, 2, 3]}}
    right = {"vgpr": {"1": [1, 9, 3]}}

    assert native.state_diff(left, right) == [{"path": "$.vgpr.1[1]", "left": 2, "right": 9}]


def test_native_launch_words_decodes_and_executes_inside_step() -> None:
    words = [
        assembler.emit("IADD3", "R1", "RZ", "RZ", 6),
        assembler.emit("IADD3", "R2", "R1", "RZ", 4),
        assembler.emit("EXIT"),
    ]

    native.reset_boundary_calls()
    warp = native.launch_words(words)
    snapshot = native.step(warp, 10)

    assert native.boundary_calls() == 2
    assert snapshot["trap"]["kind"] == "none"
    assert snapshot["vgpr"]["1"] == [6] * 32
    assert snapshot["vgpr"]["2"] == [10] * 32


def test_native_launch_words_boundary_count_is_coarse() -> None:
    words = [
        assembler.emit("IADD3", "R1", "R1", "RZ", 1),
        assembler.emit("IADD3", "R1", "R1", "RZ", 1),
        assembler.emit("IADD3", "R1", "R1", "RZ", 1),
        assembler.emit("IADD3", "R1", "R1", "RZ", 1),
        assembler.emit("EXIT"),
    ]

    native.reset_boundary_calls()
    warp = native.launch_words(words)
    snapshot = native.step(warp, 10)

    assert snapshot["trap"]["kind"] == "none"
    assert snapshot["counters"]["warp_instructions"] == 5
    assert native.boundary_calls() == 2


def test_native_launch_words_sign_extends_iadd3_immediate() -> None:
    words = [
        assembler.emit("IADD3", "R1", "RZ", "RZ", -1),
        assembler.emit("EXIT"),
    ]

    snapshot = native.step(native.launch_words(words), 10)

    assert snapshot["trap"]["kind"] == "none"
    assert snapshot["vgpr"]["1"] == [0xFFFFFFFF] * 32


def test_native_launch_words_executes_predicate_lop3_and_branch() -> None:
    words = [
        assembler.emit("IADD3", "R1", "RZ", "RZ", 9),
        assembler.emit("ISETP", "P0", "R1", "RZ", cmp="gt"),
        assembler.emit("LOP3", "R2", "R1", "R1", "RZ", 0x06, guard="P0"),
        assembler.emit("BRA", 80),
        assembler.emit("IADD3", "R3", "RZ", "RZ", 1),
        assembler.emit("EXIT"),
    ]

    snapshot = native.step(native.launch_words(words), 20)

    assert snapshot["trap"]["kind"] == "none"
    assert snapshot["vgpr"]["2"] == [0] * 32
    assert snapshot["vgpr"]["3"] == [0] * 32
    assert snapshot["predicates"]["P0"] == [True] * 32


def test_native_launch_words_reports_decode_trap() -> None:
    bad_word = assembler.emit("EXIT") | (1 << 12)
    warp = native.launch_words([bad_word])

    snapshot = native.step(warp, 10)

    assert snapshot["trap"]["kind"] == "decode"
    assert snapshot["trap"]["reason"] == "reserved"


def test_native_launch_words_reports_modifier_decode_trap() -> None:
    bad_word = assembler.emit("ISETP", "P0", "R0", "R0") | (0b110 << 36)
    warp = native.launch_words([bad_word])

    snapshot = native.step(warp, 10)

    assert snapshot["trap"]["kind"] == "decode"
    assert snapshot["trap"]["reason"] == "modifier"


def test_native_minimal_kernel_covers_alu_predicate_branch_exit_and_state_contract() -> None:
    program = [
        {"op": "IADD3", "operands": {"dst": "R1", "src0": 4, "src1": 5, "src2": "RZ"}},
        {"op": "IADD3", "operands": {"dst": "R2", "src0": "R1", "src1": "RZ", "src2": "RZ"}},
        {"op": "ISETP", "operands": {"dst": "P0", "src0": "R2", "src1": 9, "cmp": "EQ"}},
        {"op": "LOP3", "guard": "@P0", "operands": {"dst": "R3", "src0": "R1", "src1": 0xFFFFFFFF, "src2": "RZ", "lut": 0x06}},
        {"op": "BRA", "operands": {"target": 6}},
        {"op": "IADD3", "operands": {"dst": "R4", "src0": 1, "src1": 1, "src2": 1}},
        {"op": "EXIT"},
    ]

    snapshot = native.step(native.launch(program), 20)

    assert snapshot["trap"]["kind"] == "none"
    assert snapshot["active_mask"] == [False] * 32
    assert snapshot["lane_state"] == ["exited"] * 32
    assert snapshot["vgpr"]["1"] == [9] * 32
    assert snapshot["vgpr"]["2"] == [9] * 32
    assert snapshot["vgpr"]["3"] == [0xFFFFFFF6] * 32
    assert snapshot["vgpr"]["4"] == [0] * 32
    assert snapshot["predicates"]["P0"] == [True] * 32
    assert snapshot["pc"] == [7] * 32
    assert len(snapshot["bx"]["barriers"]) == 16
    assert snapshot["bx"]["barriers"][0] == {"participation_mask": 0, "reconv_pc": 0, "valid": False}
    assert snapshot["memory"] == {"global": {}, "local": {}, "shared": {}}
    assert snapshot["uniform_registers"] == [0] * 64
    assert snapshot["counters"] == {
        "divergence_events": 0,
        "instructions": 192,
        "mem_ops": 0,
        "warp_instructions": 6,
    }


def test_native_guarded_execution_does_not_write_when_predicate_is_false() -> None:
    program = [
        {"op": "LOP3", "guard": "@P0", "operands": {"dst": "R1", "src0": 1, "src1": 2, "src2": 4, "lut": 0xFF}},
        {"op": "EXIT"},
    ]

    snapshot = native.step(native.launch(program), 10)

    assert snapshot["trap"]["kind"] == "none"
    assert snapshot["vgpr"]["1"] == [0] * 32
    assert snapshot["pc"] == [2] * 32
    assert snapshot["counters"]["instructions"] == 32
    assert snapshot["counters"]["warp_instructions"] == 2


def test_native_illegal_pc_is_structured_execute_trap() -> None:
    # No EXIT: after the single instruction runs, the PC steps past the program.
    program = [{"op": "IADD3", "operands": {"dst": "R1", "src0": 1, "src1": 0, "src2": 0}}]

    snapshot = native.step(native.launch(program), 10)

    assert snapshot["trap"]["kind"] == "execute"
    assert snapshot["trap"]["reason"] == "illegal_pc"
    assert snapshot["trap"]["pc"] == 1


def test_native_illegal_branch_target_is_structured_execute_trap() -> None:
    program = [
        {"op": "BRA", "operands": {"target": 5}},
        {"op": "EXIT"},
    ]

    snapshot = native.step(native.launch(program), 10)

    assert snapshot["trap"]["kind"] == "execute"
    assert snapshot["trap"]["reason"] == "illegal_branch_target"
    assert snapshot["trap"]["detail"] == {"target": 5}


def test_native_max_steps_is_structured_trap() -> None:
    program = [{"op": "BRA", "operands": {"target": 0}}]

    snapshot = native.step(native.launch(program), 3)

    assert snapshot["trap"]["kind"] == "max_steps"
    assert snapshot["trap"]["reason"] == "budget_exhausted"
    assert snapshot["counters"]["warp_instructions"] == 3


def test_native_unknown_instruction_is_structured_decode_trap() -> None:
    snapshot = native.step(native.launch([{"op": "UNKNOWN"}]), 1)

    assert snapshot["trap"]["kind"] == "decode"
    assert snapshot["trap"]["reason"] == "unknown_instruction"
    assert snapshot["trap"]["detail"] == {"op": "UNKNOWN"}


def test_native_bad_operand_is_structured_decode_trap() -> None:
    program = [{"op": "IADD3", "operands": {"dst": "R1", "src0": "bad", "src1": 0, "src2": 0}}]

    snapshot = native.step(native.launch(program), 1)

    assert snapshot["trap"]["kind"] == "decode"
    assert snapshot["trap"]["reason"] == "decode_failure"
    assert "unsupported operand" in snapshot["trap"]["detail"]["message"]


def test_native_invalid_isetp_destination_is_structured_decode_trap() -> None:
    program = [{"op": "ISETP", "operands": {"dst": "R1", "src0": 0, "src1": 0}}]

    snapshot = native.step(native.launch(program), 1)

    assert snapshot["trap"]["kind"] == "decode"
    assert snapshot["trap"]["reason"] == "decode_failure"
    assert "invalid predicate destination" in snapshot["trap"]["detail"]["message"]


def test_native_invalid_lop3_lut_is_structured_decode_trap() -> None:
    program = [{"op": "LOP3", "operands": {"dst": "R1", "src0": 0, "src1": 0, "src2": 0, "lut": 256}}]

    snapshot = native.step(native.launch(program), 1)

    assert snapshot["trap"]["kind"] == "decode"
    assert snapshot["trap"]["reason"] == "invalid_lop3_lut"


def test_native_invalid_predicate_guard_is_structured_decode_trap() -> None:
    snapshot = native.step(native.launch([{"op": "EXIT", "guard": "@P7"}]), 1)

    assert snapshot["trap"]["kind"] == "decode"
    assert snapshot["trap"]["reason"] == "decode_failure"
    assert "invalid predicate" in snapshot["trap"]["detail"]["message"]


def test_native_snapshot_is_reproducible_and_diff_is_localized() -> None:
    program = [
        {"op": "IADD3", "operands": {"dst": "R1", "src0": 1, "src1": 2, "src2": 3}},
        {"op": "EXIT"},
    ]

    left = native.step(native.launch(program), 10)
    right = native.step(native.launch(program), 10)
    assert left == right

    mutated = native.step(native.launch(program), 10)
    mutated["vgpr"]["1"][7] = 99
    assert native.state_diff(left, mutated) == [{"path": "$.vgpr.1[7]", "left": 6, "right": 99}]
