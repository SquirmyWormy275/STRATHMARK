"""Build exact source using reviewed, hash-locked build wheels."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from scripts.verify_toolchain import verify_toolchain


def clean_source(source: Path) -> str:
    status = subprocess.run(
        ["git", "-C", str(source), "status", "--porcelain", "--untracked-files=all"],
        check=True,
        capture_output=True,
        text=True,
    )
    if status.stdout:
        raise ValueError("release source must remain clean before and after building")
    return subprocess.run(
        ["git", "-C", str(source), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def build_release(source: Path, wheelhouse: Path, provenance: Path) -> dict:
    tooling = Path(__file__).resolve().parents[1]
    lock = tooling / "requirements/release-build-tools.lock"
    required = (tooling / "requirements/release-build-tools.in",)
    source = source.resolve()
    commit = clean_source(source)
    wheelhouse = wheelhouse.resolve()
    wheelhouse.mkdir(parents=True, exist_ok=True, mode=0o700)
    if any(wheelhouse.iterdir()):
        raise ValueError("release build requires a fresh wheelhouse")
    subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            "-m",
            "pip",
            "download",
            "--require-hashes",
            "--only-binary=:all:",
            "-r",
            str(lock),
            "-d",
            str(wheelhouse),
        ],
        check=True,
    )
    verify_toolchain(lock, wheelhouse, required=required)
    subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            "-m",
            "pip",
            "install",
            "--no-index",
            "--find-links",
            str(wheelhouse),
            "--require-hashes",
            "--force-reinstall",
            "--no-compile",
            "-r",
            str(lock),
        ],
        check=True,
    )
    before = verify_toolchain(lock, wheelhouse, installed=True, required=required)
    environment = dict(
        os.environ, PIP_NO_INDEX="1", PYTHONNOUSERSITE="1", PYTHONDONTWRITEBYTECODE="1"
    )
    subprocess.run(
        [sys.executable, "-I", "-B", "-m", "build", "--no-isolation", str(source)],
        check=True,
        env=environment,
    )
    artifacts = sorted((source / "dist").glob("*"))
    if len(artifacts) != 2 or {path.suffix for path in artifacts} != {".whl", ".gz"}:
        raise ValueError("release build must produce exactly one wheel and one sdist")
    subprocess.run(
        [sys.executable, "-I", "-B", "-m", "twine", "check", *map(str, artifacts)],
        check=True,
        env=environment,
    )
    after = verify_toolchain(lock, wheelhouse, installed=True, required=required)
    if before != after or clean_source(source) != commit:
        raise ValueError("release source or build tool bytes changed during building")
    result = {"source_commit": commit, "toolchain": after}
    provenance.parent.mkdir(parents=True, exist_ok=True)
    provenance.write_text(json.dumps(result, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path.cwd())
    parser.add_argument("--wheelhouse", type=Path, required=True)
    parser.add_argument("--provenance", type=Path, required=True)
    args = parser.parse_args()
    build_release(args.source, args.wheelhouse, args.provenance)


if __name__ == "__main__":
    main()
