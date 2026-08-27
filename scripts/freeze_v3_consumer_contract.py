"""Regenerate the reviewed V3 OpenAPI document and SHA-256 pin."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from strathmark.v3.consumer_contract import build_v3_consumer_contract

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "strathmark" / "v3" / "contracts" / "v3_consumer.openapi.json"
CHECKSUM = ROOT / "strathmark" / "v3" / "contracts" / "v3_consumer.openapi.sha256"


def main() -> int:
    document = build_v3_consumer_contract()
    raw = (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    CONTRACT.parent.mkdir(parents=True, exist_ok=True)
    CONTRACT.write_bytes(raw)
    CHECKSUM.write_bytes((hashlib.sha256(raw).hexdigest() + "\n").encode("ascii"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
