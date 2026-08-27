"""Installed CLI for one isolated V3 evaluator request using an existing CNG key."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Sequence

from strathmark.v3.factory.evaluator_process import run_evaluator_request
from strathmark.v3.infrastructure.integrity import P256WindowsCNGSigner


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("request", type=Path)
    parser.add_argument("response", type=Path)
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--cng-key-name", required=True)
    parser.add_argument(
        "--cng-provider",
        default="Microsoft Software Key Storage Provider",
        choices=(
            "Microsoft Software Key Storage Provider",
            "Microsoft Platform Crypto Provider",
        ),
    )
    arguments = parser.parse_args(argv)
    signer = P256WindowsCNGSigner.open(
        arguments.cng_key_name,
        provider_name=arguments.cng_provider,
    )
    run_evaluator_request(
        arguments.request,
        arguments.response,
        registry_path=arguments.registry,
        signer=signer,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
