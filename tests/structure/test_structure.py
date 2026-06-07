from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
TOOLS = ROOT / "tools"
sys.path.insert(0, str(TOOLS))

from check_structure import (  # noqa: E402
    check_isa_has_no_iss_dependency,
    check_python_build_files_are_component_scoped,
    check_root_is_not_package,
    check_tracked_private_outputs,
    run_checks,
)


def test_repository_structure_passes() -> None:
    results = run_checks(ROOT)
    failures = [(result.name, result.errors) for result in results if not result.passed]
    assert failures == []


def test_root_package_check_rejects_build_file(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'bad'\n", encoding="utf-8")
    result = check_root_is_not_package(tmp_path)
    assert not result.passed
    assert "pyproject.toml" in result.errors[0]


def test_build_file_scope_check_rejects_isa_build_file(tmp_path: Path) -> None:
    path = tmp_path / "isa"
    path.mkdir()
    (path / "pyproject.toml").write_text("[project]\nname = 'bad'\n", encoding="utf-8")
    result = check_python_build_files_are_component_scoped(tmp_path)
    assert not result.passed
    assert "isa/pyproject.toml" in result.errors[0]


def test_reverse_dependency_check_rejects_isa_importing_iss(tmp_path: Path) -> None:
    path = tmp_path / "isa" / "currygpu" / "isa"
    path.mkdir(parents=True)
    (path / "bad.py").write_text("import currygpu.iss\n", encoding="utf-8")
    result = check_isa_has_no_iss_dependency(tmp_path)
    assert not result.passed
    assert "bad.py" in result.errors[0]


def test_tracked_output_check_rejects_generated_file(monkeypatch) -> None:
    import check_structure

    monkeypatch.setattr(
        check_structure,
        "_tracked_files",
        lambda root: ["iss/build/generated/decode.gen.cpp"],
    )
    result = check_tracked_private_outputs(ROOT)
    assert not result.passed
    assert "decode.gen.cpp" in result.errors[0]
