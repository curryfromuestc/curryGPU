from __future__ import annotations

from pathlib import Path

from currygpu.isa import assembler
from currygpu.iss import native
from its_corpus import KernelBuilder, SCHED_ORDERS, architectural_subset, corpus_cases, pre_screen

WARP_SCHED_ORDERS = ("warp_round_robin", "warp_min_id_first", "warp_max_id_first")
ROOT = Path(__file__).resolve().parents[2]


def _launch_kwargs(case) -> dict:
    return dict(case.launch_kwargs or {})


def _run_case(case, *, sched_order: str = "min_pc_first", **extra_kwargs):
    kwargs = _launch_kwargs(case)
    kwargs.update(extra_kwargs)
    return native.step(native.launch_words(case.words, sched_order=sched_order, **kwargs), case.max_steps)


def test_its_kernel_builder_generates_deterministic_corpus_words() -> None:
    first = {case.name: case.words for case in corpus_cases()}
    second = {case.name: case.words for case in corpus_cases()}

    assert first == second
    assert {
        "if_else",
        "nested",
        "variable_reduction_loop",
        "causal_mask_control_divergent",
        "collective_placement_k2",
        "barrier_shared_exchange",
        "atomic_red_add",
        "order_sensitive_exch",
        "progress_spinlock",
        "progress_consumer_first",
        "const_read",
        "grid_independent",
        "race_cross_warp_store",
    } <= set(first)


def test_its_static_pre_screen_accepts_well_formed_and_rejects_k2() -> None:
    cases = {case.name: case for case in corpus_cases()}

    for name, case in cases.items():
        accepted, reason = pre_screen(case)
        if name == "collective_placement_k2":
            assert not accepted
            assert reason == "collective_placement"
        else:
            assert accepted
            assert reason == ""


def test_its_corpus_is_schedule_order_independent() -> None:
    for case in corpus_cases():
        accepted, _ = pre_screen(case)
        if not accepted or "multi_warp" in case.tags or "grid" in case.tags or "race_negative" in case.tags:
            continue
        snapshots = []
        for order in SCHED_ORDERS:
            snapshot = _run_case(case, sched_order=order, debug_checks=True)
            assert snapshot["trap"]["kind"] == "none", (case.name, order, snapshot["trap"])
            snapshots.append(architectural_subset(snapshot))
        assert snapshots[1:] == snapshots[:-1], case.name


def test_its_corpus_marks_required_divergent_cases_non_vacuous() -> None:
    for case in corpus_cases():
        accepted, _ = pre_screen(case)
        if not accepted or not case.expected_divergent:
            continue
        snapshot = _run_case(case)
        assert snapshot["counters"]["divergence_events"] > 0, case.name


def test_its_variable_reduction_loop_consumes_laneid_source() -> None:
    case = next(case for case in corpus_cases() if case.name == "variable_reduction_loop")
    decoded = [assembler.decode_like_ir(word) for word in case.words]

    assert decoded[0]["name"] == "S2R"
    assert decoded[0]["operands"] == {"rd": "R1", "sr": "SR_LANEID"}
    assert "ELECT" not in {item["name"] for item in decoded}

    snapshot = _run_case(case)

    assert snapshot["trap"]["kind"] == "none"
    assert snapshot["vgpr"]["1"] == list(range(32))
    assert snapshot["vgpr"]["2"] == [1] * 8 + [2] * 8 + [3] * 8 + [4] * 8
    assert snapshot["vgpr"]["7"] == snapshot["vgpr"]["2"]
    assert snapshot["counters"]["divergence_events"] >= 3


def test_its_causal_mask_consumes_laneid_source() -> None:
    case = next(case for case in corpus_cases() if case.name == "causal_mask_control_divergent")
    decoded = [assembler.decode_like_ir(word) for word in case.words]

    assert decoded[0]["name"] == "S2R"
    assert decoded[0]["operands"] == {"rd": "R1", "sr": "SR_LANEID"}
    assert "ELECT" not in {item["name"] for item in decoded}

    snapshot = _run_case(case)

    assert snapshot["trap"]["kind"] == "none"
    assert snapshot["vgpr"]["1"] == list(range(32))
    assert snapshot["vgpr"]["2"] == list(range(16)) + [99] * 16
    assert snapshot["counters"]["divergence_events"] > 0


def test_its_pure_predication_candidate_is_vacuous_for_divergence_coverage() -> None:
    program = [
        {"op": "ELECT", "operands": {"pd": "P0", "membermask": 0xFFFFFFFF}},
        {"op": "IADD3", "guard": "@P0", "operands": {"dst": "R1", "src0": 1, "src1": 0, "src2": 0}},
        {"op": "IADD3", "guard": "!P0", "operands": {"dst": "R1", "src0": 2, "src1": 0, "src2": 0}},
        {"op": "EXIT"},
    ]

    snapshot = native.step(native.launch(program), 20)

    assert snapshot["trap"]["kind"] == "none"
    assert snapshot["counters"]["divergence_events"] == 0


def test_its_named_mutants_are_killed() -> None:
    killed = {
        "drop_bsync": _mutant_drops_bsync_is_killed(),
        "off_by_one_barrier": _mutant_off_by_one_barrier_is_killed(),
        "wrong_tie_break": _mutant_wrong_tie_break_is_killed(),
        "stale_blocked_on": _mutant_stale_blocked_on_is_killed(),
        "missing_dissolved_phase": _mutant_missing_dissolved_phase_is_killed(),
        "warp_run_to_block": _mutant_warp_run_to_block_is_killed(),
        "warp_sched_unfair": _mutant_warp_sched_unfair_is_killed(),
        "skip_yielded_promotion": _mutant_skip_yielded_promotion_is_killed(),
    }

    assert {name for name, value in killed.items() if not value} == set()


def test_memsync_ab_gate_is_schedule_order_independent_across_12_combinations() -> None:
    gate_cases = [case for case in corpus_cases() if "barrier_drf" in case.tags or "atomic_commutative" in case.tags]

    for case in gate_cases:
        snapshots = []
        for warp_order in WARP_SCHED_ORDERS:
            for sched_order in SCHED_ORDERS:
                snapshot = _run_case(case, sched_order=sched_order, warp_sched_order=warp_order)
                assert snapshot["trap"]["kind"] == "none", (case.name, warp_order, sched_order, snapshot["trap"])
                snapshots.append(architectural_subset(snapshot))
        assert snapshots[1:] == snapshots[:-1], case.name


def test_memsync_order_sensitive_case_is_single_schedule_deterministic_only() -> None:
    case = next(case for case in corpus_cases() if case.name == "order_sensitive_exch")

    first = _run_case(case, warp_sched_order="warp_min_id_first")
    second = _run_case(case, warp_sched_order="warp_min_id_first")
    opposite = _run_case(case, warp_sched_order="warp_max_id_first")

    assert first["trap"]["kind"] == "none"
    assert second["trap"]["kind"] == "none"
    assert opposite["trap"]["kind"] == "none"
    assert architectural_subset(first) == architectural_subset(second)
    assert first["memory"]["global"]["0x0"][:4] != opposite["memory"]["global"]["0x0"][:4]


def test_memsync_progress_cases_terminate_under_fair_warp_schedule() -> None:
    progress_cases = [case for case in corpus_cases() if "progress_test" in case.tags]

    for case in progress_cases:
        snapshot = _run_case(case, warp_sched_order="warp_round_robin")
        assert snapshot["trap"]["kind"] == "none", (case.name, snapshot["trap"])
        assert snapshot["memory"]["global"]["0x0"][:4] == [32, 0, 0, 0]


def test_memsync_macro_step_advances_exactly_one_warp_group() -> None:
    case = next(case for case in corpus_cases() if case.name == "barrier_shared_exchange")
    first = native.step(native.launch_words(case.words, **_launch_kwargs(case)), 1)
    second = native.step(native.launch_words(case.words, **_launch_kwargs(case)), 2)
    third = native.step(native.launch_words(case.words, **_launch_kwargs(case)), 3)

    assert first["trap"]["kind"] == "max_steps"
    assert first["warps"][0]["pc"] == [1] * 32
    assert first["warps"][1]["pc"] == [0] * 32
    assert first["counters"]["warp_instructions"] == 1
    assert second["trap"]["kind"] == "max_steps"
    assert second["warps"][0]["pc"] == [1] * 32
    assert second["warps"][1]["pc"] == [1] * 32
    assert second["counters"]["warp_instructions"] == 2
    assert third["trap"]["kind"] == "max_steps"
    assert third["warps"][0]["pc"] == [2] * 32
    assert third["warps"][1]["pc"] == [1] * 32
    assert third["counters"]["warp_instructions"] == 3


def test_memsync_grid_and_const_corpus_members_have_expected_state() -> None:
    const_case = next(case for case in corpus_cases() if case.name == "const_read")
    grid_case = next(case for case in corpus_cases() if case.name == "grid_independent")

    const_snapshot = _run_case(const_case)
    grid_snapshot = _run_case(grid_case)

    assert const_snapshot["trap"]["kind"] == "none"
    assert const_snapshot["vgpr"]["8"] == [0x04030201] * 32
    assert const_snapshot["const_memory"]["2"] == [1, 2, 3, 4, 5, 6, 7, 8]
    assert grid_snapshot["trap"]["kind"] == "none"
    assert grid_snapshot["memory"]["global"]["0x0"][:2] == [1, 2]
    assert [cta["memory"]["shared"]["0x0"][0] for cta in grid_snapshot["ctas"]] == [1, 2]


def test_memsync_race_negative_is_classified_and_opt_in_traps() -> None:
    case = next(case for case in corpus_cases() if case.name == "race_cross_warp_store")

    unchecked_a = _run_case(case, warp_sched_order="warp_min_id_first")
    unchecked_b = _run_case(case, warp_sched_order="warp_min_id_first")
    checked = _run_case(case, warp_sched_order="warp_min_id_first", race_check=True)
    opposite = _run_case(case, warp_sched_order="warp_max_id_first")

    assert unchecked_a["trap"]["kind"] == "none"
    assert unchecked_b["trap"]["kind"] == "none"
    assert architectural_subset(unchecked_a) == architectural_subset(unchecked_b)
    assert unchecked_a["memory"]["global"]["0x0"][:32] != opposite["memory"]["global"]["0x0"][:32]
    assert checked["trap"]["kind"] == "memory"
    assert checked["trap"]["reason"] == "data_race"
    assert checked["trap"]["detail"]["space"] == "global"


def test_native_split_minimum_modules_are_present_and_registered() -> None:
    required_files = [
        ROOT / "iss" / "binding" / "memory_space.h",
        ROOT / "iss" / "binding" / "memory_space.cpp",
        ROOT / "iss" / "binding" / "cta_barrier.h",
        ROOT / "iss" / "binding" / "cta_barrier.cpp",
        ROOT / "iss" / "binding" / "block_state.h",
        ROOT / "iss" / "binding" / "race_shadow.h",
        ROOT / "iss" / "binding" / "race_shadow.cpp",
    ]
    cmake_text = (ROOT / "iss" / "CMakeLists.txt").read_text(encoding="utf-8")

    assert [path for path in required_files if not path.exists()] == []
    for source in ("binding/memory_space.cpp", "binding/cta_barrier.cpp", "binding/atomic_ops.cpp", "binding/race_shadow.cpp"):
        assert source in cmake_text


def _mutant_drops_bsync_is_killed() -> bool:
    case = next(case for case in corpus_cases() if case.name == "if_else")
    mutated = tuple(word for word in case.words if _decoded_name(word) != "BSYNC")
    snapshot = native.step(native.launch_words(mutated, debug_checks=True), 200)
    baseline = native.step(native.launch_words(case.words, debug_checks=True), 200)
    return snapshot["trap"]["kind"] != "none" or architectural_subset(snapshot) != architectural_subset(baseline)


def _mutant_off_by_one_barrier_is_killed() -> bool:
    case = next(case for case in corpus_cases() if case.name == "if_else")
    mutated = []
    changed = False
    for word in case.words:
        if not changed and _decoded_name(word) == "BSYNC":
            mutated.append(_replace_barrier(word, "B1"))
            changed = True
        else:
            mutated.append(word)
    snapshot = native.step(native.launch_words(mutated, debug_checks=True), 200)
    return snapshot["trap"]["kind"] == "convergence" and snapshot["trap"]["reason"] == "bsync_invalid_barrier"


def _mutant_wrong_tie_break_is_killed() -> bool:
    case = next(case for case in corpus_cases() if case.name == "if_else")
    baseline = native.step(native.launch_words(case.words, sched_order="min_pc_first"), 200)
    mutated = native.step(native.launch_words(case.words, sched_order="max_pc_first"), 200)
    return baseline["counters"] != mutated["counters"] and architectural_subset(baseline) == architectural_subset(mutated)


def _mutant_stale_blocked_on_is_killed() -> bool:
    case = next(case for case in corpus_cases() if case.name == "nested")
    snapshot = native.step(native.launch_words(case.words, debug_checks=True), 200)
    return snapshot["trap"]["kind"] == "none" and snapshot["bx"]["barriers"][0]["valid"] is False and snapshot["bx"]["barriers"][1]["valid"] is False


def _mutant_missing_dissolved_phase_is_killed() -> bool:
    case = next(case for case in corpus_cases() if case.name == "causal_mask_control_divergent")
    snapshot = native.step(native.launch_words(case.words, debug_checks=True), 200)
    return snapshot["trap"]["kind"] == "none"


def _mutant_warp_run_to_block_is_killed() -> bool:
    # Run-to-block scheduling never preempts a spinning consumer warp.
    # warp_min_id_first on the consumer-first geometry reproduces exactly
    # that starvation, so the D-tier budget must kill it while the fair
    # rotation terminates the same program.
    case = next(case for case in corpus_cases() if case.name == "progress_consumer_first")
    fair = _run_case(case, warp_sched_order="warp_round_robin")
    run_to_block_like = _run_case(case, warp_sched_order="warp_min_id_first")

    return (
        fair["trap"]["kind"] == "none"
        and run_to_block_like["trap"]["kind"] == "max_steps"
        and run_to_block_like["trap"]["reason"] == "budget_exhausted"
    )


def _mutant_warp_sched_unfair_is_killed() -> bool:
    # A rotation that keeps skipping the lowest-id runnable warp is
    # reproduced by warp_max_id_first on the producer-first geometry: the
    # spinning consumer keeps priority and the producer never runs. Distinct
    # from the run-to-block analogue in both policy and starvation geometry.
    case = next(case for case in corpus_cases() if case.name == "progress_spinlock")
    fair = _run_case(case, warp_sched_order="warp_round_robin")
    unfair = _run_case(case, warp_sched_order="warp_max_id_first")

    return (
        fair["trap"]["kind"] == "none"
        and unfair["trap"]["kind"] == "max_steps"
        and unfair["trap"]["reason"] == "budget_exhausted"
    )


def _mutant_skip_yielded_promotion_is_killed() -> bool:
    # The single-warp case reaches the all-exited end state only through a
    # yielded-lane promotion, and inserting YIELD into a barrier-DRF
    # multi-warp program must stay architecturally invisible: dropping the
    # promotion parks lanes forever, while cross-warp state leakage shows up
    # as diverging register results.
    case = next(case for case in corpus_cases() if case.name == "yield_arrival")
    snapshot = _run_case(case)
    plain = _yield_invariance_snapshot(insert_yield=False)
    yielded = _yield_invariance_snapshot(insert_yield=True)

    return (
        snapshot["trap"]["kind"] == "none"
        and snapshot["active_mask"] == [False] * 32
        and snapshot["lane_state"] == ["exited"] * 32
        and snapshot["counters"]["warp_instructions"] == len(case.words)
        and plain["trap"]["kind"] == "none"
        and yielded["trap"]["kind"] == "none"
        and all(warp["lane_state"] == ["exited"] * 32 for warp in yielded["warps"])
        and [warp["vgpr"]["3"] for warp in yielded["warps"]]
        == [warp["vgpr"]["3"] for warp in plain["warps"]]
    )


def _yield_invariance_snapshot(*, insert_yield: bool):
    kb = KernelBuilder()
    kb.emit("S2R", "R1", "SR_WARPID")
    kb.emit("IADD3", "R2", "R1", "RZ", 1)
    if insert_yield:
        kb.emit("YIELD")
    kb.emit("BAR", "B0", mode="sync")
    kb.emit("IADD3", "R3", "R2", "R2", 0)
    kb.emit("EXIT")
    return native.step(
        native.launch_words(kb.words(), num_warps=2, ntid=(64, 1, 1), warp_sched_order="warp_round_robin"),
        200,
    )


def _decoded_name(word: int) -> str:
    from currygpu.isa import assembler

    return assembler.decode_like_ir(word)["name"]


def _replace_barrier(word: int, replacement: str) -> int:
    from currygpu.isa import assembler

    decoded = assembler.decode_like_ir(word)
    operands = dict(decoded["operands"])
    operands["bar"] = replacement
    return assembler.emit(
        decoded["name"],
        **operands,
        guard=decoded["guard"]["predicate"],
        guard_neg=decoded["guard"]["negated"],
        **decoded["modifiers"],
    )
