"""Publish GitHub release assets only after proving their exact public PyPI hashes."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import tempfile
import urllib.request
from collections.abc import Callable
from pathlib import Path

from scripts.verify_pypi_publication import verify_artifact_metadata
from scripts.verify_release_tag import VerifiedReleaseTag, verify_release_tag


def run_gh(args: list[str]) -> dict | list | None:
    reply = subprocess.run(["gh", *args], check=True, capture_output=True, text=True)
    return json.loads(reply.stdout) if args[0] == "api" or "--json" in args else None


def release_notes(source: Path, release: VerifiedReleaseTag) -> str:
    changelog = (source / "CHANGELOG.md").read_text(encoding="utf-8")
    match = re.search(
        rf"^## \[{re.escape(release.version)}\][^\n]*\n(.*?)(?=^## |\Z)",
        changelog,
        flags=re.MULTILINE | re.DOTALL,
    )
    if not match or not match[1].strip():
        raise ValueError("exact source changelog has no notes for this version")
    return (
        match[1].strip()
        + f"\n\nInstall this release:\n\n```bash\npython -m pip install strathmark=={release.version}\n```\n"
        + f"\n[Verified PyPI release](https://pypi.org/project/strathmark/{release.version}/). "
        + "The attached wheel and source archive match the verified PyPI files; "
        + "SHA256SUMS records their hashes.\n"
        + f"\nImmutable source: `{release.commit}` (`{release.source_tag}`).\n"
        + "\nPublication does not change a competition's selected engine or grant "
        + "Windows CNG production qualification.\n"
    )


def validate_assets(release: dict, expected: dict[str, tuple[str, int]], *, complete: bool) -> None:
    assets = {asset["name"]: asset for asset in release.get("assets", [])}
    if len(assets) != len(release.get("assets", [])) or not set(assets).issubset(expected):
        raise ValueError("GitHub release has unexpected or duplicate assets")
    for name, asset in assets.items():
        digest, size = expected[name]
        if (
            asset.get("digest") != f"sha256:{digest}"
            or asset.get("size") != size
            or asset.get("state") != "uploaded"
        ):
            raise ValueError(f"GitHub release asset differs from verified PyPI artifacts: {name}")
    if complete and set(assets) != set(expected):
        raise ValueError("GitHub release is missing verified assets")


def publish_release(
    source: Path,
    dist: Path,
    authorization_tag: str,
    repository: str,
    *,
    gh: Callable[[list[str]], dict | None] = run_gh,
    metadata: dict | None = None,
) -> None:
    verified = verify_release_tag(source, authorization_tag)
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("expected an owner/repository identity")
    if not re.fullmatch(
        r"[0-9]+(?:\.[0-9]+)*(?:(?:a|b|rc)[0-9]+)?(?:\.post[0-9]+)?(?:\.dev[0-9]+)?",
        verified.version,
    ):
        raise ValueError("release version is not canonical")
    if metadata is None:
        with urllib.request.urlopen(
            f"https://pypi.org/pypi/strathmark/{verified.version}/json", timeout=30
        ) as reply:
            metadata = json.load(reply)
    verify_artifact_metadata(dist, verified.version, metadata)
    artifacts = sorted([*dist.glob("*.whl"), *dist.glob("*.tar.gz")])
    expected = {
        p.name: (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_size) for p in artifacts
    }
    prerelease = bool(re.search(r"(?:a|b|rc)[0-9]+|\.dev[0-9]+", verified.version))
    endpoint = f"repos/{repository}/releases/tags/{verified.source_tag}"
    try:
        existing = gh(["api", endpoint])
    except subprocess.CalledProcessError as error:
        if "HTTP 404" not in (error.stderr or ""):
            raise
        existing = None
    with tempfile.TemporaryDirectory(prefix="strathmark-github-release-") as directory:
        root = Path(directory)
        checksums = root / "SHA256SUMS"
        checksums.write_text(
            "".join(f"{digest}  {name}\n" for name, (digest, _) in expected.items()),
            encoding="utf-8",
        )
        expected[checksums.name] = (
            hashlib.sha256(checksums.read_bytes()).hexdigest(),
            checksums.stat().st_size,
        )
        if existing is None:
            notes = root / "release-notes.md"
            notes.write_text(release_notes(source, verified), encoding="utf-8")
            command = [
                "release",
                "create",
                verified.source_tag,
                "--repo",
                repository,
                "--draft",
                "--verify-tag",
                "--title",
                f"STRATHMARK {verified.version}",
                "--notes-file",
                str(notes),
            ]
            if prerelease:
                command += ["--prerelease", "--latest=false"]
            gh(command)
            existing = gh(["api", endpoint])
        if existing["tag_name"] != verified.source_tag or existing["prerelease"] != prerelease:
            raise ValueError("GitHub release tag or prerelease classification differs")
        validate_assets(existing, expected, complete=not existing["draft"])
        if not existing["draft"]:
            print(f"GitHub {verified.source_tag}: published assets already match PyPI")
            return
        present = {asset["name"] for asset in existing["assets"]}
        for artifact in [*artifacts, checksums]:
            if artifact.name not in present:
                gh(["release", "upload", verified.source_tag, str(artifact), "--repo", repository])
        validate_assets(gh(["api", endpoint]), expected, complete=True)
        command = ["release", "edit", verified.source_tag, "--repo", repository, "--draft=false"]
        if prerelease:
            command.append("--latest=false")
        gh(command)
        published = gh(["api", endpoint])
        if published["draft"]:
            raise ValueError("GitHub release remained a draft")
        validate_assets(published, expected, complete=True)
    print(f"GitHub {verified.source_tag}: published exact verified PyPI artifacts and checksums")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--dist-dir", type=Path, required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--repository", required=True)
    args = parser.parse_args(argv)
    publish_release(args.source, args.dist_dir, args.tag, args.repository)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
