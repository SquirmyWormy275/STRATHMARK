"""No executable evidence may be sealed after its authenticated tools change."""

from pathlib import Path

import pytest

from scripts import run_v3_release_evidence as runner


def test_release_proof_rechecks_tools_before_signing(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    monkeypatch.setattr(runner, "require_clean_release_inputs", lambda root: None)
    monkeypatch.setattr(runner, "dependency_snapshot", lambda path: ("a" * 64, "b" * 64))
    monkeypatch.setattr(runner, "git_head", lambda root: "c" * 40)
    monkeypatch.setattr(runner, "source_tree_digest", lambda root: "d" * 64)
    calls = []

    def tools(root):
        calls.append(root)
        return {"installed_files_sha256": "before" if len(calls) == 1 else "changed"}

    monkeypatch.setattr(runner, "qualification_toolchain", tools)
    monkeypatch.setattr(
        runner,
        "_installed_artifact_proof",
        lambda **kwargs: (
            {"name": "installed_artifact"},
            tmp_path / "candidate.whl",
        ),
    )
    monkeypatch.setattr(runner, "_execute", lambda *args, **kwargs: {})
    for name in ("_proof", "_pytest_proof", "_machine_proof"):
        monkeypatch.setattr(runner, name, lambda name, *args, **kwargs: {"name": name})

    def must_not_sign(*args, **kwargs):
        raise AssertionError("changed qualification tools must not produce a signed receipt")

    monkeypatch.setattr(runner, "create_evidence_envelope", must_not_sign)
    output = tmp_path / "evidence.json"
    with pytest.raises(RuntimeError, match="tool bytes changed while proofs"):
        runner.build_evidence(
            output=output,
            wheel_directory=tmp_path / "wheels",
            artifact_directory=tmp_path / "artifacts",
            local_models=("fixture",),
            stress_seconds=30,
            nvidia_smi=Path("/approved/nvidia-smi"),
            nvidia_smi_sha256="a" * 64,
        )
    assert len(calls) == 2
    assert not output.exists()
