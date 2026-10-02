"""Development training must verify provenance and exclude audit input."""

import subprocess
import sys
from types import SimpleNamespace

import pytest

from strathmark.v3.contracts.canonical import canonical_digest
from strathmark.v3.factory import candidate_cli
from strathmark.v3.runtime_identity import verify_source_revision


def test_source_revision_rejects_installed_source_drift(tmp_path, monkeypatch):
    import hashlib

    import strathmark.v3.runtime_identity as identity

    package = tmp_path / "strathmark"
    package.mkdir()
    (package / "__init__.py").write_bytes(b"# synthetic source\n")
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True)
    # This fixture compares exact source bytes. Do not inherit the Windows
    # runner's checkout conversion policy into its standalone synthetic repo.
    subprocess.run(["git", "-C", str(tmp_path), "config", "core.autocrlf", "false"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "add", "strathmark"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "-c",
            "user.name=Synthetic",
            "-c",
            "user.email=synthetic@example.invalid",
            "commit",
            "--quiet",
            "-m",
            "Synthetic provenance test",
        ],
        check=True,
    )
    revision = subprocess.check_output(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"], text=True
    ).strip()
    matching = canonical_digest(
        {"__init__.py": hashlib.sha256(b"# synthetic source\n").hexdigest()}
    )
    monkeypatch.setattr(identity, "implementation_digest", lambda: matching)
    assert verify_source_revision(tmp_path, revision) == matching
    monkeypatch.setattr(identity, "implementation_digest", lambda: "a" * 64)
    with pytest.raises(ValueError, match="differs"):
        verify_source_revision(tmp_path, revision)
    with pytest.raises(ValueError, match="complete lowercase"):
        verify_source_revision(tmp_path, "pretend-source")


def test_builder_rejects_audit_rows_before_any_fit(tmp_path, monkeypatch):
    from strathmark.v3.factory.workbook_history import load_workbook_history
    from tests.v3.unit.test_workbook_history import workbook_history_fixture

    workbook_history_fixture(tmp_path / "synthetic.xlsx")
    history = load_workbook_history(
        tmp_path / "synthetic.xlsx", cutoff_at_utc="2025-01-01T00:00:00.000Z"
    )
    # This is an input-boundary test. The optional model backend must never
    # be used; any attempted fit against this empty module fails the test.
    monkeypatch.setitem(sys.modules, "catboost", SimpleNamespace())
    monkeypatch.setattr(candidate_cli, "verify_source_revision", lambda *_: "a" * 64)
    payload = {
        "source_commit": "b" * 40,
        "source_repository": str(tmp_path),
        "source_artifact_digest": "a" * 64,
        "cutoff_at_utc": "2025-01-01T00:00:00.000Z",
        "role_year_boundaries": [2020, 2021, 2022],
        "workbook_sha256": history.source_sha256,
        "excluded": {},
        "observations": [item.to_dict() for item in history.observations],
    }
    with pytest.raises(ValueError, match="builder refuses locked-audit"):
        candidate_cli._build_candidate(payload, tmp_path / "candidate")
    assert not (tmp_path / "candidate").exists()
