"""Check the complete runtime identity required by the frozen workbook receipt."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def verify_runtime(node: Path, node_modules: Path) -> dict[str, str]:
    receipt = json.loads(
        (ROOT / "benchmarks/v3/formula_engine_verification.json").read_text("utf-8")
    )
    package = node_modules / "@oai/artifact-tool/package.json"
    if not node.is_file() or not package.is_file():
        raise ValueError("designated Formula Node and artifact-tool installation are required")
    observed = {
        "node_version": subprocess.check_output(
            [str(node), "--version"], text=True, timeout=10
        ).strip(),
        "artifact_tool_version": json.loads(package.read_text("utf-8"))["version"],
    }
    for key, actual in observed.items():
        if actual != receipt[key]:
            raise ValueError(
                f"Formula runtime mismatch: {key} requires {receipt[key]}, observed {actual}"
            )
    return observed


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--node", type=Path, default=os.environ.get("STRATHMARK_ARTIFACT_NODE"))
    parser.add_argument(
        "--node-modules", type=Path, default=os.environ.get("STRATHMARK_ARTIFACT_NODE_MODULES")
    )
    args = parser.parse_args()
    if args.node is None or args.node_modules is None:
        parser.error(
            "provide --node and --node-modules, or their STRATHMARK_ARTIFACT environment variables"
        )
    try:
        result = verify_runtime(args.node, args.node_modules)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        print(json.dumps({"result": "failed", "authority_changed": False, "reason": str(error)}))
        raise SystemExit(2) from error
    print(json.dumps({"result": "passed", "authority_changed": False, **result}))


if __name__ == "__main__":
    main()
