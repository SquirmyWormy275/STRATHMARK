"""Automatic publication respects successful main CI and immutable release sources."""

from __future__ import annotations

import hashlib

import pytest

from scripts.prepare_pypi_release import (
    prepare_manual_release,
    prepare_release,
    source_ci_passed,
    successful_main_ci,
)
from scripts.verify_pypi_publication import verify_artifact_metadata
from scripts.verify_release_tag import ReleaseTagError, verify_release_tag
from tests.test_package_release import _git, _release_repository


def test_successful_new_version_creates_and_pushes_exact_annotated_tags(tmp_path):
    repository = _release_repository(tmp_path, version="3.0.0rc8")
    for tag in ("v3.0.0rc8", "pypi-v3.0.0rc8"):
        _git(repository, "tag", "-d", tag)
    remote = tmp_path / "remote.git"
    _git(repository, "init", "--bare", str(remote))
    _git(repository, "remote", "add", "origin", str(remote))
    commit = _git(repository, "rev-parse", "HEAD")

    result = prepare_release(repository, commit, release_exists=lambda version: False)

    assert result["publish"] == "true"
    verified = verify_release_tag(repository, result["release_tag"])
    assert verified.commit == commit
    assert _git(remote, "rev-parse", "refs/tags/v3.0.0rc8^{commit}") == commit
    assert _git(remote, "rev-parse", "refs/tags/pypi-v3.0.0rc8^{commit}") == commit
    assert _git(remote, "cat-file", "-t", "refs/tags/v3.0.0rc8") == "tag"


def test_existing_source_tag_is_preserved_after_documentation_commits(tmp_path):
    repository = _release_repository(tmp_path)
    source = _git(repository, "rev-parse", "HEAD")
    _git(repository, "tag", "-d", "pypi-v2.0.0")
    (repository / "README.md").write_text("Documentation update\n", encoding="utf-8")
    _git(repository, "add", "README.md")
    _git(repository, "commit", "-m", "docs only")
    head = _git(repository, "rev-parse", "HEAD")
    checked = []

    result = prepare_release(
        repository,
        head,
        release_exists=lambda version: False,
        ci_passed=lambda commit: checked.append(commit) or True,
        push=False,
    )

    assert checked == [source]
    assert result["source_commit"] == source
    assert _git(repository, "rev-parse", "pypi-v2.0.0^{commit}") == source


def test_existing_source_requires_its_own_successful_ci(tmp_path):
    repository = _release_repository(tmp_path)
    (repository / "README.md").write_text("Later main\n", encoding="utf-8")
    _git(repository, "add", "README.md")
    _git(repository, "commit", "-m", "later main")

    with pytest.raises(ReleaseTagError, match="no successful main CI"):
        prepare_release(
            repository,
            _git(repository, "rev-parse", "HEAD"),
            release_exists=lambda version: False,
            ci_passed=lambda commit: False,
            push=False,
        )


def test_published_version_does_not_recreate_or_push_tags(tmp_path):
    repository = _release_repository(tmp_path)
    _git(repository, "tag", "-d", "v2.0.0", "pypi-v2.0.0")
    result = prepare_release(
        repository,
        _git(repository, "rev-parse", "HEAD"),
        release_exists=lambda version: version == "2.0.0",
    )
    assert result["publish"] == "false"
    assert not _git(repository, "tag", "--list")


def test_newer_main_waits_for_its_own_ci(tmp_path):
    repository = _release_repository(tmp_path)
    result = prepare_release(repository, "0" * 40, release_exists=lambda version: False)
    assert result["publish"] == "false"


def test_dirty_source_is_rejected(tmp_path):
    repository = _release_repository(tmp_path)
    (repository / "unreviewed.py").write_text("print('unexpected')\n", encoding="utf-8")
    with pytest.raises(ReleaseTagError, match="clean"):
        prepare_release(
            repository,
            _git(repository, "rev-parse", "HEAD"),
            release_exists=lambda version: False,
            push=False,
        )


def test_conflicting_authorization_never_moves_existing_tag(tmp_path):
    repository = _release_repository(tmp_path)
    source = _git(repository, "rev-parse", "HEAD")
    (repository / "README.md").write_text("Later main\n", encoding="utf-8")
    _git(repository, "add", "README.md")
    _git(repository, "commit", "-m", "later main")
    head = _git(repository, "rev-parse", "HEAD")
    _git(repository, "tag", "-f", "-a", "pypi-v2.0.0", head, "-m", "wrong source")

    with pytest.raises(ReleaseTagError, match="different commit"):
        prepare_release(
            repository,
            head,
            release_exists=lambda version: False,
            ci_passed=lambda commit: True,
            push=False,
        )
    assert _git(repository, "rev-parse", "v2.0.0^{commit}") == source
    assert _git(repository, "rev-parse", "pypi-v2.0.0^{commit}") == head


@pytest.mark.parametrize(
    "field,value",
    [("event", "pull_request"), ("head_branch", "feature"), ("conclusion", "failure")],
)
def test_untrusted_ci_events_cannot_authorize_publication(field, value):
    event = {
        "repository": {"full_name": "owner/STRATHMARK"},
        "workflow_run": {
            "head_repository": {"full_name": "owner/STRATHMARK"},
            "head_branch": "main",
            "event": "push",
            "status": "completed",
            "conclusion": "success",
            "head_sha": "1" * 40,
        },
    }
    assert successful_main_ci(event, "owner/STRATHMARK") == "1" * 40
    event["workflow_run"][field] = value
    with pytest.raises(ReleaseTagError, match="successful same-repository main CI"):
        successful_main_ci(event, "owner/STRATHMARK")


def test_fork_ci_cannot_authorize_publication():
    event = {
        "repository": {"full_name": "owner/STRATHMARK"},
        "workflow_run": {"head_repository": {"full_name": "fork/STRATHMARK"}},
    }
    with pytest.raises(ReleaseTagError, match="same-repository"):
        successful_main_ci(event, "owner/STRATHMARK")


def test_manual_recovery_requires_exact_source_ci_and_preserves_tags(tmp_path):
    repository = _release_repository(tmp_path)
    commit = _git(repository, "rev-parse", "HEAD")
    tag_oid = _git(repository, "rev-parse", "pypi-v2.0.0")
    (repository / "README.md").write_text("Later maintained main\n", encoding="utf-8")
    _git(repository, "add", "README.md")
    _git(repository, "commit", "-m", "later main")
    checked = []
    result = prepare_manual_release(
        repository, "pypi-v2.0.0", ci_passed=lambda value: checked.append(value) or True
    )
    assert checked == [commit]
    assert result["source_commit"] == commit
    assert result["authorization_oid"] == tag_oid
    assert _git(repository, "rev-parse", "pypi-v2.0.0") == tag_oid


def test_manual_recovery_rejects_source_without_successful_main_ci(tmp_path):
    repository = _release_repository(tmp_path)
    with pytest.raises(ReleaseTagError, match="no successful same-repository main CI"):
        prepare_manual_release(repository, "pypi-v2.0.0", ci_passed=lambda value: False)


def test_manual_recovery_rejects_tag_outside_main(tmp_path):
    repository = _release_repository(tmp_path)
    _git(repository, "checkout", "--orphan", "unrelated-main")
    _git(repository, "commit", "-m", "unrelated main")
    with pytest.raises(ReleaseTagError, match="not an ancestor"):
        prepare_manual_release(repository, "pypi-v2.0.0", ci_passed=lambda value: True)


@pytest.mark.parametrize(
    "field,value",
    [
        ("head_repository", {"full_name": "attacker/fork"}),
        ("head_sha", "2" * 40),
        ("status", "in_progress"),
        ("event", "pull_request"),
        ("conclusion", "failure"),
    ],
)
def test_recovery_ci_query_rejects_untrusted_run(monkeypatch, field, value):
    import scripts.prepare_pypi_release as module

    run = {
        "head_sha": "1" * 40,
        "head_branch": "main",
        "event": "push",
        "status": "completed",
        "conclusion": "success",
        "head_repository": {"full_name": "owner/STRATHMARK"},
    }
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/STRATHMARK")
    monkeypatch.setenv("GH_TOKEN", "test-only")
    monkeypatch.setattr(module, "_json", lambda *args, **kwargs: {"workflow_runs": [run]})
    assert source_ci_passed("1" * 40)
    run[field] = value
    assert not source_ci_passed("1" * 40)


def test_publication_verification_rejects_missing_yanked_or_substituted_files(tmp_path):
    artifacts = [
        tmp_path / "strathmark-3.0.0rc7-py3-none-any.whl",
        tmp_path / "strathmark-3.0.0rc7.tar.gz",
    ]
    for artifact in artifacts:
        artifact.write_bytes(artifact.name.encode())
    metadata = {
        "info": {"version": "3.0.0rc7"},
        "urls": [
            {
                "filename": artifact.name,
                "yanked": False,
                "digests": {"sha256": hashlib.sha256(artifact.read_bytes()).hexdigest()},
            }
            for artifact in artifacts
        ],
    }
    verify_artifact_metadata(tmp_path, "3.0.0rc7", metadata)
    metadata["urls"][0]["yanked"] = True
    with pytest.raises(ValueError, match="yanked"):
        verify_artifact_metadata(tmp_path, "3.0.0rc7", metadata)
    metadata["urls"][0]["yanked"] = False
    artifacts[0].write_bytes(b"substituted")
    with pytest.raises(ValueError, match="hash differs"):
        verify_artifact_metadata(tmp_path, "3.0.0rc7", metadata)
    metadata["urls"].pop()
    with pytest.raises(ValueError, match="file set"):
        verify_artifact_metadata(tmp_path, "3.0.0rc7", metadata)
