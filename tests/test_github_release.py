"""GitHub publication must preserve the exact public PyPI release on retries."""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest

from scripts.publish_github_release import publish_release, release_notes, run_gh
from scripts.verify_release_tag import ReleaseTagError, verify_release_tag
from tests.test_package_release import _git, _release_repository


def _fixture(tmp_path, version="3.0.0rc9"):
    source = _release_repository(tmp_path, version=version)
    (source / "CHANGELOG.md").write_text(
        f"# Changelog\n\n## [{version}] - 2026-10-13\n\n- Automate releases.\n\n"
        "## [2.0.0] - 2026-01-01\n\n- Old entry.\n",
        encoding="utf-8",
    )
    _git(source, "add", "CHANGELOG.md")
    _git(source, "commit", "--amend", "--no-edit")
    for tag in (f"v{version}", f"pypi-v{version}"):
        _git(source, "tag", "-d", tag)
        _git(source, "tag", "-a", tag, "-m", "verified release")
    dist = tmp_path / "dist"
    dist.mkdir()
    for name in (f"strathmark-{version}-py3-none-any.whl", f"strathmark-{version}.tar.gz"):
        (dist / name).write_bytes(name.encode())
    metadata = {
        "info": {"version": version},
        "urls": [
            {"filename": p.name, "digests": {"sha256": hashlib.sha256(p.read_bytes()).hexdigest()}}
            for p in sorted(dist.iterdir())
        ],
    }
    return source, dist, metadata


class GitHub:
    def __init__(self, tag, *, draft=True):
        self.release = {"tag_name": tag, "draft": draft, "prerelease": "rc" in tag, "assets": []}
        self.release.update(name="STRATHMARK " + tag.removeprefix("v"), body="")
        self.release["id"] = 42
        self.exists = False
        self.commands = []
        self.notes = ""

    def __call__(self, args):
        self.commands.append(args)
        if args[0] == "api":
            if "/releases?" in args[1]:
                return [[self.release]] if self.exists else [[]]
            if "/releases/tags/" in args[1] and self.release["draft"]:
                raise subprocess.CalledProcessError(1, args, stderr="HTTP 404: Not Found")
            if not self.exists:
                raise subprocess.CalledProcessError(1, args, stderr="HTTP 404: Not Found")
            return self.release
        if args[:2] == ["release", "create"]:
            if self.exists:
                raise subprocess.CalledProcessError(1, args, stderr="HTTP 422: Already exists")
            self.exists = True
            self.notes = Path(args[args.index("--notes-file") + 1]).read_text()
            self.release["body"] = self.notes
        elif args[:2] == ["release", "upload"]:
            asset = Path(args[3])
            self.release["assets"].append(
                {
                    "name": asset.name,
                    "size": asset.stat().st_size,
                    "state": "uploaded",
                    "digest": "sha256:" + hashlib.sha256(asset.read_bytes()).hexdigest(),
                }
            )
        elif args[:2] == ["release", "edit"]:
            assert len(self.release["assets"]) == 3
            self.release["draft"] = False
        else:
            pytest.fail(f"unexpected mutation: {args}")
        return None


def test_creates_verified_draft_attaches_exact_files_then_publishes(tmp_path):
    source, dist, metadata = _fixture(tmp_path)
    github = GitHub("v3.0.0rc9")
    publish_release(
        source, dist, "pypi-v3.0.0rc9", "owner/STRATHMARK", gh=github, metadata=metadata
    )
    create = next(args for args in github.commands if args[:2] == ["release", "create"])
    assert "--draft" in create and "--verify-tag" in create and "--prerelease" in create
    assert "--latest=false" in create
    assert "Automate releases." in github.notes and "Old entry." not in github.notes
    assert "https://pypi.org/project/strathmark/3.0.0rc9/" in github.notes
    assert not github.release["draft"]
    before = len(github.commands)
    publish_release(
        source, dist, "pypi-v3.0.0rc9", "owner/STRATHMARK", gh=github, metadata=metadata
    )
    assert len(github.commands) == before + 1


def test_draft_retry_uploads_only_missing_assets(tmp_path):
    source, dist, metadata = _fixture(tmp_path)
    github = GitHub("v3.0.0rc9")
    github.exists = True
    github.release["body"] = release_notes(source, verify_release_tag(source, "pypi-v3.0.0rc9"))
    artifact = next(dist.glob("*.whl"))
    github(["release", "upload", "v3.0.0rc9", str(artifact)])
    github.commands.clear()
    publish_release(
        source, dist, "pypi-v3.0.0rc9", "owner/STRATHMARK", gh=github, metadata=metadata
    )
    uploaded = [Path(args[3]).name for args in github.commands if args[:2] == ["release", "upload"]]
    assert artifact.name not in uploaded and len(uploaded) == 2


@pytest.mark.parametrize("draft", [True, False])
def test_conflicting_asset_is_never_replaced(tmp_path, draft):
    source, dist, metadata = _fixture(tmp_path)
    github = GitHub("v3.0.0rc9", draft=draft)
    github.exists = True
    github.release["body"] = release_notes(source, verify_release_tag(source, "pypi-v3.0.0rc9"))
    artifact = next(dist.glob("*.whl"))
    github(["release", "upload", "v3.0.0rc9", str(artifact)])
    github.release["assets"][0]["digest"] = "sha256:" + "0" * 64
    github.commands.clear()
    with pytest.raises(ValueError, match="differs"):
        publish_release(
            source, dist, "pypi-v3.0.0rc9", "owner/STRATHMARK", gh=github, metadata=metadata
        )
    assert all(args[0] == "api" for args in github.commands)


def test_pypi_substitution_cannot_create_github_release(tmp_path):
    source, dist, metadata = _fixture(tmp_path)
    github = GitHub("v3.0.0rc9")
    metadata["urls"][0]["digests"]["sha256"] = "0" * 64
    with pytest.raises(ValueError, match="hash differs"):
        publish_release(
            source, dist, "pypi-v3.0.0rc9", "owner/STRATHMARK", gh=github, metadata=metadata
        )
    assert not github.commands


@pytest.mark.parametrize("move_source", [False, True])
def test_tag_change_after_build_cannot_publish_github_release(tmp_path, move_source):
    source, dist, metadata = _fixture(tmp_path)
    commit = _git(source, "rev-parse", "HEAD")
    authorization_oid = _git(source, "rev-parse", "refs/tags/pypi-v3.0.0rc9")
    if move_source:
        (source / "unreviewed.txt").write_text("changed after CI", encoding="utf-8")
        _git(source, "add", "unreviewed.txt")
        _git(source, "commit", "-m", "unreviewed source")
        _git(source, "tag", "-f", "-a", "v3.0.0rc9", "-m", "moved source")
    _git(source, "tag", "-f", "-a", "pypi-v3.0.0rc9", "-m", "moved authorization")
    github = GitHub("v3.0.0rc9")
    with pytest.raises(ReleaseTagError, match="CI-authorized commit|changed after CI"):
        publish_release(
            source,
            dist,
            "pypi-v3.0.0rc9",
            "owner/STRATHMARK",
            expected_commit=commit,
            expected_authorization_oid=authorization_oid,
            gh=github,
            metadata=metadata,
        )
    assert not github.commands


def test_stable_version_does_not_force_prerelease_or_latest(tmp_path):
    source, dist, metadata = _fixture(tmp_path, "2.0.2")
    github = GitHub("v2.0.2")
    publish_release(source, dist, "pypi-v2.0.2", "owner/STRATHMARK", gh=github, metadata=metadata)
    create = next(args for args in github.commands if args[:2] == ["release", "create"])
    assert "--prerelease" not in create and "--latest=false" not in create


def test_api_errors_cannot_be_misread_as_missing_release(tmp_path):
    source, dist, metadata = _fixture(tmp_path)

    def forbidden(args):
        raise subprocess.CalledProcessError(1, args, stderr="HTTP 403: Forbidden")

    with pytest.raises(subprocess.CalledProcessError):
        publish_release(
            source, dist, "pypi-v3.0.0rc9", "owner/STRATHMARK", gh=forbidden, metadata=metadata
        )


def test_github_cli_wrapper_decodes_issue_list_json(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, "[]"))
    assert run_gh(["issue", "list", "--json", "number,title,body"]) == []


def test_draft_lookup_uses_authenticated_listing_and_id_not_published_only_tag_endpoint(tmp_path):
    source, dist, metadata = _fixture(tmp_path)
    github = GitHub("v3.0.0rc9")
    publish_release(
        source, dist, "pypi-v3.0.0rc9", "owner/STRATHMARK", gh=github, metadata=metadata
    )
    reads = [args[1] for args in github.commands if args[0] == "api"]
    assert any("/releases?" in endpoint for endpoint in reads)
    assert any(endpoint.endswith("/releases/42") for endpoint in reads)
    assert all("/releases/tags/" not in endpoint for endpoint in reads)


@pytest.mark.parametrize("draft", [True, False])
@pytest.mark.parametrize("field", ["name", "body"])
def test_existing_release_notes_and_title_must_match_exact_source(tmp_path, draft, field):
    source, dist, metadata = _fixture(tmp_path)
    github = GitHub("v3.0.0rc9", draft=draft)
    github.exists = True
    github.release["body"] = release_notes(source, verify_release_tag(source, "pypi-v3.0.0rc9"))
    github.release[field] = "Stale manual content"
    with pytest.raises(ValueError, match="notes or title"):
        publish_release(
            source, dist, "pypi-v3.0.0rc9", "owner/STRATHMARK", gh=github, metadata=metadata
        )
    assert len(github.commands) == 1
