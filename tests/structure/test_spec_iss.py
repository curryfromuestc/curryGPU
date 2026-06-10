from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SPEC = ROOT / "docs" / "implement" / "ISS" / "spec-iss.md"
NATIVE = ROOT / "iss" / "binding" / "native.cpp"


def _native_convergence_reasons() -> set[str]:
    text = NATIVE.read_text(encoding="utf-8")
    reasons = set(re.findall(r'set_convergence_trap\("([^"]+)"', text))
    reasons.update(re.findall(r'set_trap\("convergence",\s*"([^"]+)"', text))
    return reasons


def test_spec_iss_documents_native_convergence_reasons() -> None:
    spec = SPEC.read_text(encoding="utf-8")
    missing = sorted(reason for reason in _native_convergence_reasons() if f"`{reason}`" not in spec)

    assert missing == []


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
