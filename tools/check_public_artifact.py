#!/usr/bin/env python3
"""Check that public package artifacts do not contain private ISS assets."""

from __future__ import annotations

import argparse
import sys
import tarfile
import zipfile
from pathlib import Path


FORBIDDEN_PARTS = (
    "iss/build/generated/",
    "build/generated/",
    "isa/layout/production/",
    "isa/private_layout/",
    "layout/production/",
    "private_layout/",
)

FORBIDDEN_SUFFIXES = (
    "decode.gen.cpp",
    "decoded_inst.gen.h",
    "emit.gen.py",
    ".bin",
    ".cubin",
    ".fatbin",
)


def artifact_members(path: Path) -> list[str]:
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            return archive.namelist()
    if tarfile.is_tarfile(path):
        with tarfile.open(path) as archive:
            return archive.getnames()
    raise ValueError(f"unsupported artifact format: {path}")


def forbidden_members(members: list[str]) -> list[str]:
    blocked: list[str] = []
    for member in members:
        normalized = member.replace("\\", "/")
        if any(part in normalized for part in FORBIDDEN_PARTS):
            blocked.append(member)
            continue
        if any(normalized.endswith(suffix) for suffix in FORBIDDEN_SUFFIXES):
            blocked.append(member)
    return sorted(blocked)


def check_artifact(path: Path) -> list[str]:
    return forbidden_members(artifact_members(path))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifacts", nargs="+", type=Path)
    args = parser.parse_args(argv)

    errors = []
    for artifact in args.artifacts:
        blocked = check_artifact(artifact)
        if blocked:
            errors.append((artifact, blocked))
    for artifact, blocked in errors:
        print(f"fail: {artifact}")
        for member in blocked:
            print(f"  - {member}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
