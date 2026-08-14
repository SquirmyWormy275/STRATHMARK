"""Smoke-test an installed wheel or sdist outside the source checkout."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kind", choices=("wheel", "sdist"), required=True)
    parser.add_argument("--dist-dir", type=Path, default=Path("dist"))
    parser.add_argument(
        "--offline",
        action="store_true",
        help="reuse installed dependencies and disable package-index access",
    )
    args = parser.parse_args(argv)
    pattern = "*.whl" if args.kind == "wheel" else "*.tar.gz"
    candidates = sorted(args.dist_dir.resolve().glob(pattern))
    if len(candidates) != 1:
        raise SystemExit(
            f"expected exactly one {args.kind} in {args.dist_dir}, found {len(candidates)}"
        )

    with tempfile.TemporaryDirectory(prefix=f"strathmark-{args.kind}-") as directory:
        root = Path(directory)
        environment = root / "venv"
        venv_command = [sys.executable, "-m", "venv"]
        if args.offline:
            venv_command.append("--system-site-packages")
        subprocess.run([*venv_command, str(environment)], check=True)
        python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        install_command = [str(python), "-m", "pip", "install"]
        if args.offline:
            install_command.extend(
                ["--no-index", "--no-deps", "--no-build-isolation", "--no-cache-dir"]
            )
        subprocess.run(
            [*install_command, f"{candidates[0]}[api]"],
            check=True,
            cwd=root,
        )
        if not args.offline:
            subprocess.run([str(python), "-m", "pip", "check"], check=True, cwd=root)
        subprocess.run(
            [
                str(python),
                "-c",
                (
                    "from datetime import date; "
                    "from fastapi.testclient import TestClient; "
                    "from strathmark.api import app; "
                    "from strathmark.consumer_contract import ("
                    "EXPECTED_SHADOW_CONSUMER_PATHS, load_shadow_consumer_contract, "
                    "shadow_consumer_contract_digest); "
                    "from strathmark.predictor import FilePredictionProvider; "
                    "contract=load_shadow_consumer_contract(); "
                    "digest=shadow_consumer_contract_digest(document=contract); "
                    "assert set(contract['paths']) == EXPECTED_SHADOW_CONSUMER_PATHS; "
                    "assert len(digest) == 64; "
                    "assert TestClient(app).get('/docs').status_code == 200; "
                    "bundle=FilePredictionProvider().snapshot(date(2026,8,11)); "
                    "assert bundle.core is not None and bundle.source == 'package'; "
                    "print('installed distribution smoke OK')"
                ),
            ],
            check=True,
            cwd=root,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
