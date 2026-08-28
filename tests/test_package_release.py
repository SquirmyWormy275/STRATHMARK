"""Release automation must publish only reviewed, immutable package sources."""

from __future__ import annotations

import subprocess
import zipfile
from pathlib import Path

import pytest

from scripts.smoke_installed_distribution import declared_wheel_extras
from scripts.verify_release_tag import ReleaseTagError, tomllib, verify_release_tag


def _git(repository: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repository), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _release_repository(
    tmp_path: Path, *, version: str = "2.0.0", tag_version: str | None = None
) -> Path:
    repository = tmp_path / "release-repository"
    repository.mkdir()
    _git(repository, "init")
    _git(repository, "config", "user.name", "Release Test")
    _git(repository, "config", "user.email", "release-test@example.invalid")
    (repository / "pyproject.toml").write_text(
        f'[project]\nname = "strathmark"\nversion = "{version}"\n',
        encoding="utf-8",
    )
    _git(repository, "add", "pyproject.toml")
    _git(repository, "commit", "-m", "release source")
    release_version = tag_version or version
    _git(repository, "tag", f"v{release_version}")
    _git(
        repository,
        "tag",
        "-a",
        f"pypi-v{release_version}",
        "-m",
        f"Authorize PyPI {release_version}",
    )
    return repository


def test_release_tag_accepts_annotated_authorization_over_exact_source_tag(tmp_path):
    repository = _release_repository(tmp_path)

    verified = verify_release_tag(repository, "pypi-v2.0.0")

    assert verified.version == "2.0.0"
    assert verified.authorization_tag == "pypi-v2.0.0"
    assert verified.source_tag == "v2.0.0"
    assert verified.commit == _git(repository, "rev-parse", "HEAD")


def test_release_tag_rejects_lightweight_authorization_tag(tmp_path):
    repository = _release_repository(tmp_path)
    _git(repository, "tag", "-d", "pypi-v2.0.0")
    _git(repository, "tag", "pypi-v2.0.0")

    with pytest.raises(ReleaseTagError, match="annotated"):
        verify_release_tag(repository, "pypi-v2.0.0")


def test_release_tag_rejects_project_version_mismatch(tmp_path):
    repository = _release_repository(tmp_path, version="2.0.1", tag_version="2.0.0")

    with pytest.raises(ReleaseTagError, match="does not match"):
        verify_release_tag(repository, "pypi-v2.0.0")


def test_release_tag_rejects_authorization_tag_for_another_checkout(tmp_path):
    repository = _release_repository(tmp_path)
    (repository / "later.txt").write_text("later\n", encoding="utf-8")
    _git(repository, "add", "later.txt")
    _git(repository, "commit", "-m", "later commit")

    with pytest.raises(ReleaseTagError, match="checked-out HEAD"):
        verify_release_tag(repository, "pypi-v2.0.0")


def test_release_tag_rejects_dirty_checkout(tmp_path):
    repository = _release_repository(tmp_path)
    (repository / "pyproject.toml").write_text(
        '[project]\nname = "strathmark"\nversion = "2.0.0"\n# dirty\n',
        encoding="utf-8",
    )

    with pytest.raises(ReleaseTagError, match="clean"):
        verify_release_tag(repository, "pypi-v2.0.0")


def test_publication_workflows_are_tag_bound_and_rehearse_exact_artifacts():
    root = Path(__file__).resolve().parents[1]
    publish = (root / ".github/workflows/publish.yml").read_text(encoding="utf-8")
    test_publish = (root / ".github/workflows/test-publish.yml").read_text(encoding="utf-8")

    for workflow in (publish, test_publish):
        assert "release_tag:" in workflow
        assert "refs/tags/${{ inputs.release_tag }}" in workflow
        assert "scripts/verify_release_tag.py" in workflow
        assert "python -m twine check dist/*" in workflow
        assert "--kind wheel" in workflow
        assert "--kind sdist" in workflow
        assert "--all-extras" in workflow
        assert "id-token: write" in workflow
        assert '--tag "${{ inputs.release_tag }}"' not in workflow
        assert "RELEASE_TAG: ${{ inputs.release_tag }}" in workflow

    assert "environment: pypi" in publish
    assert "repository-url: https://test.pypi.org/legacy/" in test_publish
    assert "environment: testpypi" in test_publish


def test_current_metadata_does_not_claim_all_v3_authority_is_os_independent():
    root = Path(__file__).resolve().parents[1]
    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
    release_doc = (root / "docs/PACKAGE_RELEASE.md").read_text(encoding="utf-8")

    assert '"Operating System :: OS Independent"' not in pyproject
    assert "V2 portable library" in release_doc
    assert "V3 race-day authority" in release_doc
    assert "Windows" in release_doc


def test_sdist_uses_an_explicit_source_allowlist():
    root = Path(__file__).resolve().parents[1]
    pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))

    included = pyproject["tool"]["hatch"]["build"]["targets"]["sdist"]["include"]

    assert "/strathmark" in included
    assert "/pyproject.toml" in included
    assert "/.tmp" not in included
    assert "/.pytest-*" not in included


def test_release_scratch_and_test_artifacts_are_vcs_ignored():
    root = Path(__file__).resolve().parents[1]
    ignored = (root / ".gitignore").read_text(encoding="utf-8").splitlines()

    assert ".tmp/" in ignored
    assert ".tmp-*/" in ignored
    assert ".pytest-*/" in ignored
    assert ".pytest_tmp_*/" in ignored
    assert ".deployment-*/" in ignored


def test_declared_wheel_extras_come_from_exact_artifact_metadata(tmp_path):
    wheel = tmp_path / "strathmark-2.0.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(
            "strathmark-2.0.0.dist-info/METADATA",
            "Metadata-Version: 2.4\n"
            "Name: strathmark\n"
            "Version: 2.0.0\n"
            "Provides-Extra: ml\n"
            "Provides-Extra: api\n"
            "Provides-Extra: ml\n",
        )

    assert declared_wheel_extras(wheel) == ("api", "ml")
