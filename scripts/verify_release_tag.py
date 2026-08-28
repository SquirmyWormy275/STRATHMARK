"""Fail closed unless a checkout is authorized by an exact annotated PyPI tag."""

from __future__ import annotations

import argparse
import subprocess
from dataclasses import dataclass
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised by the Python 3.10 CI lane
    import tomli as tomllib


class ReleaseTagError(RuntimeError):
    """The requested release source is not safe to publish."""


@dataclass(frozen=True)
class VerifiedReleaseTag:
    authorization_tag: str
    source_tag: str
    version: str
    commit: str


def _git(repository: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repository), *args],
        check=check,
        capture_output=True,
        text=True,
    )


def _rev_parse(repository: Path, revision: str) -> str:
    completed = _git(repository, "rev-parse", "--verify", revision, check=False)
    if completed.returncode:
        raise ReleaseTagError(f"missing required Git revision: {revision}")
    return completed.stdout.strip()


def verify_release_tag(repository: Path, authorization_tag: str) -> VerifiedReleaseTag:
    repository = repository.resolve()
    prefix = "pypi-v"
    if not authorization_tag.startswith(prefix) or len(authorization_tag) == len(prefix):
        raise ReleaseTagError("publication tag must use the pypi-v<project-version> form")
    if any(character in authorization_tag for character in ("/", "\\", " ", "\t", "\n")):
        raise ReleaseTagError("publication tag contains an unsafe character")

    version = authorization_tag.removeprefix(prefix)
    source_tag = f"v{version}"
    authorization_ref = f"refs/tags/{authorization_tag}"
    source_ref = f"refs/tags/{source_tag}"

    object_type = _git(repository, "cat-file", "-t", authorization_ref, check=False)
    if object_type.returncode or object_type.stdout.strip() != "tag":
        raise ReleaseTagError(
            f"publication authorization tag {authorization_tag!r} must be annotated"
        )

    authorization_commit = _rev_parse(repository, f"{authorization_ref}^{{commit}}")
    source_commit = _rev_parse(repository, f"{source_ref}^{{commit}}")
    head_commit = _rev_parse(repository, "HEAD^{commit}")
    if authorization_commit != source_commit:
        raise ReleaseTagError(
            f"publication tag {authorization_tag!r} does not authorize exact source tag {source_tag!r}"
        )
    if authorization_commit != head_commit:
        raise ReleaseTagError(
            f"publication tag {authorization_tag!r} does not identify checked-out HEAD"
        )

    pyproject = repository / "pyproject.toml"
    try:
        project_version = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["version"]
    except (OSError, KeyError, tomllib.TOMLDecodeError) as error:
        raise ReleaseTagError(
            "cannot read a static [project].version from pyproject.toml"
        ) from error
    if project_version != version:
        raise ReleaseTagError(
            f"publication tag version {version!r} does not match project version {project_version!r}"
        )

    dirty = _git(repository, "status", "--porcelain", "--untracked-files=all")
    if dirty.stdout:
        raise ReleaseTagError("release checkout must be clean before artifact assembly")

    return VerifiedReleaseTag(
        authorization_tag=authorization_tag,
        source_tag=source_tag,
        version=version,
        commit=head_commit,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, default=Path.cwd())
    parser.add_argument("--tag", required=True)
    args = parser.parse_args(argv)
    verified = verify_release_tag(args.repository, args.tag)
    print(
        f"release source verified: {verified.source_tag} / "
        f"{verified.authorization_tag} / {verified.commit}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
