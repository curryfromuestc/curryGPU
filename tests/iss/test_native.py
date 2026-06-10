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


def test_native_s2r_laneid_writes_physical_lane_index() -> None:
    program = [
        {"op": "S2R", "operands": {"rd": "R1", "sr": "SR_LANEID"}},
        {"op": "EXIT"},
    ]

    snapshot = native.step(native.launch(program), 10)

    assert snapshot["trap"]["kind"] == "none"
    assert snapshot["vgpr"]["1"] == list(range(32))
    assert snapshot["pc"] == [2] * 32
    assert snapshot["predicates"]["P0"] == [False] * 32
    assert snapshot["memory"] == {"global": {}, "local": {}, "shared": {}}
    assert snapshot["counters"] == {
        "divergence_events": 0,
        "instructions": 64,
        "mem_ops": 0,
        "warp_instructions": 2,
    }


def test_native_launch_words_executes_s2r_laneid() -> None:
    words = [
        assembler.emit("S2R", "R2", "SR_LANEID"),
        assembler.emit("EXIT"),
    ]

    snapshot = native.step(native.launch_words(words), 10)

    assert snapshot["trap"]["kind"] == "none"
    assert snapshot["vgpr"]["2"] == list(range(32))


def test_native_s2r_rz_destination_is_discarded() -> None:
    program = [
        {"op": "S2R", "operands": {"rd": "RZ", "sr": "SR_LANEID"}},
        {"op": "EXIT"},
    ]

    snapshot = native.step(native.launch(program), 10)

    assert snapshot["trap"]["kind"] == "none"
    assert snapshot["vgpr"]["0"] == [0] * 32


def test_native_s2r_guard_false_lanes_do_not_write() -> None:
    lane_values = [lane % 2 for lane in range(32)]
    program = [
        {"op": "IADD3", "operands": {"dst": "R1", "src0": lane_values, "src1": 0, "src2": 0}},
        {"op": "ISETP", "operands": {"dst": "P0", "src0": "R1", "src1": 0, "cmp": "EQ"}},
        {"op": "S2R", "guard": "@P0", "operands": {"rd": "R2", "sr": "SR_LANEID"}},
        {"op": "EXIT"},
    ]

    snapshot = native.step(native.launch(program), 10)

    assert snapshot["trap"]["kind"] == "none"
    assert snapshot["vgpr"]["2"] == [lane if lane % 2 == 0 else 0 for lane in range(32)]


def test_native_s2r_exited_lanes_do_not_write() -> None:
    lane_values = [lane % 2 for lane in range(32)]
    program = [
        {"op": "IADD3", "operands": {"dst": "R1", "src0": lane_values, "src1": 0, "src2": 0}},
        {"op": "ISETP", "operands": {"dst": "P0", "src0": "R1", "src1": 0, "cmp": "EQ"}},
        {"op": "EXIT", "guard": "@P0"},
        {"op": "S2R", "operands": {"rd": "R2", "sr": "SR_LANEID"}},
        {"op": "EXIT"},
    ]

    snapshot = native.step(native.launch(program), 20)

    assert snapshot["trap"]["kind"] == "none"
    assert snapshot["vgpr"]["2"] == [0 if lane % 2 == 0 else lane for lane in range(32)]
    assert snapshot["lane_state"] == ["exited"] * 32


def test_native_s2r_does_not_hide_missing_exit_max_step_trap() -> None:
    program = [
        {"op": "S2R", "operands": {"rd": "R1", "sr": "SR_LANEID"}},
        {"op": "BRA", "operands": {"target": 0}},
    ]

    snapshot = native.step(native.launch(program), 3)

    assert snapshot["trap"]["kind"] == "max_steps"
    assert snapshot["trap"]["reason"] == "budget_exhausted"
    assert snapshot["counters"]["warp_instructions"] == 3


def test_native_s2r_unknown_selector_is_structured_decode_trap() -> None:
    program = [{"op": "S2R", "operands": {"rd": "R1", "sr": "SR_TID_X"}}]

    snapshot = native.step(native.launch(program), 1)

    assert snapshot["trap"]["kind"] == "decode"
    assert snapshot["trap"]["reason"] == "decode_failure"
    assert "special register" in snapshot["trap"]["detail"]["message"]


def test_native_s2r_out_of_range_destination_uses_gpr_bounds_check() -> None:
    program = [{"op": "S2R", "operands": {"rd": "R999", "sr": "SR_LANEID"}}]

    snapshot = native.step(native.launch(program), 1)

    assert snapshot["trap"]["kind"] == "decode"
    assert snapshot["trap"]["reason"] == "decode_failure"
    assert "GPR index out of range" in snapshot["trap"]["detail"]["message"]


def test_native_divergent_branch_runs_both_paths_under_min_pc_first() -> None:
    lane_values = [lane % 2 for lane in range(32)]
    program = [
        {"op": "IADD3", "operands": {"dst": "R1", "src0": lane_values, "src1": 0, "src2": 0}},
        {"op": "ISETP", "operands": {"dst": "P0", "src0": "R1", "src1": 0, "cmp": "EQ"}},
        {"op": "BRA", "guard": "@P0", "operands": {"target": 5}},
        {"op": "IADD3", "operands": {"dst": "R2", "src0": 20, "src1": 0, "src2": 0}},
        {"op": "BRA", "operands": {"target": 6}},
        {"op": "IADD3", "operands": {"dst": "R2", "src0": 10, "src1": 0, "src2": 0}},
        {"op": "EXIT"},
    ]

    snapshot = native.step(native.launch(program, sched_order="min_pc_first"), 20)

    assert snapshot["trap"]["kind"] == "none"
    assert snapshot["active_mask"] == [False] * 32
    assert snapshot["lane_state"] == ["exited"] * 32
    assert snapshot["vgpr"]["2"] == [10 if lane % 2 == 0 else 20 for lane in range(32)]
    assert snapshot["pc"] == [7] * 32
    assert snapshot["counters"]["divergence_events"] > 0


def test_native_divergent_branch_no_longer_traps_on_old_uniformity_reasons() -> None:
    lane_values = [lane % 2 for lane in range(32)]
    program = [
        {"op": "IADD3", "operands": {"dst": "R1", "src0": lane_values, "src1": 0, "src2": 0}},
        {"op": "ISETP", "operands": {"dst": "P0", "src0": "R1", "src1": 0, "cmp": "EQ"}},
        {"op": "BRA", "guard": "@P0", "operands": {"target": 4}},
        {"op": "EXIT"},
        {"op": "EXIT"},
    ]

    snapshot = native.step(native.launch(program), 20)

    assert snapshot["trap"]["kind"] == "none"
    assert snapshot["trap"]["reason"] == ""


def test_native_sched_order_argument_accepts_ac2_orders() -> None:
    program = [{"op": "EXIT"}]

    for order in ("min_pc_first", "max_pc_first", "round_robin", "oldest_group_first"):
        snapshot = native.step(native.launch(program, sched_order=order), 3)
        assert snapshot["trap"]["kind"] == "none"


def test_native_bssy_bsync_diamond_reconverges() -> None:
    lane_values = [lane % 2 for lane in range(32)]
    program = [
        {"op": "IADD3", "operands": {"dst": "R1", "src0": lane_values, "src1": 0, "src2": 0}},
        {"op": "ISETP", "operands": {"dst": "P0", "src0": "R1", "src1": 0, "cmp": "EQ"}},
        {"op": "BSSY", "operands": {"bar": "B0", "target": 7}},
        {"op": "BRA", "guard": "@P0", "operands": {"target": 6}},
        {"op": "IADD3", "operands": {"dst": "R2", "src0": 20, "src1": 0, "src2": 0}},
        {"op": "BRA", "operands": {"target": 7}},
        {"op": "IADD3", "operands": {"dst": "R2", "src0": 10, "src1": 0, "src2": 0}},
        {"op": "BSYNC", "operands": {"bar": "B0"}},
        {"op": "IADD3", "operands": {"dst": "R3", "src0": "R2", "src1": 1, "src2": 0}},
        {"op": "EXIT"},
    ]

    snapshot = native.step(native.launch(program), 40)

    assert snapshot["trap"]["kind"] == "none"
    assert snapshot["vgpr"]["2"] == [10 if lane % 2 == 0 else 20 for lane in range(32)]
    assert snapshot["vgpr"]["3"] == [11 if lane % 2 == 0 else 21 for lane in range(32)]
    assert snapshot["lane_state"] == ["exited"] * 32
    assert snapshot["bx"]["barriers"][0] == {"participation_mask": 0, "reconv_pc": 0, "valid": False}


def test_native_nested_barrier_waits_on_matching_blocked_on() -> None:
    lane_values = [lane % 2 for lane in range(32)]
    program = [
        {"op": "IADD3", "operands": {"dst": "R1", "src0": lane_values, "src1": 0, "src2": 0}},
        {"op": "ISETP", "operands": {"dst": "P0", "src0": "R1", "src1": 0, "cmp": "EQ"}},
        {"op": "BSSY", "operands": {"bar": "B0", "target": 12}},
        {"op": "BSSY", "operands": {"bar": "B1", "target": 8}},
        {"op": "BRA", "guard": "@P0", "operands": {"target": 7}},
        {"op": "IADD3", "operands": {"dst": "R2", "src0": 20, "src1": 0, "src2": 0}},
        {"op": "BRA", "operands": {"target": 8}},
        {"op": "IADD3", "operands": {"dst": "R2", "src0": 10, "src1": 0, "src2": 0}},
        {"op": "BSYNC", "operands": {"bar": "B1"}},
        {"op": "IADD3", "operands": {"dst": "R3", "src0": "R2", "src1": 1, "src2": 0}},
        {"op": "BRA", "operands": {"target": 12}},
        {"op": "IADD3", "operands": {"dst": "R4", "src0": 99, "src1": 0, "src2": 0}},
        {"op": "BSYNC", "operands": {"bar": "B0"}},
        {"op": "IADD3", "operands": {"dst": "R5", "src0": "R3", "src1": 1, "src2": 0}},
        {"op": "EXIT"},
    ]

    snapshot = native.step(native.launch(program), 60)

    assert snapshot["trap"]["kind"] == "none"
    assert snapshot["vgpr"]["4"] == [0] * 32
    assert snapshot["vgpr"]["5"] == [12 if lane % 2 == 0 else 22 for lane in range(32)]
    assert snapshot["bx"]["barriers"][0] == {"participation_mask": 0, "reconv_pc": 0, "valid": False}
    assert snapshot["bx"]["barriers"][1] == {"participation_mask": 0, "reconv_pc": 0, "valid": False}


def test_native_break_dissolved_barrier_allows_bsync_fallthrough() -> None:
    program = [
        {"op": "BSSY", "operands": {"bar": "B2", "target": 3}},
        {"op": "BREAK", "operands": {"bar": "B2"}},
        {"op": "BSYNC", "operands": {"bar": "B2"}},
        {"op": "EXIT"},
    ]

    snapshot = native.step(native.launch(program), 10)

    assert snapshot["trap"]["kind"] == "none"
    assert snapshot["lane_state"] == ["exited"] * 32
    assert snapshot["bx"]["barriers"][2] == {"participation_mask": 0, "reconv_pc": 0, "valid": False}


def test_native_yield_promotes_when_no_active_group_remains() -> None:
    program = [
        {"op": "YIELD"},
        {"op": "IADD3", "operands": {"dst": "R1", "src0": 7, "src1": 0, "src2": 0}},
        {"op": "EXIT"},
    ]

    snapshot = native.step(native.launch(program), 10)

    assert snapshot["trap"]["kind"] == "none"
    assert snapshot["vgpr"]["1"] == [7] * 32
    assert snapshot["lane_state"] == ["exited"] * 32


def test_native_bsync_unarmed_traps_convergence() -> None:
    snapshot = native.step(native.launch([{"op": "BSYNC", "operands": {"bar": "B0"}}]), 5)

    assert snapshot["trap"]["kind"] == "convergence"
    assert snapshot["trap"]["reason"] == "bsync_invalid_barrier"
    assert snapshot["trap"]["detail"]["barrier_index"] == 0


def test_native_bssy_clobber_traps_convergence() -> None:
    program = [
        {"op": "BSSY", "operands": {"bar": "B0", "target": 3}},
        {"op": "BSSY", "operands": {"bar": "B0", "target": 3}},
        {"op": "EXIT"},
        {"op": "EXIT"},
    ]

    snapshot = native.step(native.launch(program), 5)

    assert snapshot["trap"]["kind"] == "convergence"
    assert snapshot["trap"]["reason"] == "bssy_clobbers_live_barrier"
    assert snapshot["trap"]["detail"]["barrier_index"] == 0


def test_native_bssy_illegal_reconv_pc_traps_convergence() -> None:
    snapshot = native.step(native.launch([{"op": "BSSY", "operands": {"bar": "B0", "target": 99}}]), 5)

    assert snapshot["trap"]["kind"] == "convergence"
    assert snapshot["trap"]["reason"] == "illegal_reconv_pc"
    assert snapshot["trap"]["detail"]["target"] == 99


def test_native_predicated_bssy_and_bsync_are_rejected() -> None:
    bssy = native.step(native.launch([{"op": "BSSY", "guard": "@P0", "operands": {"bar": "B0", "target": 1}}]), 5)
    bsync = native.step(
        native.launch(
            [
                {"op": "BSSY", "operands": {"bar": "B0", "target": 2}},
                {"op": "BSYNC", "guard": "@P0", "operands": {"bar": "B0"}},
                {"op": "EXIT"},
            ]
        ),
        5,
    )

    assert bssy["trap"]["kind"] == "convergence"
    assert bssy["trap"]["reason"] == "predicated_barrier_unsupported"
    assert bsync["trap"]["kind"] == "convergence"
    assert bsync["trap"]["reason"] == "predicated_barrier_unsupported"


def test_native_bssy_out_of_range_barrier_traps_slots_exhausted() -> None:
    program = [
        {"op": "BSSY", "operands": {"bar": "B16", "target": 1}},
        {"op": "EXIT"},
    ]

    snapshot = native.step(native.launch(program), 5)

    assert snapshot["trap"]["kind"] == "convergence"
    assert snapshot["trap"]["reason"] == "barrier_slots_exhausted"
    assert snapshot["trap"]["detail"]["barrier_index"] == 16


def test_native_cross_blocked_barriers_trap_deadlock_no_progress() -> None:
    program = [
        {"op": "ELECT", "operands": {"pd": "P0", "membermask": 0xFFFFFFFF}},
        {"op": "BSSY", "operands": {"bar": "B0", "target": 6}},
        {"op": "BSSY", "operands": {"bar": "B1", "target": 5}},
        {"op": "BRA", "guard": "@P0", "operands": {"target": 5}},
        {"op": "BSYNC", "operands": {"bar": "B1"}},
        {"op": "BSYNC", "operands": {"bar": "B0"}},
        {"op": "EXIT"},
    ]

    snapshot = native.step(native.launch(program), 20)

    assert snapshot["trap"]["kind"] == "convergence"
    assert snapshot["trap"]["reason"] == "deadlock_no_progress"


def test_native_elect_selects_unique_subwarp_leader_and_preserves_nonmembers() -> None:
    program = [
        {"op": "ISETP", "operands": {"dst": "P1", "src0": 0, "src1": 0, "cmp": "EQ"}},
        {"op": "ELECT", "operands": {"pd": "P1", "membermask": 0x0000000F}},
        {"op": "EXIT"},
    ]

    snapshot = native.step(native.launch(program), 10)

    assert snapshot["trap"]["kind"] == "none"
    assert snapshot["predicates"]["P1"][:8] == [True, False, False, False, True, True, True, True]


def test_native_vote_any_all_eq_and_ballot_are_observable() -> None:
    lane_values = [lane % 2 for lane in range(32)]
    program = [
        {"op": "IADD3", "operands": {"dst": "R1", "src0": lane_values, "src1": 0, "src2": 0}},
        {"op": "ISETP", "operands": {"dst": "P0", "src0": "R1", "src1": 0, "cmp": "EQ"}},
        {"op": "VOTE", "operands": {"pd": "P1", "src": "P0", "membermask": 0x0000000F, "mode": "ANY"}},
        {"op": "VOTE", "operands": {"pd": "P2", "src": "P0", "membermask": 0x0000000F, "mode": "ALL"}},
        {"op": "VOTE", "operands": {"pd": "P3", "src": "P0", "membermask": 0x0000000F, "mode": "EQ"}},
        {"op": "VOTE", "operands": {"pd": "P4", "src": "P0", "membermask": 0x0000000F, "mode": "BALLOT", "rd": "R2"}},
        {"op": "EXIT"},
    ]

    snapshot = native.step(native.launch(program), 20)

    assert snapshot["trap"]["kind"] == "none"
    assert snapshot["predicates"]["P1"][:6] == [True, True, True, True, False, False]
    assert snapshot["predicates"]["P2"][:6] == [False, False, False, False, False, False]
    assert snapshot["predicates"]["P3"][:6] == [False, False, False, False, False, False]
    assert snapshot["predicates"]["P4"][:6] == [True, True, True, True, False, False]
    assert snapshot["vgpr"]["2"][:6] == [0b0101, 0b0101, 0b0101, 0b0101, 0, 0]


def test_native_collective_membermask_not_subset_traps() -> None:
    program = [
        {"op": "ELECT", "guard": "@P0", "operands": {"pd": "P1", "membermask": 0x1}},
    ]

    snapshot = native.step(native.launch(program), 5)

    assert snapshot["trap"]["kind"] == "convergence"
    assert snapshot["trap"]["reason"] == "membermask_not_subset"


def test_native_collective_zero_membermask_traps_self_not_in_membermask() -> None:
    snapshot = native.step(native.launch([{"op": "ELECT", "operands": {"pd": "P1", "membermask": 0}}]), 5)

    assert snapshot["trap"]["kind"] == "convergence"
    assert snapshot["trap"]["reason"] == "self_not_in_membermask"


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
