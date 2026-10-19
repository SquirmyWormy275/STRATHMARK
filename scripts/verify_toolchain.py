"""Verify locked wheel bytes and optionally their installed package contents."""

from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.metadata
import io
import json
import os
import platform
import re
import stat
import sys
import zipfile
from email.parser import BytesParser
from pathlib import Path, PurePosixPath


def _name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _bytes(path: Path, limit: int = 256 * 1024 * 1024) -> bytes:
    descriptor = os.open(
        path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    )
    with os.fdopen(descriptor, "rb") as stream:
        status = os.fstat(stream.fileno())
        if not stat.S_ISREG(status.st_mode) or status.st_size > limit:
            raise ValueError("toolchain input must be a bounded regular file")
        raw = stream.read(limit + 1)
        if len(raw) > limit:
            raise ValueError("toolchain input exceeds its byte bound")
        after = os.fstat(stream.fileno())
        if (status.st_size, status.st_mtime_ns, status.st_ctime_ns) != (
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise ValueError("toolchain input changed while being read")
        return raw


def _marker_applies(marker: str) -> bool:
    """Evaluate the equality-only platform markers emitted by the reviewed lock.

    No dependency code is imported before its bytes are verified. Unknown marker
    syntax is refused so a future lock format needs deliberate review.
    """
    environment = {
        "os_name": os.name,
        "sys_platform": sys.platform,
        "platform_machine": platform.machine(),
        "platform_python_implementation": platform.python_implementation(),
        "implementation_name": sys.implementation.name,
        "python_version": ".".join(map(str, sys.version_info[:2])),
        "python_full_version": platform.python_version(),
    }

    def evaluate(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.Name) and node.id in environment:
            return environment[node.id]
        if isinstance(node, ast.BoolOp) and isinstance(node.op, (ast.And, ast.Or)):
            values = [evaluate(value) for value in node.values]
            if not all(isinstance(value, bool) for value in values):
                raise ValueError("unsupported toolchain lock marker")
            return all(values) if isinstance(node.op, ast.And) else any(values)
        if isinstance(node, ast.Compare) and len(node.ops) == 1:
            left, right = evaluate(node.left), evaluate(node.comparators[0])
            if not isinstance(left, str) or not isinstance(right, str):
                raise ValueError("unsupported toolchain lock marker")
            if isinstance(node.ops[0], ast.Eq):
                return left == right
            if isinstance(node.ops[0], ast.NotEq):
                return left != right
        raise ValueError("unsupported toolchain lock marker")

    try:
        result = evaluate(ast.parse(marker, mode="eval").body)
    except SyntaxError as error:
        raise ValueError("unsupported toolchain lock marker") from error
    if not isinstance(result, bool):
        raise ValueError("unsupported toolchain lock marker")
    return result


def locked_hashes(path: Path) -> dict[tuple[str, str], set[str]]:
    result = {}
    for requirement in _bytes(path, 2 * 1024 * 1024).decode().replace("\\\n", " ").splitlines():
        requirement = requirement.strip()
        if not requirement or requirement.startswith("#"):
            continue
        match = re.fullmatch(
            r"([A-Za-z0-9_.-]+)==([^\s;]+)(?:\s*;\s*(.*?))?((?:\s+--hash=sha256:[0-9a-f]{64})+)",
            requirement,
        )
        if match is None:
            raise ValueError("toolchain lock must contain exact versions and SHA-256 hashes")
        if match[3] is not None and not _marker_applies(match[3]):
            continue
        hashes = set(re.findall(r"--hash=sha256:([0-9a-f]{64})(?=\s|$)", match[4]))
        identity = (_name(match[1]), match[2])
        result.setdefault(identity, set()).update(hashes)
    if not result:
        raise ValueError("toolchain hash lock is empty")
    return result


def verify_toolchain(
    lock: Path, wheelhouse: Path, *, installed: bool = False, required: tuple[Path, ...] = ()
) -> dict:
    allowed = locked_hashes(lock)
    artifacts = {}
    installed_files = {}
    packages = set()
    expected_paths: set[Path] = set()
    package_roots: set[Path] = set()
    for path in sorted(wheelhouse.glob("*.whl")):
        raw = _bytes(path)
        digest = hashlib.sha256(raw).hexdigest()
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            metadata_names = [
                name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
            ]
            if len(metadata_names) != 1:
                raise ValueError("toolchain wheel metadata is ambiguous")
            metadata = BytesParser().parsebytes(archive.read(metadata_names[0]))
            identity = (_name(metadata["Name"]), metadata["Version"])
            if digest not in allowed.get(identity, set()) or any(
                name == identity[0] for name, _version in packages
            ):
                raise ValueError("toolchain wheel differs from its trusted hash lock")
            packages.add(identity)
            artifacts[path.name] = digest
            if not installed:
                continue
            distribution = importlib.metadata.distribution(metadata["Name"])
            if distribution.version != identity[1]:
                raise ValueError("installed toolchain version differs from the verified wheel")
            for member in archive.infolist():
                if member.is_dir() or member.filename.endswith(".dist-info/RECORD"):
                    continue
                relative = PurePosixPath(member.filename)
                if relative.is_absolute() or ".." in relative.parts:
                    raise ValueError("toolchain wheel contains an unsafe member")
                if relative.parts[0].endswith(".data"):
                    # Proof tools are invoked through the pinned Python module,
                    # never wheel-generated console wrappers with rewritten shebangs.
                    if len(relative.parts) < 3 or relative.parts[1] not in {"purelib", "platlib"}:
                        continue
                    relative = PurePosixPath(*relative.parts[2:])
                expected = hashlib.sha256(archive.read(member)).hexdigest()
                installed_path = Path(distribution.locate_file(str(relative)))
                actual = hashlib.sha256(_bytes(installed_path)).hexdigest()
                expected_paths.add(installed_path.absolute())
                if not relative.parts[0].endswith(".dist-info"):
                    package_roots.add(Path(distribution.locate_file(relative.parts[0])).absolute())
                if actual != expected:
                    raise ValueError(f"installed toolchain bytes differ: {identity[0]}/{relative}")
                installed_files[f"{identity[0]}/{relative}"] = actual
    if not artifacts:
        raise ValueError("verified toolchain wheelhouse is empty")
    if packages != set(allowed):
        raise ValueError("wheelhouse is missing a required exact toolchain package")
    for path in required:
        for line in path.read_text().splitlines():
            if line.strip() and not line.lstrip().startswith("#"):
                name, version = line.strip().split("==")
                if (_name(name), version) not in packages:
                    raise ValueError("wheelhouse is missing a required exact toolchain package")
    # A substituted __init__.py is not the only import attack: an extra module
    # or cached bytecode could also run. Fresh proof environments use --no-compile
    # and -B. Check the union for shared namespace packages across locked wheels.
    for package_root in package_roots:
        if package_root.is_symlink():
            raise ValueError("installed toolchain contains a linked package directory")
        if package_root.is_dir():
            for directory, directories, files in os.walk(package_root, followlinks=False):
                if any((Path(directory) / name).is_symlink() for name in directories):
                    raise ValueError("installed toolchain contains a linked package directory")
                for name in files:
                    if (Path(directory) / name).absolute() not in expected_paths:
                        raise ValueError(f"installed toolchain contains an unexpected file: {name}")
        elif package_root.suffix == ".py":
            if package_root.with_suffix(".pyc").exists() or any(
                (package_root.parent / "__pycache__").glob(f"{package_root.stem}.*.pyc")
            ):
                raise ValueError("installed toolchain contains unverified cached bytecode")
    return {
        "schema_version": "strathmark-verified-toolchain-v1",
        "lock_sha256": hashlib.sha256(_bytes(lock)).hexdigest(),
        "wheel_sha256": artifacts,
        "installed_files_sha256": hashlib.sha256(
            json.dumps(installed_files, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        if installed
        else None,
        "python_sha256": hashlib.sha256(_bytes(Path(os.sys.executable).resolve())).hexdigest(),
    }


def qualification_toolchain(root: Path) -> dict:
    """Verify separately provisioned qualification artifacts; never bless observations."""
    wheelhouse = os.environ.get("STRATHMARK_VERIFIED_WHEELHOUSE")
    expected_python = os.environ.get("STRATHMARK_EVIDENCE_PYTHON_SHA256", "")
    if not wheelhouse or not Path(wheelhouse).is_absolute():
        raise ValueError("qualification requires an absolute STRATHMARK_VERIFIED_WHEELHOUSE")
    if re.fullmatch(r"[0-9a-f]{64}", expected_python) is None:
        raise ValueError("qualification requires an approved STRATHMARK_EVIDENCE_PYTHON_SHA256")
    observed_python = hashlib.sha256(_bytes(Path(sys.executable).resolve())).hexdigest()
    if observed_python != expected_python:
        raise ValueError("qualification Python differs from its approved digest")
    identity = verify_toolchain(
        root / "requirements/v3-verified.lock",
        Path(wheelhouse),
        installed=True,
        required=(
            root / "requirements/v3-release.lock",
            root / "requirements/v3-evidence-tools.lock",
        ),
    )
    if identity["python_sha256"] != expected_python:
        raise ValueError("qualification Python changed during verification")
    return identity


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--wheelhouse", type=Path, required=True)
    parser.add_argument("--installed", action="store_true")
    parser.add_argument("--required", type=Path, action="append", default=[])
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = verify_toolchain(
        args.lock, args.wheelhouse, installed=args.installed, required=tuple(args.required)
    )
    encoded = json.dumps(result, sort_keys=True, indent=2) + "\n"
    if args.output:
        args.output.write_text(encoded)
    else:
        print(encoded, end="")


if __name__ == "__main__":
    main()
