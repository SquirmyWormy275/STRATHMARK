"""Small executable probes used by the V3 release-evidence runner."""

from __future__ import annotations

import argparse
import json
import re
import sys
from importlib.metadata import PackageNotFoundError, version
from importlib.resources import files
from pathlib import Path


def _probe_dependencies(lock_path: Path) -> dict[str, object]:
    packages: dict[str, str] = {}
    for raw in lock_path.read_text("utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.count("==") != 1:
            raise RuntimeError("dependency lock is not exact")
        name, expected = line.split("==")
        canonical = re.sub(r"[-_.]+", "-", name).lower()
        if not canonical or canonical in packages:
            raise RuntimeError("dependency lock identity differs")
        try:
            observed = version(name)
        except PackageNotFoundError as exc:
            raise RuntimeError(f"dependency is not installed: {canonical}") from exc
        if observed != expected:
            raise RuntimeError(f"dependency version differs: {canonical}")
        packages[canonical] = observed
    if not packages:
        raise RuntimeError("dependency lock is empty")
    return {"result": "passed", "package_count": len(packages), "packages": packages}


def _probe_installed(installed_root: Path, expected_version: str) -> dict[str, object]:
    # The runner invokes this process with -I; only the explicitly installed target is
    # admitted.  The checkout itself is never placed on sys.path.
    sys.path.insert(0, str(installed_root.resolve()))
    import strathmark  # noqa: PLC0415
    from strathmark.v3.consumer_contract import (  # noqa: PLC0415
        EXPECTED_V3_CONSUMER_PATHS,
        load_v3_consumer_contract,
        v3_consumer_contract_digest,
    )

    if strathmark.__version__ != expected_version or version("strathmark") != expected_version:
        raise RuntimeError("installed STRATHMARK version differs")
    contract = load_v3_consumer_contract()
    if set(contract["paths"]) != EXPECTED_V3_CONSUMER_PATHS:
        raise RuntimeError("installed V3 consumer contract differs")
    if len(v3_consumer_contract_digest()) != 64:
        raise RuntimeError("installed V3 consumer contract digest differs")
    required = (
        "v3_consumer.openapi.json",
        "v3_consumer.openapi.sha256",
        "windows_capacity_manifest.json",
        "v3-release.lock",
    )
    contract_root = files("strathmark.v3.contracts")
    if any(not contract_root.joinpath(name).is_file() for name in required):
        raise RuntimeError("installed V3 release contract set is incomplete")
    return {
        "result": "passed",
        "distribution": "strathmark",
        "version": expected_version,
        "consumer_path_count": len(contract["paths"]),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    dependencies = subparsers.add_parser("dependencies")
    dependencies.add_argument("--lock", type=Path, required=True)
    installed = subparsers.add_parser("installed-wheel")
    installed.add_argument("--installed-root", type=Path, required=True)
    installed.add_argument("--expected-version", required=True)
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "dependencies":
            report = _probe_dependencies(arguments.lock)
        else:
            report = _probe_installed(arguments.installed_root, arguments.expected_version)
    except Exception as exc:
        print(json.dumps({"result": "failed", "reason": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
