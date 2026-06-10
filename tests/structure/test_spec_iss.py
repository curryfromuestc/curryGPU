from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SPEC = ROOT / "docs" / "implement" / "ISS" / "spec-iss.md"
NATIVE = ROOT / "iss" / "binding" / "native.cpp"
TRAP_KINDS = ("convergence", "memory", "synchronization")


def _trap_reasons_from_source(text: str) -> dict[str, set[str]]:
    reasons = {kind: set() for kind in TRAP_KINDS}
    reasons["convergence"].update(re.findall(r'set_convergence_trap\("([^"]+)"', text))
    for kind, reason in re.findall(r'set_trap\("(convergence|memory|synchronization)",\s*"([^"]+)"', text):
        reasons[kind].add(reason)
    return reasons


def _missing_documented_trap_reasons(source: str, spec: str) -> list[tuple[str, str]]:
    missing = []
    for kind, reasons in _trap_reasons_from_source(source).items():
        missing.extend((kind, reason) for reason in sorted(reasons) if f"`{reason}`" not in spec)
    return missing


def test_spec_iss_documents_native_trap_reasons() -> None:
    spec = SPEC.read_text(encoding="utf-8")
    source = NATIVE.read_text(encoding="utf-8")

    assert _missing_documented_trap_reasons(source, spec) == []


def test_spec_iss_trap_reason_extractor_covers_memory_and_synchronization() -> None:
    source = """
    set_trap("memory", "undocumented_reason", pc, detail);
    set_trap("synchronization", "documented_sync_reason", pc, detail);
    set_convergence_trap("documented_convergence_reason", pc, -1);
    """
    spec = "`documented_sync_reason` `documented_convergence_reason`"

    assert _missing_documented_trap_reasons(source, spec) == [("memory", "undocumented_reason")]


def test_spec_iss_documents_required_phase3_trap_reasons() -> None:
    spec = SPEC.read_text(encoding="utf-8")
    required_reasons = [
        "barrier_deadlock",
        "barrier_count_not_warp_multiple",
        "barrier_id_out_of_range",
        "misaligned_address",
        "shared_oob",
        "local_oob",
        "global_oob",
        "const_oob",
        "unsupported_space_access",
        "generic_resolve_failure",
        "atomic_on_local_unsupported",
        "atomic_on_readonly_space",
        "atomic_misaligned",
        "atomic_unsupported_op",
        "red_has_destination",
        "data_race",
        "deadlock_no_progress",
    ]

    assert [reason for reason in required_reasons if f"`{reason}`" not in spec] == []


def test_spec_iss_documents_required_its_contract_terms() -> None:
    spec = SPEC.read_text(encoding="utf-8")
    required_terms = [
        "BSSY",
        "BSYNC",
        "BREAK",
        "YIELD",
        "EXIT",
        "CONT",
        "barrier_phase",
        "blocked_on",
        "ELECT",
        "VOTE",
        "pre-screen",
        "min_pc_first",
        "max_pc_first",
        "round_robin",
        "oldest_group_first",
        "DEC-1",
        "warp_round_robin",
        "warp_min_id_first",
        "warp_max_id_first",
        "weak fairness",
        "livelock",
        "forward progress",
        "run-to-block",
        "per-thread arrival",
        "cta_blocked",
        "arrived_thread_set",
    ]

    assert [term for term in required_terms if term not in spec] == []


def test_runtime_source_no_longer_emits_old_uniformity_traps() -> None:
    text = NATIVE.read_text(encoding="utf-8")

    assert "non_uniform_pc" not in text
    assert "non_uniform_branch" not in text


def test_spec_iss_has_no_out_of_repo_hard_dependency() -> None:
    spec = SPEC.read_text(encoding="utf-8")

    assert "control-sync-uniform.md" not in spec
    assert "nv_patent" not in spec
