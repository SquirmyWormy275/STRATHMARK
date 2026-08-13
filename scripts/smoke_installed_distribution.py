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
        subprocess.run([sys.executable, "-m", "venv", str(environment)], check=True)
        python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        subprocess.run(
            [str(python), "-m", "pip", "install", f"{candidates[0]}[api]"],
            check=True,
            cwd=root,
        )
        subprocess.run([str(python), "-m", "pip", "check"], check=True, cwd=root)
        subprocess.run(
            [
                str(python),
                "-c",
                (
                    "from datetime import date; "
                    "from fastapi.testclient import TestClient; "
                    "from strathmark.api import app; "
                    "from strathmark.predictor import FilePredictionProvider; "
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
