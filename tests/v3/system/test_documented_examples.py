from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import jsonschema
import pytest

ROOT = Path(__file__).resolve().parents[3]
CURRENT_GUIDES = (
    ROOT / "README.md",
    ROOT / "ONBOARDING.md",
    ROOT / "docs/ARCHITECTURE.md",
    ROOT / "docs/PREDICTION_ENGINE_V3.md",
    ROOT / "docs/DEPLOYMENT.md",
    ROOT / "docs/STRATHEX_CONSUMER_MIGRATION.md",
    ROOT / "docs/wiki/Home.md",
    ROOT / "docs/wiki/Architecture-Overview.md",
    ROOT / "docs/wiki/Deployment.md",
    ROOT / "docs/wiki/REST-API.md",
    ROOT / "docs/wiki/STRATHEX-Consumer.md",
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _prose(path: Path) -> str:
    return " ".join(_read(path).split()).casefold()


def test_public_guides_share_one_explicit_release_and_authority_status() -> None:
    required = (
        "implemented",
        "release candidate",
        "rehearsal",
        "source-bound",
        "V2 remains the trusted production authority until an explicit cutover",
        "no production authority has changed",
    )
    for path in CURRENT_GUIDES:
        text = _prose(path)
        for statement in required:
            assert statement.casefold() in text, f"{path}: missing {statement!r}"


def test_v3_contract_table_matches_the_frozen_openapi_document() -> None:
    contract_path = ROOT / "strathmark/v3/contracts/v3_consumer.openapi.json"
    contract = json.loads(_read(contract_path))
    expected_digest = _read(contract_path.with_suffix(".sha256")).strip()
    assert hashlib.sha256(contract_path.read_bytes()).hexdigest() == expected_digest

    guide = _read(ROOT / "docs/PREDICTION_ENGINE_V3.md")
    documented = set(re.findall(r"^\| `(GET|POST)` \| `(/v3/[^`]+)` \|", guide, re.MULTILINE))
    actual = {
        (method.upper(), path)
        for path, path_item in contract["paths"].items()
        for method in path_item
        if method in {"get", "post", "put", "patch", "delete"}
    }
    assert documented == actual


@pytest.mark.filterwarnings("ignore:jsonschema.RefResolver is deprecated:DeprecationWarning")
def test_every_frozen_openapi_example_validates_against_its_schema() -> None:
    contract = json.loads(_read(ROOT / "strathmark/v3/contracts/v3_consumer.openapi.json"))
    resolver = jsonschema.RefResolver.from_schema(contract)
    validated = 0
    for path_item in contract["paths"].values():
        for method, operation in path_item.items():
            if method not in {"get", "post", "put", "patch", "delete"}:
                continue
            request = (
                operation.get("requestBody", {}).get("content", {}).get("application/json", {})
            )
            if "example" in request:
                jsonschema.validate(request["example"], request["schema"], resolver=resolver)
                validated += 1
            for response in operation["responses"].values():
                content = response.get("content", {}).get("application/json", {})
                if "example" in content:
                    jsonschema.validate(content["example"], content["schema"], resolver=resolver)
                    validated += 1
    assert validated >= 20


def test_readme_python_examples_execute_against_the_current_tree() -> None:
    examples = re.findall(r"```python\n(.*?)```", _read(ROOT / "README.md"), re.DOTALL)
    assert examples
    for index, example in enumerate(examples):
        namespace = {"__name__": f"strathmark_documented_example_{index}"}
        exec(compile(example, f"README.md:python-block-{index + 1}", "exec"), namespace)


def test_deployment_documents_every_runtime_configuration_key() -> None:
    deployment = _read(ROOT / "docs/DEPLOYMENT.md")
    documented = set(re.findall(r"^\| `(STRATHMARK_V3_[A-Z_]+)` \|", deployment, re.MULTILINE))
    assert documented == {
        "STRATHMARK_V3_ARCHIVE_ROOT",
        "STRATHMARK_V3_BACKUP_ROOT",
        "STRATHMARK_V3_BLOB_ROOT",
        "STRATHMARK_V3_BUNDLE_ROOT",
        "STRATHMARK_V3_CANONICAL_MAX_BYTES",
        "STRATHMARK_V3_CANONICAL_MAX_DEPTH",
        "STRATHMARK_V3_DB_PATH",
        "STRATHMARK_V3_INTEGRITY_KEY_ROOT",
        "STRATHMARK_V3_RECOVERY_ROOT",
        "STRATHMARK_V3_TEMP_PATH",
    }


def test_current_guides_do_not_restate_retired_v2_constraints_as_v3_rules() -> None:
    stale_unqualified_claims = (
        "STRATHMARK 2.0.0 is an offline-capable",
        "LLMs cannot generate numeric predictions",
        "numeric LLM: prohibited",
        "five legacy keys remain",
        "exclusive UTC cutoff and one immutable model bundle per request",
        "STRATHMARK implements tournament-manager RBAC",
        "copy the previous mark into the final",
    )
    for path in CURRENT_GUIDES:
        text = _read(path)
        for claim in stale_unqualified_claims:
            assert claim.casefold() not in text.casefold(), f"{path}: stale claim {claim!r}"


def test_handicap_source_of_truth_is_timeless_domain_material() -> None:
    foundation = _read(ROOT / "docs/wiki/Handicap-Mark-Math.md")
    assert "Mandatory domain reading" in foundation
    assert "smaller mark starts earlier" in foundation
    assert (
        "Adding or subtracting the same constant from every mark preserves the race" in foundation
    )
    for roadmap_term in (
        "STRATHMARK's current mark calculation",
        "posterior_crn_v2",
        "rounded_gap_fallback",
        "LLM council",
        "release attestation",
    ):
        assert roadmap_term not in foundation


def test_current_documentation_local_links_resolve() -> None:
    checked = 0
    candidates = set(ROOT.glob("*.md"))
    candidates.update((ROOT / "docs").rglob("*.md"))
    candidates.add(ROOT / "strathmark/migrations/README.md")
    for path in sorted(candidates):
        text = _read(path)
        for target in re.findall(r"\[[^\]]+\]\(([^)]+)\)", text):
            clean = target.split("#", 1)[0].strip()
            if not clean or "://" in clean or clean.startswith("mailto:"):
                continue
            resolved = (path.parent / clean).resolve()
            if not resolved.exists() and resolved.suffix == "":
                resolved = resolved.with_suffix(".md")
            assert resolved.exists(), f"{path}: broken link {target!r}"
            checked += 1
    assert checked >= 40
