"""Explicit process inputs for source-bound release evidence."""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
from pathlib import Path

GENERATED_SIDECARS = frozenset(
    {
        "benchmarks/v3/v3_executable_evidence.json",
        "benchmarks/v3/v3_release_attestation.json",
    }
)
GENERATED_PREFIXES = ("benchmarks/v3/release_evidence/", "dist/v3-release/")
PROCESS_ENVIRONMENT_KEYS = frozenset(
    {
        "SystemRoot",
        "WINDIR",
        "PATH",
        "TEMP",
        "TMP",
        "HOME",
        "USERPROFILE",
        "LANG",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD",
        "PYTHONNOUSERSITE",
        "PYTHONDONTWRITEBYTECODE",
        "PIP_CONFIG_FILE",
        "PIP_NO_INDEX",
        "STRATHMARK_TEST_DB",
        "STRATHMARK_DB_PATH",
        "STRATHMARK_V3_DB_PATH",
    }
)


def evidence_environment(overrides: dict[str, str]) -> dict[str, str]:
    """Pass only OS necessities and explicit disposable test paths to children."""

    if set(overrides) != {"STRATHMARK_TEST_DB", "STRATHMARK_DB_PATH", "STRATHMARK_V3_DB_PATH"}:
        raise ValueError("unexpected evidence environment override")
    if overrides["STRATHMARK_TEST_DB"] != "1":
        raise ValueError("release evidence requires disposable test authority")
    temporary = Path(overrides["STRATHMARK_V3_DB_PATH"]).absolute().parent
    temporary.mkdir(parents=True, exist_ok=True)
    environment = {
        "PATH": os.pathsep.join((str(Path(sys.executable).absolute().parent), os.defpath)),
        "TEMP": str(temporary),
        "TMP": str(temporary),
        "HOME": str(temporary),
        "USERPROFILE": str(temporary),
        "LANG": "C.UTF-8",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PIP_CONFIG_FILE": os.devnull,
        "PIP_NO_INDEX": "1",
        **overrides,
    }
    if os.name == "nt":
        # Windows needs its OS directory, but no inherited executable search path.
        windows = os.environ.get("SystemRoot", r"C:\Windows")
        environment.update(SystemRoot=windows, WINDIR=windows)
        environment["PATH"] = os.pathsep.join(
            (str(Path(sys.executable).absolute().parent), str(Path(windows) / "System32"))
        )
    return environment


def isolated_python_argv(argv: list[str]) -> list[str]:
    if not argv or Path(argv[0]).resolve() != Path(sys.executable).resolve():
        raise ValueError("evidence commands require the designated Python executable")
    arguments = argv[2:] if argv[1:2] == ["-I"] else argv[1:]
    if arguments[:1] == ["-B"]:
        arguments = arguments[1:]
    # -I ignores PYTHONDONTWRITEBYTECODE; use the interpreter flag explicitly.
    return [argv[0], "-I", "-B", *arguments]


def pinned_executable(path: Path, expected_sha256: str) -> dict[str, str]:
    if not path.is_absolute() or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise ValueError("native evidence tool requires an absolute path and approved SHA-256")
    resolved = path.resolve(strict=True)
    if not resolved.is_file():
        raise ValueError("native evidence tool is not a regular file")
    observed = hashlib.sha256(resolved.read_bytes()).hexdigest()
    if observed != expected_sha256:
        raise ValueError("native evidence tool digest differs from its approved identity")
    return {"path": str(resolved), "sha256": observed}


def trusted_git_argv(*arguments: str) -> list[str]:
    """Use the OS Git installation and disable repository process hooks."""

    git = Path(r"C:\Program Files\Git\cmd\git.exe") if os.name == "nt" else Path("/usr/bin/git")
    if not git.is_file():
        raise ValueError("release checks require Git at its trusted system installation path")
    return [
        str(git),
        "-c",
        "core.fsmonitor=false",
        "-c",
        f"core.hooksPath={os.devnull}",
        *arguments,
    ]


def git_environment() -> dict[str, str]:
    """Ignore caller repository redirects, command overrides, and global Git settings."""

    environment = {
        "PATH": os.defpath,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
    }
    if os.name == "nt":
        environment["SystemRoot"] = os.environ.get("SystemRoot", r"C:\Windows")
    return environment


def require_clean_release_inputs(root: Path) -> None:
    """Reject unrecorded inputs, including root hooks and ignored source files."""

    environment = git_environment()
    completed = subprocess.run(
        trusted_git_argv("-C", str(root), "status", "--porcelain=v1", "--untracked-files=all"),
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode:
        raise ValueError("release source cleanliness could not be determined")
    for line in completed.stdout.splitlines():
        path = line[3:].strip().strip('"').replace("\\", "/").split(" -> ")[-1]
        if path not in GENERATED_SIDECARS and not path.startswith(GENERATED_PREFIXES):
            raise ValueError("release evidence generation requires committed source inputs")
    ignored = subprocess.run(
        trusted_git_argv(
            "-C",
            str(root),
            "ls-files",
            "--others",
            "--ignored",
            "--exclude-standard",
            "-z",
        ),
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    if ignored.returncode:
        raise ValueError("ignored release inputs could not be determined")
    for name in ignored.stdout.split("\0"):
        parts = Path(name).parts
        if (
            not parts
            or parts[0] in {".git", ".tmp", "dist", "build"}
            or parts[0].startswith(".venv")
        ):
            continue
        if Path(name).suffix.lower() in {".py", ".pyc", ".pyo", ".pyd", ".pth", ".dll", ".so"}:
            raise ValueError("release evidence generation rejects ignored executable inputs")
