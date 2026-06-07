#!/usr/bin/env python3
"""Repository structure checks for the ISS component layout."""

from __future__ import annotations

import argparse
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


ROOT_BUILD_FILES = {
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "CMakeLists.txt",
    "MANIFEST.in",
}

PYTHON_BUILD_FILES = {
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "MANIFEST.in",
}

GENERATED_MARKERS = (
    "iss/build/generated/",
    "iss/build/generated\\",
)

PRIVATE_MARKERS = (
    "isa/layout/production/",
    "isa/layout/production\\",
    "isa/private_layout/",
    "isa/private_layout\\",
)

PRODUCTION_BINARY_SUFFIXES = (
    ".bin",
    ".cubin",
    ".fatbin",
)


@dataclass(frozen=True)
class CheckResult:
    name: str
    errors: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not self.errors


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="utf-8", errors="ignore")


def _tracked_files(root: Path) -> list[str]:
    proc = subprocess.run(
        ["git", "ls-files"],
        cwd=root,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return [line for line in proc.stdout.splitlines() if line]


def check_root_is_not_package(root: Path) -> CheckResult:
    errors: list[str] = []
    for name in sorted(ROOT_BUILD_FILES):
        if (root / name).exists():
            errors.append(f"root build file is not allowed: {name}")
    if (root / "currygpu").exists():
        errors.append("root currygpu package directory is not allowed")
    return CheckResult("root_is_not_package", tuple(errors))


def check_python_build_files_are_component_scoped(root: Path) -> CheckResult:
    errors: list[str] = []
    for path in root.rglob("*"):
        if not path.is_file() or path.name not in PYTHON_BUILD_FILES:
            continue
        rel = path.relative_to(root).as_posix()
        if not rel.startswith("iss/"):
            errors.append(f"python build file must live under iss/: {rel}")
    return CheckResult("python_build_files_are_component_scoped", tuple(errors))


def check_isa_has_no_iss_dependency(root: Path) -> CheckResult:
    isa_root = root / "isa"
    if not isa_root.exists():
        return CheckResult("isa_has_no_iss_dependency", ())

    errors: list[str] = []
    forbidden = (
        "currygpu.iss",
        "from currygpu import iss",
        "import iss",
        "from iss",
    )
    for path in isa_root.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix not in {".py", ".pyi", ".toml", ".txt", ".md", ".json", ".cmake"}:
            continue
        text = _read_text(path)
        for needle in forbidden:
            if needle in text:
                rel = path.relative_to(root).as_posix()
                errors.append(f"isa must not depend on iss: {rel} contains {needle!r}")
    return CheckResult("isa_has_no_iss_dependency", tuple(errors))


def check_tracked_private_outputs(root: Path) -> CheckResult:
    errors: list[str] = []
    for rel in _tracked_files(root):
        normalized = rel.replace("\\", "/")
        if any(normalized.startswith(marker.replace("\\", "/")) for marker in GENERATED_MARKERS):
            errors.append(f"generated output must not be tracked: {rel}")
        if any(normalized.startswith(marker.replace("\\", "/")) for marker in PRIVATE_MARKERS):
            errors.append(f"production layout content must not be tracked: {rel}")
        if normalized.startswith("iss/") and normalized.endswith(PRODUCTION_BINARY_SUFFIXES):
            errors.append(f"production binary must not be tracked: {rel}")
    return CheckResult("tracked_private_outputs", tuple(errors))


def run_checks(root: Path) -> list[CheckResult]:
    return [
        check_root_is_not_package(root),
        check_python_build_files_are_component_scoped(root),
        check_isa_has_no_iss_dependency(root),
        check_tracked_private_outputs(root),
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)

    root = args.root.resolve()
    results = run_checks(root)
    failures = [result for result in results if not result.passed]
    for result in results:
        status = "ok" if result.passed else "fail"
        print(f"{status}: {result.name}")
        for error in result.errors:
            print(f"  - {error}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
