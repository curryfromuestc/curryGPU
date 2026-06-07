from __future__ import annotations

import sys
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
TOOLS = ROOT / "tools"
sys.path.insert(0, str(TOOLS))

from check_public_artifact import check_artifact  # noqa: E402


def test_public_artifact_accepts_public_sample_files(tmp_path: Path) -> None:
    artifact = tmp_path / "sample.whl"
    with zipfile.ZipFile(artifact, "w") as archive:
        archive.writestr("currygpu/isa/isa_ir.json", "{}")
        archive.writestr("currygpu/iss/core.py", "")

    assert check_artifact(artifact) == []


def test_public_artifact_rejects_generated_decoder(tmp_path: Path) -> None:
    artifact = tmp_path / "bad.whl"
    with zipfile.ZipFile(artifact, "w") as archive:
        archive.writestr("currygpu/iss/build/generated/decode.gen.cpp", "")

    assert check_artifact(artifact) == ["currygpu/iss/build/generated/decode.gen.cpp"]


def test_public_artifact_rejects_private_layout(tmp_path: Path) -> None:
    artifact = tmp_path / "bad.whl"
    with zipfile.ZipFile(artifact, "w") as archive:
        archive.writestr("currygpu/isa/layout/production/table.py", "")

    assert check_artifact(artifact) == ["currygpu/isa/layout/production/table.py"]
