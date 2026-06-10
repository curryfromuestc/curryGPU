from __future__ import annotations

import pytest

from currygpu.iss import native
from currygpu.isa import assembler
from its_corpus import architectural_subset


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


def test_native_state_diff_localizes_memory_byte() -> None:
    left = {"memory": {"global": {"0x0": [0, 1, 2]}}}
    right = {"memory": {"global": {"0x0": [0, 9, 2]}}}

    assert native.state_diff(left, right) == [{"path": "$.memory.global.0x0[1]", "left": 1, "right": 9}]


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


def test_native_s2r_geometry_selectors_single_warp() -> None:
    words = [
        assembler.emit("S2R", "R1", "SR_TID.X"),
        assembler.emit("S2R", "R2", "SR_TID.Y"),
        assembler.emit("S2R", "R3", "SR_NTID.X"),
        assembler.emit("S2R", "R4", "SR_NTID.Y"),
        assembler.emit("S2R", "R5", "SR_NTID.Z"),
        assembler.emit("S2R", "R6", "SR_CTAID.X"),
        assembler.emit("S2R", "R7", "SR_NCTAID.X"),
        assembler.emit("S2R", "R8", "SR_WARPID"),
        assembler.emit("S2R", "R9", "SR_NWARPID"),
        assembler.emit("EXIT"),
    ]

    snapshot = native.step(native.launch_words(words, ntid=(8, 4, 1)), 20)

    assert snapshot["trap"]["kind"] == "none"
    assert snapshot["vgpr"]["1"] == [lane % 8 for lane in range(32)]
    assert snapshot["vgpr"]["2"] == [(lane // 8) % 4 for lane in range(32)]
    assert snapshot["vgpr"]["3"] == [8] * 32
    assert snapshot["vgpr"]["4"] == [4] * 32
    assert snapshot["vgpr"]["5"] == [1] * 32
    assert snapshot["vgpr"]["6"] == [0] * 32
    assert snapshot["vgpr"]["7"] == [1] * 32
    assert snapshot["vgpr"]["8"] == [0] * 32
    assert snapshot["vgpr"]["9"] == [1] * 32


def test_native_launch_accepts_new_single_warp_kwargs_and_rejects_invalid_geometry() -> None:
    snapshot = native.step(
        native.launch(
            [{"op": "EXIT"}],
            num_warps=1,
            warp_sched_order="warp_min_id_first",
            ntid=(16, 2, 1),
            nctaid=(1, 1, 1),
            race_check=True,
        ),
        5,
    )
    assert snapshot["trap"]["kind"] == "none"
    assert len(snapshot["cta_barriers"]) == 16

    with pytest.raises(ValueError, match="prod\\(ntid\\)"):
        native.launch([{"op": "EXIT"}], ntid=(31, 1, 1))
    with pytest.raises(ValueError, match="unknown warp_sched_order"):
        native.launch([{"op": "EXIT"}], warp_sched_order="bogus")

    grid_snapshot = native.step(native.launch([{"op": "EXIT"}], nctaid=(2, 1, 1)), 10)
    assert grid_snapshot["trap"]["kind"] == "none"
    assert len(grid_snapshot["ctas"]) == 2


def test_native_launch_words_executes_multi_warp_s2r_geometry() -> None:
    words = [
        assembler.emit("S2R", "R1", "SR_WARPID"),
        assembler.emit("S2R", "R2", "SR_NWARPID"),
        assembler.emit("S2R", "R3", "SR_TID.X"),
        assembler.emit("EXIT"),
    ]

    snapshot = native.step(native.launch_words(words, num_warps=2, ntid=(64, 1, 1)), 20)

    assert snapshot["trap"]["kind"] == "none"
    assert len(snapshot["warps"]) == 2
    assert snapshot["warps"][0]["vgpr"]["1"] == [0] * 32
    assert snapshot["warps"][1]["vgpr"]["1"] == [1] * 32
    assert snapshot["warps"][0]["vgpr"]["2"] == [2] * 32
    assert snapshot["warps"][1]["vgpr"]["2"] == [2] * 32
    assert snapshot["warps"][0]["vgpr"]["3"] == list(range(32))
    assert snapshot["warps"][1]["vgpr"]["3"] == list(range(32, 64))
    assert snapshot["counters"]["warp_instructions"] == 8
    assert len(snapshot["cta_barriers"]) == 16
    assert snapshot["memory"] == {"global": {}, "local": {}, "shared": {}}


def test_architectural_subset_projects_multi_warp_cta_shape() -> None:
    words = [
        assembler.emit("S2R", "R1", "SR_WARPID"),
        assembler.emit("EXIT"),
    ]

    subset = architectural_subset(native.step(native.launch_words(words, num_warps=2), 10))

    assert set(subset) == {"warps", "memory", "trap", "cta_barriers"}
    assert len(subset["warps"]) == 2
    assert set(subset["warps"][0]) == {
        "active_mask",
        "pc",
        "lane_state",
        "vgpr",
        "predicates",
        "uniform_registers",
        "bx",
    }
    assert subset["warps"][1]["vgpr"]["1"] == [1] * 32


def test_native_multi_warp_shared_memory_is_cta_scoped() -> None:
    words = [
        assembler.emit("S2R", "R2", "SR_WARPID"),
        assembler.emit("IADD3", "R1", "R2", "RZ", 1),
        assembler.emit("STS", "R1", "R2", width="u8"),
        assembler.emit("EXIT"),
    ]

    snapshot = native.step(
        native.launch_words(words, num_warps=2, ntid=(64, 1, 1), warp_sched_order="warp_max_id_first"),
        20,
    )

    assert snapshot["trap"]["kind"] == "none"
    assert len(snapshot["warps"]) == 2
    assert snapshot["warps"][0]["vgpr"]["1"] == [1] * 32
    assert snapshot["warps"][1]["vgpr"]["1"] == [2] * 32
    assert snapshot["memory"]["shared"]["0x0"][:2] == [1, 2]
    assert snapshot["memory"]["local"] == {}
    assert snapshot["counters"]["mem_ops"] == 64


def test_native_bar_sync_releases_after_all_warps_arrive() -> None:
    words = [
        assembler.emit("S2R", "R1", "SR_WARPID"),
        assembler.emit("BAR", "B0", mode="sync"),
        assembler.emit("IADD3", "R2", "R1", "RZ", 10),
        assembler.emit("EXIT"),
    ]

    partial = native.step(native.launch_words(words, num_warps=2, ntid=(64, 1, 1)), 1)
    assert partial["trap"]["kind"] == "max_steps"
    assert partial["warps"][0]["pc"] == [1] * 32
    assert partial["warps"][1]["pc"] == [0] * 32

    snapshot = native.step(native.launch_words(words, num_warps=2, ntid=(64, 1, 1)), 20)

    assert snapshot["trap"]["kind"] == "none"
    assert snapshot["warps"][0]["vgpr"]["2"] == [10] * 32
    assert snapshot["warps"][1]["vgpr"]["2"] == [11] * 32
    assert snapshot["cta_barriers"][0] == {
        "phase": "inactive",
        "arrived_count": 0,
        "expected_count": 0,
        "phase_parity": 1,
    }


def test_native_bar_sync_dynamic_expected_excludes_exited_threads() -> None:
    words = [
        assembler.emit("S2R", "R1", "SR_WARPID"),
        assembler.emit("ISETP", "P0", "R1", "RZ", cmp="eq"),
        assembler.emit("EXIT", guard="P0"),
        assembler.emit("BAR", "B0", mode="sync"),
        assembler.emit("IADD3", "R2", "R1", "RZ", 7),
        assembler.emit("EXIT"),
    ]

    snapshot = native.step(native.launch_words(words, num_warps=2, ntid=(64, 1, 1)), 20)

    assert snapshot["trap"]["kind"] == "none"
    assert snapshot["warps"][0]["active_mask"] == [False] * 32
    assert snapshot["warps"][1]["vgpr"]["2"] == [8] * 32
    assert snapshot["cta_barriers"][0]["phase"] == "inactive"


def test_native_bar_explicit_count_does_not_exclude_exited_threads() -> None:
    words = [
        assembler.emit("S2R", "R1", "SR_WARPID"),
        assembler.emit("ISETP", "P0", "R1", "RZ", cmp="eq"),
        assembler.emit("EXIT", guard="P0"),
        assembler.emit("BAR", "B0", 64, mode="sync"),
        assembler.emit("EXIT"),
    ]

    snapshot = native.step(native.launch_words(words, num_warps=2, ntid=(64, 1, 1)), 20)

    assert snapshot["trap"]["kind"] == "synchronization"
    assert snapshot["trap"]["reason"] == "barrier_deadlock"
    assert snapshot["trap"]["detail"]["bar_id"] == 0
    assert snapshot["cta_barriers"][0]["phase"] == "gathering"
    assert snapshot["cta_barriers"][0]["arrived_count"] == 32
    assert snapshot["cta_barriers"][0]["expected_count"] == 64


def test_native_bar_dynamic_barrier_id_out_of_range_traps() -> None:
    # The 4-bit encoded bar field cannot express ids above 15, so the
    # dict-program path is the only route into the runtime range check.
    program = [
        {"op": "BAR", "operands": {"bar": "B16"}},
        {"op": "EXIT"},
    ]

    snapshot = native.step(native.launch(program), 10)

    assert snapshot["trap"]["kind"] == "synchronization"
    assert snapshot["trap"]["reason"] == "barrier_id_out_of_range"
    assert snapshot["trap"]["detail"]["bar_id"] == 16


def test_native_bar_arv_arrives_without_blocking_and_releases_sync_waiter() -> None:
    words = [
        assembler.emit("S2R", "R1", "SR_WARPID"),
        assembler.emit("ISETP", "P0", "R1", "RZ", cmp="eq"),
        assembler.emit("BAR", "B0", 64, mode="arv", guard="P0"),
        assembler.emit("BAR", "B0", 64, mode="sync", guard="P0", guard_neg=True),
        assembler.emit("IADD3", "R2", "R1", "RZ", 3),
        assembler.emit("EXIT"),
    ]

    snapshot = native.step(native.launch_words(words, num_warps=2, ntid=(64, 1, 1)), 20)

    assert snapshot["trap"]["kind"] == "none"
    assert snapshot["warps"][0]["vgpr"]["2"] == [3] * 32
    assert snapshot["warps"][1]["vgpr"]["2"] == [4] * 32
    assert snapshot["cta_barriers"][0]["phase"] == "inactive"


def test_native_bar_invalid_runtime_count_traps() -> None:
    program = [
        {"op": "BAR", "operands": {"bar": "B0", "count": 33, "mode": "SYNC"}},
        {"op": "EXIT"},
    ]

    snapshot = native.step(native.launch(program, num_warps=1), 10)

    assert snapshot["trap"]["kind"] == "synchronization"
    assert snapshot["trap"]["reason"] == "barrier_count_not_warp_multiple"
    assert snapshot["trap"]["detail"]["bar_id"] == 0


def test_native_atomg_add_returns_old_values_and_updates_global() -> None:
    words = [
        assembler.emit("S2R", "R1", "SR_LANEID"),
        assembler.emit("IADD3", "R2", "RZ", "RZ", 1),
        assembler.emit("ATOMG", "R4", ("RZ", 0), "R2", op="add"),
        assembler.emit("EXIT"),
    ]

    snapshot = native.step(native.launch_words(words, global_allocations=[(0, 4)]), 20)

    assert snapshot["trap"]["kind"] == "none"
    assert snapshot["vgpr"]["4"] == list(range(32))
    assert snapshot["memory"]["global"]["0x0"][:4] == [32, 0, 0, 0]
    assert snapshot["counters"]["mem_ops"] == 32


def test_native_redg_xor_updates_without_writing_registers() -> None:
    words = [
        assembler.emit("IADD3", "R1", "RZ", "RZ", 0x5A),
        assembler.emit("IADD3", "R4", "RZ", "RZ", 123),
        assembler.emit("REDG", ("RZ", 0), "R1", op="xor"),
        assembler.emit("EXIT"),
    ]

    snapshot = native.step(native.launch_words(words, global_allocations=[(0, 4)]), 20)

    assert snapshot["trap"]["kind"] == "none"
    assert snapshot["vgpr"]["4"] == [123] * 32
    assert snapshot["memory"] == {"global": {}, "local": {}, "shared": {}}
    assert snapshot["counters"]["mem_ops"] == 32


def test_native_atoms_cas_serializes_by_lane_id() -> None:
    words = [
        assembler.emit("IADD3", "R1", "RZ", "RZ", 0),
        assembler.emit("IADD3", "R2", "RZ", "RZ", 99),
        assembler.emit("IADD3", "R3", "RZ", "RZ", 0),
        assembler.emit("ATOMS", "R4", ("RZ", 0), "R2", op="cas", cmp="R3"),
        assembler.emit("EXIT"),
    ]

    snapshot = native.step(native.launch_words(words), 20)

    assert snapshot["trap"]["kind"] == "none"
    assert snapshot["vgpr"]["4"][0] == 0
    assert snapshot["vgpr"]["4"][1:] == [99] * 31
    assert snapshot["memory"]["shared"]["0x0"][:4] == [99, 0, 0, 0]


def test_native_atomic_on_local_and_misaligned_traps() -> None:
    local_program = [
        {"op": "ATOM", "operands": {"rd": "R1", "addr": {"base": "RZ", "imm": 0x2000000000000000}, "src": "R2", "op": "ADD"}},
        {"op": "EXIT"},
    ]
    local_snapshot = native.step(native.launch(local_program), 10)

    assert local_snapshot["trap"]["kind"] == "memory"
    assert local_snapshot["trap"]["reason"] == "atomic_on_local_unsupported"

    words = [
        assembler.emit("IADD3", "R1", "RZ", "RZ", 1),
        assembler.emit("ATOMG", "R2", ("RZ", 1), "R1", op="add"),
        assembler.emit("EXIT"),
    ]
    misaligned_snapshot = native.step(native.launch_words(words, global_allocations=[(0, 8)]), 10)

    assert misaligned_snapshot["trap"]["kind"] == "memory"
    assert misaligned_snapshot["trap"]["reason"] == "atomic_misaligned"


@pytest.mark.parametrize(
    ("op", "old", "src", "cmp", "expected"),
    [
        ("add", 5, 3, 0, 8),
        ("min", 5, 3, 0, 3),
        ("max", 5, 7, 0, 7),
        ("inc", 5, 5, 0, 0),
        ("dec", 0, 9, 0, 9),
        ("and", 0x0C, 0x0A, 0, 0x08),
        ("or", 0x0C, 0x0A, 0, 0x0E),
        ("xor", 0x0C, 0x0A, 0, 0x06),
        ("exch", 5, 3, 0, 3),
        ("cas", 5, 9, 5, 9),
    ],
)
def test_native_atomg_single_lane_integer_ops(op: str, old: int, src: int, cmp: int, expected: int) -> None:
    words = [
        assembler.emit("IADD3", "R1", "RZ", "RZ", old),
        assembler.emit("STG", "R1", ("RZ", 0), width="32"),
        assembler.emit("ELECT", "P0", 0xFFFFFFFF),
        assembler.emit("IADD3", "R2", "RZ", "RZ", src),
        assembler.emit("IADD3", "R3", "RZ", "RZ", cmp),
        assembler.emit("ATOMG", "R4", ("RZ", 0), "R2", op=op, cmp="R3", guard="P0"),
        assembler.emit("EXIT"),
    ]

    snapshot = native.step(native.launch_words(words, global_allocations=[(0, 4)]), 20)

    assert snapshot["trap"]["kind"] == "none"
    assert snapshot["vgpr"]["4"][0] == old
    assert snapshot["vgpr"]["4"][1:] == [0] * 31
    assert snapshot["memory"]["global"].get("0x0", [0, 0, 0, 0])[:4] == [expected, 0, 0, 0]


def test_native_atomic_runtime_rejects_red_destination_and_unknown_op() -> None:
    red_snapshot = native.step(
        native.launch([{"op": "REDG", "operands": {"rd": "R1", "addr": {"base": "RZ", "imm": 0}, "src": "R2", "op": "ADD"}}]),
        10,
    )

    assert red_snapshot["trap"]["kind"] == "memory"
    assert red_snapshot["trap"]["reason"] == "red_has_destination"

    unsupported_snapshot = native.step(
        native.launch([{"op": "ATOMG", "operands": {"rd": "R1", "addr": {"base": "RZ", "imm": 0}, "src": "R2", "op": "BAD"}}]),
        10,
    )

    assert unsupported_snapshot["trap"]["kind"] == "memory"
    assert unsupported_snapshot["trap"]["reason"] == "atomic_unsupported_op"


def test_native_multi_warp_red_add_is_schedule_independent() -> None:
    words = [
        assembler.emit("IADD3", "R1", "RZ", "RZ", 1),
        assembler.emit("REDG", ("RZ", 0), "R1", op="add"),
        assembler.emit("EXIT"),
    ]

    finals = []
    for warp_sched_order in ("warp_round_robin", "warp_min_id_first", "warp_max_id_first"):
        snapshot = native.step(
            native.launch_words(
                words,
                num_warps=2,
                ntid=(64, 1, 1),
                warp_sched_order=warp_sched_order,
                global_allocations=[(0, 4)],
            ),
            20,
        )
        assert snapshot["trap"]["kind"] == "none"
        finals.append(snapshot["memory"]["global"]["0x0"][:4])

    assert finals == [[64, 0, 0, 0]] * 3


def test_native_order_sensitive_exch_is_fixed_schedule_deterministic() -> None:
    words = [
        assembler.emit("S2R", "R1", "SR_WARPID"),
        assembler.emit("IADD3", "R1", "R1", "RZ", 1),
        assembler.emit("ATOMG", "R4", ("RZ", 0), "R1", op="exch"),
        assembler.emit("EXIT"),
    ]

    first = native.step(
        native.launch_words(words, num_warps=2, ntid=(64, 1, 1), warp_sched_order="warp_min_id_first", global_allocations=[(0, 4)]),
        20,
    )
    second = native.step(
        native.launch_words(words, num_warps=2, ntid=(64, 1, 1), warp_sched_order="warp_min_id_first", global_allocations=[(0, 4)]),
        20,
    )
    opposite = native.step(
        native.launch_words(words, num_warps=2, ntid=(64, 1, 1), warp_sched_order="warp_max_id_first", global_allocations=[(0, 4)]),
        20,
    )

    assert first["trap"]["kind"] == "none"
    assert second["trap"]["kind"] == "none"
    assert opposite["trap"]["kind"] == "none"
    assert first["memory"] == second["memory"]
    assert first["memory"]["global"]["0x0"][:4] != opposite["memory"]["global"]["0x0"][:4]


def test_native_grid_executes_ctas_in_order_with_shared_global_memory() -> None:
    words = [
        assembler.emit("S2R", "R4", "SR_CTAID.X"),
        assembler.emit("S2R", "R3", "SR_NCTAID.X"),
        assembler.emit("IADD3", "R2", "R4", "RZ", 1),
        assembler.emit("STG", "R2", "R4", width="u8"),
        assembler.emit("STS", "R2", ("RZ", 0), width="u8"),
        assembler.emit("EXIT"),
    ]

    snapshot = native.step(native.launch_words(words, nctaid=(2, 1, 1), global_allocations=[(0, 2)]), 20)

    assert snapshot["trap"]["kind"] == "none"
    assert snapshot["memory"]["global"]["0x0"][:2] == [1, 2]
    assert len(snapshot["ctas"]) == 2
    assert "global" not in snapshot["ctas"][0]["memory"]
    assert snapshot["ctas"][0]["vgpr"]["4"] == [0] * 32
    assert snapshot["ctas"][1]["vgpr"]["4"] == [1] * 32
    assert snapshot["ctas"][0]["vgpr"]["3"] == [2] * 32
    assert snapshot["ctas"][1]["vgpr"]["3"] == [2] * 32
    assert snapshot["ctas"][0]["memory"]["shared"]["0x0"][0] == 1
    assert snapshot["ctas"][1]["memory"]["shared"]["0x0"][0] == 2


def test_native_grid_stops_at_first_cta_trap() -> None:
    words = [
        assembler.emit("LDG", "R1", ("RZ", 4), width="32"),
        assembler.emit("EXIT"),
    ]

    snapshot = native.step(native.launch_words(words, nctaid=(2, 1, 1), global_allocations=[(0, 4)]), 20)

    assert snapshot["trap"]["kind"] == "memory"
    assert snapshot["trap"]["reason"] == "global_oob"
    assert len(snapshot["ctas"]) == 2
    assert snapshot["ctas"][0]["trap"]["kind"] == "memory"
    assert snapshot["ctas"][1]["pc"] == [0] * 32
    assert snapshot["ctas"][1]["memory"]["shared"] == {}


def test_native_race_check_default_off_preserves_racy_execution() -> None:
    words = [
        assembler.emit("S2R", "R1", "SR_WARPID"),
        assembler.emit("S2R", "R4", "SR_LANEID"),
        assembler.emit("IADD3", "R2", "R1", "RZ", 1),
        assembler.emit("STG", "R2", "R4", width="u8"),
        assembler.emit("EXIT"),
    ]

    first = native.step(
        native.launch_words(
            words,
            num_warps=2,
            ntid=(64, 1, 1),
            warp_sched_order="warp_min_id_first",
            global_allocations=[(0, 32)],
        ),
        20,
    )
    second = native.step(
        native.launch_words(
            words,
            num_warps=2,
            ntid=(64, 1, 1),
            warp_sched_order="warp_min_id_first",
            global_allocations=[(0, 32)],
        ),
        20,
    )

    assert first["trap"]["kind"] == "none"
    assert second["trap"]["kind"] == "none"
    assert first["memory"] == second["memory"]
    assert first["memory"]["global"]["0x0"][:32] == [2] * 32


def test_native_race_check_traps_on_cross_warp_write_conflict() -> None:
    words = [
        assembler.emit("S2R", "R1", "SR_WARPID"),
        assembler.emit("S2R", "R4", "SR_LANEID"),
        assembler.emit("IADD3", "R2", "R1", "RZ", 1),
        assembler.emit("STG", "R2", "R4", width="u8"),
        assembler.emit("EXIT"),
    ]

    snapshot = native.step(
        native.launch_words(
            words,
            num_warps=2,
            ntid=(64, 1, 1),
            warp_sched_order="warp_min_id_first",
            global_allocations=[(0, 32)],
            race_check=True,
        ),
        20,
    )

    assert snapshot["trap"]["kind"] == "memory"
    assert snapshot["trap"]["reason"] == "data_race"
    assert snapshot["trap"]["detail"]["address"] == 0
    assert snapshot["trap"]["detail"]["space"] == "global"
    assert snapshot["trap"]["detail"]["racing_lanes"] == [0, 32]
    assert snapshot["trap"]["detail"]["access_kinds"] == ["write", "write"]


def test_native_race_check_barrier_release_separates_epochs() -> None:
    words = [
        assembler.emit("S2R", "R1", "SR_WARPID"),
        assembler.emit("S2R", "R4", "SR_LANEID"),
        assembler.emit("IADD3", "R2", "R1", "RZ", 1),
        assembler.emit("ISETP", "P0", "R1", "RZ", cmp="eq"),
        assembler.emit("STS", "R2", "R4", width="u8", guard="P0"),
        assembler.emit("BAR", "B0", mode="sync"),
        assembler.emit("STS", "R2", "R4", width="u8", guard="P0", guard_neg=True),
        assembler.emit("EXIT"),
    ]

    unchecked = native.step(native.launch_words(words, num_warps=2, ntid=(64, 1, 1)), 30)
    checked = native.step(native.launch_words(words, num_warps=2, ntid=(64, 1, 1), race_check=True), 30)

    assert checked["trap"]["kind"] == "none"
    assert checked["memory"] == unchecked["memory"]
    assert checked["memory"]["shared"]["0x0"][:32] == [2] * 32


def test_native_race_check_does_not_report_atomic_conflicts() -> None:
    words = [
        assembler.emit("IADD3", "R1", "RZ", "RZ", 1),
        assembler.emit("REDG", ("RZ", 0), "R1", op="add"),
        assembler.emit("EXIT"),
    ]

    snapshot = native.step(
        native.launch_words(words, num_warps=2, ntid=(64, 1, 1), race_check=True, global_allocations=[(0, 4)]),
        20,
    )

    assert snapshot["trap"]["kind"] == "none"
    assert snapshot["memory"]["global"]["0x0"][:4] == [64, 0, 0, 0]


def test_native_race_check_ignores_guard_false_lanes() -> None:
    words = [
        assembler.emit("S2R", "R1", "SR_WARPID"),
        assembler.emit("S2R", "R4", "SR_LANEID"),
        assembler.emit("IADD3", "R2", "R1", "RZ", 1),
        assembler.emit("ISETP", "P0", "R1", "RZ", cmp="eq"),
        assembler.emit("STG", "R2", "R4", width="u8", guard="P0"),
        assembler.emit("ISETP", "P1", "RZ", "RZ", cmp="ne"),
        assembler.emit("STG", "R2", "R4", width="u8", guard="P1"),
        assembler.emit("EXIT"),
    ]

    snapshot = native.step(
        native.launch_words(words, num_warps=2, ntid=(64, 1, 1), race_check=True, global_allocations=[(0, 32)]),
        30,
    )

    assert snapshot["trap"]["kind"] == "none"
    assert snapshot["memory"]["global"]["0x0"][:32] == [1] * 32


def test_native_launch_words_executes_encoded_memory_and_fence_noop() -> None:
    words = [
        assembler.emit("IADD3", "R1", "RZ", "RZ", 0x44),
        assembler.emit("STG", "R1", ("RZ", 0), width="u8"),
        assembler.emit("MEMBAR", scope="sys", order="sc"),
        assembler.emit("FENCE", scope="cta", order="acquire"),
        assembler.emit("LDG", "R2", ("RZ", 0), width="u8"),
        assembler.emit("EXIT"),
    ]

    snapshot = native.step(native.launch_words(words, global_allocations=[(0, 128)]), 10)

    assert snapshot["trap"]["kind"] == "none"
    assert snapshot["vgpr"]["2"] == [0x44] * 32
    assert snapshot["memory"]["global"]["0x0"][0] == 0x44


def test_native_direct_global_store_and_load_word_roundtrip() -> None:
    program = [
        {"op": "IADD3", "operands": {"dst": "R1", "src0": "RZ", "src1": "RZ", "src2": 0x12345678}},
        {"op": "STG", "operands": {"src": "R1", "addr": {"base": "RZ", "imm": 0}, "width": "32"}},
        {"op": "LDG", "operands": {"rd": "R2", "addr": {"base": "RZ", "imm": 0}, "width": "32"}},
        {"op": "EXIT"},
    ]

    snapshot = native.step(native.launch(program, global_allocations=[(0, 128)]), 10)

    assert snapshot["trap"]["kind"] == "none"
    assert snapshot["vgpr"]["2"] == [0x12345678] * 32
    assert snapshot["memory"]["global"]["0x0"][:4] == [0x78, 0x56, 0x34, 0x12]
    assert snapshot["counters"]["mem_ops"] == 64


def test_native_subword_load_extends_to_32_bits() -> None:
    program = [
        {"op": "IADD3", "operands": {"dst": "R1", "src0": "RZ", "src1": "RZ", "src2": 0x000000FF}},
        {"op": "STG", "operands": {"src": "R1", "addr": {"base": "RZ", "imm": 0}, "width": "u8"}},
        {"op": "LDG", "operands": {"rd": "R2", "addr": {"base": "RZ", "imm": 0}, "width": "u8"}},
        {"op": "LDG", "operands": {"rd": "R3", "addr": {"base": "RZ", "imm": 0}, "width": "s8"}},
        {"op": "EXIT"},
    ]

    snapshot = native.step(native.launch(program, global_allocations=[(0, 128)]), 10)

    assert snapshot["trap"]["kind"] == "none"
    assert snapshot["vgpr"]["2"] == [0xFF] * 32
    assert snapshot["vgpr"]["3"] == [0xFFFFFFFF] * 32


def test_native_wide_global_load_store_register_group_little_endian() -> None:
    program = [
        {"op": "IADD3", "operands": {"dst": "R4", "src0": "RZ", "src1": "RZ", "src2": 0x11111111}},
        {"op": "IADD3", "operands": {"dst": "R5", "src0": "RZ", "src1": "RZ", "src2": 0x22222222}},
        {"op": "STG", "operands": {"src": "R4", "addr": {"base": "RZ", "imm": 0}, "width": "64"}},
        {"op": "LDG", "operands": {"rd": "R8", "addr": {"base": "RZ", "imm": 0}, "width": "64"}},
        {"op": "EXIT"},
    ]

    snapshot = native.step(native.launch(program, global_allocations=[(0, 128)]), 10)

    assert snapshot["trap"]["kind"] == "none"
    assert snapshot["vgpr"]["8"] == [0x11111111] * 32
    assert snapshot["vgpr"]["9"] == [0x22222222] * 32
    assert snapshot["memory"]["global"]["0x0"][:8] == [0x11, 0x11, 0x11, 0x11, 0x22, 0x22, 0x22, 0x22]


def test_native_shared_and_local_memory_spaces_are_separate() -> None:
    program = [
        {"op": "IADD3", "operands": {"dst": "R1", "src0": "RZ", "src1": "RZ", "src2": 7}},
        {"op": "STS", "operands": {"src": "R1", "addr": {"base": "RZ", "imm": 0}, "width": "32"}},
        {"op": "STL", "operands": {"src": "R1", "addr": {"base": "RZ", "imm": 4}, "width": "32"}},
        {"op": "EXIT"},
    ]

    snapshot = native.step(native.launch(program), 10)

    assert snapshot["trap"]["kind"] == "none"
    assert snapshot["memory"]["shared"]["0x0"][:4] == [7, 0, 0, 0]
    assert snapshot["memory"]["local"]["0"]["0x0"][4:8] == [7, 0, 0, 0]
    assert snapshot["memory"]["global"] == {}


def test_native_memory_guard_false_lanes_are_suppressed() -> None:
    program = [
        {"op": "ISETP", "operands": {"pd": "P0", "src_a": "RZ", "src_b": "RZ", "cmp": "NE"}},
        {"op": "STG", "guard": "P0", "operands": {"src": "RZ", "addr": {"base": "RZ", "imm": 0}, "width": "32"}},
        {"op": "EXIT"},
    ]

    snapshot = native.step(native.launch(program, global_allocations=[(0, 128)]), 10)

    assert snapshot["trap"]["kind"] == "none"
    assert snapshot["memory"]["global"] == {}
    assert snapshot["counters"]["mem_ops"] == 0


def test_native_global_oob_traps_when_allocations_are_declared() -> None:
    program = [
        {"op": "LDG", "operands": {"rd": "R1", "addr": {"base": "RZ", "imm": 8}, "width": "32"}},
    ]

    snapshot = native.step(native.launch(program, global_allocations=[(0, 8)]), 10)

    assert snapshot["trap"]["kind"] == "memory"
    assert snapshot["trap"]["reason"] == "global_oob"
    assert snapshot["trap"]["detail"]["address"] == 8


def test_native_shared_local_and_alignment_traps() -> None:
    shared = native.step(native.launch([{"op": "LDS", "operands": {"rd": "R1", "addr": {"base": "RZ", "imm": 8}, "width": "32"}}], shared_mem_bytes=8), 10)
    local = native.step(native.launch([{"op": "LDL", "operands": {"rd": "R1", "addr": {"base": "RZ", "imm": 8}, "width": "32"}}], local_mem_bytes=8), 10)
    misaligned = native.step(native.launch([{"op": "LDG", "operands": {"rd": "R1", "addr": {"base": "RZ", "imm": 2}, "width": "32"}}]), 10)

    assert shared["trap"]["kind"] == "memory"
    assert shared["trap"]["reason"] == "shared_oob"
    assert local["trap"]["kind"] == "memory"
    assert local["trap"]["reason"] == "local_oob"
    assert misaligned["trap"]["kind"] == "memory"
    assert misaligned["trap"]["reason"] == "misaligned_address"


def test_native_membar_and_fence_are_noops() -> None:
    program = [
        {"op": "IADD3", "operands": {"dst": "R1", "src0": "RZ", "src1": "RZ", "src2": 3}},
        {"op": "MEMBAR", "operands": {"scope": "GPU", "order": "SC"}},
        {"op": "FENCE", "operands": {"scope": "CTA", "order": "ACQUIRE"}},
        {"op": "IADD3", "operands": {"dst": "R2", "src0": "R1", "src1": "RZ", "src2": 4}},
        {"op": "EXIT"},
    ]

    snapshot = native.step(native.launch(program), 10)

    assert snapshot["trap"]["kind"] == "none"
    assert snapshot["vgpr"]["2"] == [7] * 32


def test_native_generic_address_resolves_shared_local_and_global() -> None:
    program = [
        {"op": "IADD3", "operands": {"dst": "R1", "src0": "RZ", "src1": "RZ", "src2": 5}},
        {"op": "ST", "operands": {"src": "R1", "addr": {"base": "RZ", "imm": 0x1000000000000000}, "width": "32"}},
        {"op": "ST", "operands": {"src": "R1", "addr": {"base": "RZ", "imm": 0x2000000000000004}, "width": "32"}},
        {"op": "ST", "operands": {"src": "R1", "addr": {"base": "RZ", "imm": 64}, "width": "32"}},
        {"op": "LD", "operands": {"rd": "R2", "addr": {"base": "RZ", "imm": 0x1000000000000000}, "width": "32"}},
        {"op": "LD", "operands": {"rd": "R3", "addr": {"base": "RZ", "imm": 0x2000000000000004}, "width": "32"}},
        {"op": "LD", "operands": {"rd": "R4", "addr": {"base": "RZ", "imm": 64}, "width": "32"}},
        {"op": "EXIT"},
    ]

    snapshot = native.step(native.launch(program, global_allocations=[(64, 4)]), 20)

    assert snapshot["trap"]["kind"] == "none"
    assert snapshot["vgpr"]["2"] == [5] * 32
    assert snapshot["vgpr"]["3"] == [5] * 32
    assert snapshot["vgpr"]["4"] == [5] * 32
    assert snapshot["memory"]["shared"]["0x0"][:4] == [5, 0, 0, 0]
    assert snapshot["memory"]["local"]["0"]["0x0"][4:8] == [5, 0, 0, 0]
    assert snapshot["memory"]["global"]["0x0"][64:68] == [5, 0, 0, 0]


def test_native_ldc_reads_const_bank_and_snapshots_top_level_const_memory() -> None:
    words = [
        assembler.emit("LDC", "R8", 2, ("RZ", 0), width="64"),
        assembler.emit("EXIT"),
    ]

    snapshot = native.step(native.launch_words(words, const_banks={2: bytes([1, 2, 3, 4, 5, 6, 7, 8])}), 10)

    assert snapshot["trap"]["kind"] == "none"
    assert snapshot["vgpr"]["8"] == [0x04030201] * 32
    assert snapshot["vgpr"]["9"] == [0x08070605] * 32
    assert snapshot["const_memory"]["2"] == [1, 2, 3, 4, 5, 6, 7, 8]
    assert snapshot["memory"] == {"global": {}, "local": {}, "shared": {}}


def test_native_ldc_const_oob_traps() -> None:
    snapshot = native.step(
        native.launch(
            [{"op": "LDC", "operands": {"rd": "R1", "bank": 3, "addr": {"base": "RZ", "imm": 0}, "width": "32"}}],
            const_banks={2: [1, 2, 3, 4]},
        ),
        10,
    )

    assert snapshot["trap"]["kind"] == "memory"
    assert snapshot["trap"]["reason"] == "const_oob"
    assert snapshot["trap"]["detail"]["space"] == "const"


def test_native_cvta_shared_roundtrip_is_address_arithmetic() -> None:
    words = [
        assembler.emit("IADD3", "R2", "RZ", "RZ", 16),
        assembler.emit("IADD3", "R3", "RZ", "RZ", 0),
        assembler.emit("CVTA", "R4", "R2", direction="to_shared"),
        assembler.emit("CVTA", "R6", "R4", direction="from_shared"),
        assembler.emit("EXIT"),
    ]

    snapshot = native.step(native.launch_words(words), 10)

    assert snapshot["trap"]["kind"] == "none"
    assert snapshot["vgpr"]["4"] == [16] * 32
    assert snapshot["vgpr"]["5"] == [0x10000000] * 32
    assert snapshot["vgpr"]["6"] == [16] * 32
    assert snapshot["vgpr"]["7"] == [0] * 32


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
