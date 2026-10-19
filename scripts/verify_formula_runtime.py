"""Authenticate the complete tool inventory required by the frozen workbook receipt."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

try:
    from scripts.verify_toolchain import _bytes
except ModuleNotFoundError:  # direct script execution
    from verify_toolchain import _bytes

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "strathmark-formula-toolchain-v1"


def formula_environment() -> dict[str, str]:
    """Do not permit NODE_OPTIONS, NODE_PATH or other ambient code injection."""
    return {
        key: value
        for key, value in os.environ.items()
        if key in {"SystemRoot", "WINDIR", "TEMP", "TMP", "HOME", "USERPROFILE", "LANG"}
    }


def _module_inventory(root: Path) -> dict[str, str]:
    if not root.is_dir():
        raise ValueError("designated Formula module directory is required")
    root = root.resolve(strict=True)
    inventory = {}
    for directory, directories, files in os.walk(root, followlinks=False):
        for name in sorted(directories + files):
            path = Path(directory) / name
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                try:
                    path.resolve(strict=True).relative_to(root)
                except (ValueError, OSError) as error:
                    raise ValueError(
                        "Formula module link escaped its approved inventory"
                    ) from error
                inventory[relative] = "symlink:" + os.readlink(path)
            elif path.is_file():
                inventory[relative] = hashlib.sha256(_bytes(path)).hexdigest()
            elif not path.is_dir():
                raise ValueError("Formula inventory contains a non-regular input")
    if not inventory:
        raise ValueError("Formula module inventory is empty")
    return inventory


def runtime_identity(
    node: Path,
    node_modules: Path,
    *,
    manifest: Path | None = None,
    expected_manifest_sha256: str | None = None,
) -> dict[str, str]:
    """Compare bytes with separately approved inputs before executing any tool."""
    manifest_value = manifest or os.environ.get("STRATHMARK_FORMULA_TOOLCHAIN_MANIFEST")
    expected = expected_manifest_sha256 or os.environ.get("STRATHMARK_FORMULA_TOOLCHAIN_SHA256", "")
    if manifest_value is None or re.fullmatch(r"[0-9a-f]{64}", expected) is None:
        raise ValueError(
            "Formula qualification requires an independently approved toolchain manifest and SHA-256"
        )
    raw = _bytes(Path(manifest_value), 32 * 1024 * 1024)
    if hashlib.sha256(raw).hexdigest() != expected:
        raise ValueError("Formula toolchain manifest differs from its approved digest")
    trusted = json.loads(raw)
    if (
        not isinstance(trusted, dict)
        or set(trusted) != {"schema_version", "node_sha256", "module_files"}
        or trusted["schema_version"] != SCHEMA
    ):
        raise ValueError("Formula toolchain manifest schema differs")
    node_digest = hashlib.sha256(_bytes(node.resolve(strict=True))).hexdigest()
    if node_digest != trusted["node_sha256"]:
        raise ValueError("Formula Node bytes differ from the approved manifest")
    inventory = _module_inventory(node_modules)
    if inventory != trusted["module_files"]:
        raise ValueError("Formula module bytes differ from the approved manifest")
    return {
        "toolchain_manifest_sha256": expected,
        "node_sha256": node_digest,
        "module_files_sha256": hashlib.sha256(
            json.dumps(inventory, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    }


def verify_runtime(
    node: Path,
    node_modules: Path,
    *,
    manifest: Path | None = None,
    expected_manifest_sha256: str | None = None,
) -> dict[str, str]:
    identity = runtime_identity(
        node, node_modules, manifest=manifest, expected_manifest_sha256=expected_manifest_sha256
    )
    receipt = json.loads(
        (ROOT / "benchmarks/v3/formula_engine_verification.json").read_text("utf-8")
    )
    package = node_modules / "@oai/artifact-tool/package.json"
    observed = {
        "node_version": subprocess.check_output(
            [str(node.resolve(strict=True)), "--version"],
            env=formula_environment(),
            text=True,
            timeout=10,
        ).strip(),
        "artifact_tool_version": json.loads(_bytes(package))["version"],
    }
    for key, actual in observed.items():
        if actual != receipt[key]:
            raise ValueError(
                f"Formula runtime mismatch: {key} requires {receipt[key]}, observed {actual}"
            )
    if (
        runtime_identity(
            node, node_modules, manifest=manifest, expected_manifest_sha256=expected_manifest_sha256
        )
        != identity
    ):
        raise ValueError("Formula tool identity changed during verification")
    return {**observed, **identity}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--node", type=Path, default=os.environ.get("STRATHMARK_ARTIFACT_NODE"))
    parser.add_argument(
        "--node-modules", type=Path, default=os.environ.get("STRATHMARK_ARTIFACT_NODE_MODULES")
    )
    parser.add_argument(
        "--manifest", type=Path, default=os.environ.get("STRATHMARK_FORMULA_TOOLCHAIN_MANIFEST")
    )
    parser.add_argument(
        "--manifest-sha256", default=os.environ.get("STRATHMARK_FORMULA_TOOLCHAIN_SHA256")
    )
    args = parser.parse_args()
    if args.node is None or args.node_modules is None:
        parser.error(
            "provide --node and --node-modules, or their STRATHMARK_ARTIFACT environment variables"
        )
    try:
        result = verify_runtime(
            args.node,
            args.node_modules,
            manifest=args.manifest,
            expected_manifest_sha256=args.manifest_sha256,
        )
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        print(json.dumps({"result": "failed", "authority_changed": False, "reason": str(error)}))
        raise SystemExit(2) from error
    print(json.dumps({"result": "passed", "authority_changed": False, **result}))


if __name__ == "__main__":
    main()
