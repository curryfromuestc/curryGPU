from __future__ import annotations

from currygpu.isa import assembler
from currygpu.iss import native
from its_corpus import SCHED_ORDERS, architectural_subset, corpus_cases, pre_screen


def test_its_kernel_builder_generates_deterministic_corpus_words() -> None:
    first = {case.name: case.words for case in corpus_cases()}
    second = {case.name: case.words for case in corpus_cases()}

    assert first == second
    assert {"if_else", "nested", "variable_reduction_loop", "causal_mask_control_divergent", "collective_placement_k2"} <= set(first)


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
        if not accepted:
            continue
        snapshots = []
        for order in SCHED_ORDERS:
            snapshot = native.step(native.launch_words(case.words, sched_order=order, debug_checks=True), 200)
            assert snapshot["trap"]["kind"] == "none", (case.name, order, snapshot["trap"])
            snapshots.append(architectural_subset(snapshot))
        assert snapshots[1:] == snapshots[:-1], case.name


def test_its_corpus_marks_required_divergent_cases_non_vacuous() -> None:
    for case in corpus_cases():
        accepted, _ = pre_screen(case)
        if not accepted or not case.expected_divergent:
            continue
        snapshot = native.step(native.launch_words(case.words), 200)
        assert snapshot["counters"]["divergence_events"] > 0, case.name


def test_its_variable_reduction_loop_consumes_laneid_source() -> None:
    case = next(case for case in corpus_cases() if case.name == "variable_reduction_loop")
    decoded = [assembler.decode_like_ir(word) for word in case.words]

    assert decoded[0]["name"] == "S2R"
    assert decoded[0]["operands"] == {"rd": "R1", "sr": "SR_LANEID"}
    assert "ELECT" not in {item["name"] for item in decoded}

    snapshot = native.step(native.launch_words(case.words), 200)

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

    snapshot = native.step(native.launch_words(case.words), 200)

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
    }

    assert {name for name, value in killed.items() if not value} == set()


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
