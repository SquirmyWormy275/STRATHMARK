"""Offline smoke for an installed STRATHMARK wheel.

Usage: ``python tests/installed_wheel_shadow_smoke.py`` from an environment where
the built wheel (including the ``api`` extra dependencies) is installed.
"""

from __future__ import annotations

from strathmark import (
    load_shadow_consumer_contract,
    shadow_consumer_contract_digest,
)
from strathmark.api import app

EXPECTED_PATHS = {
    "/health",
    "/v1/shadow/calculate",
    "/v1/shadow/drift",
    "/v1/shadow/mirror/replay",
    "/v1/shadow/outcomes/apply",
    "/v1/shadow/receipts/lookup",
    "/v1/shadow/status",
}


def main() -> None:
    document = load_shadow_consumer_contract()
    assert set(document["paths"]) == EXPECTED_PATHS
    installed_paths = {route.path for route in app.routes}
    assert EXPECTED_PATHS <= installed_paths
    print(f"shadow-consumer-contract {shadow_consumer_contract_digest()} routes=7")


if __name__ == "__main__":
    main()
