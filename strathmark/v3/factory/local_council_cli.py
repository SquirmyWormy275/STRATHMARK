"""Pinned, local-only three-family council diagnostics without promotion authority.

This Linux development profile uses Qwen, Ministral and Gemma locally. It does
not impersonate the production contract's two-local/one-cloud promoted council.
Raw envelopes, validator decisions and exact model/runtime identities are retained.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.client
import ipaddress
import json
import math
import secrets
import time
from pathlib import Path
from urllib.parse import urlsplit

from strathmark.v3.assessors.llm_council import (
    HMACTokenKey,
    LLMMemberSpec,
    ProviderKind,
    build_provider_packet,
    render_member_prompt,
)
from strathmark.v3.assessors.output_validation import (
    LLM_OUTPUT_SCHEMA_VERSION,
    REQUIRED_QUANTILES,
    LLMOutputError,
    validate_member_output,
)
from strathmark.v3.contracts.canonical import canonical_bytes, canonical_digest
from strathmark.v3.contracts.evidence import EvidencePacket, TargetContext
from strathmark.v3.contracts.identifiers import StableIdentifier
from strathmark.v3.factory.workbook_history import load_workbook_history

OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "schema_version",
        "state",
        "quantiles",
        "evidence_refs",
        "warnings",
        "fact_codes",
        "abstention_reason",
    ],
    "properties": {
        "schema_version": {"const": LLM_OUTPUT_SCHEMA_VERSION},
        "state": {"enum": ["committed", "abstained"]},
        "quantiles": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["probability", "time_ms"],
                "properties": {
                    "probability": {"enum": list(REQUIRED_QUANTILES)},
                    "time_ms": {"type": "integer", "minimum": 1, "maximum": 600000},
                },
            },
        },
        "evidence_refs": {"type": "array", "items": {"type": "string"}},
        "warnings": {
            "type": "array",
            "items": {
                "enum": [
                    "conflicting_evidence",
                    "insufficient_support",
                    "missing_context",
                    "rapid_change",
                    "sparse_evidence",
                ]
            },
        },
        "fact_codes": {"type": "array", "items": {"const": "observed_raw_time"}},
        "abstention_reason": {
            "type": ["string", "null"],
            "enum": [
                None,
                "conflicting_numeric_evidence",
                "insufficient_numeric_evidence",
                "unsupported_context",
            ],
        },
    },
}


class LocalProviderError(ValueError):
    def __init__(self, status, raw, *, truncated=False):
        super().__init__(
            f"provider_http_{status}" if not truncated else "provider_response_too_large"
        )
        self.raw = raw
        self.code = str(self)
        self.truncated = truncated


def member_response_schema(references, *, fact_codes=("observed_raw_time",)):
    """Constrain transport shape without manufacturing a committed forecast."""
    import copy

    abstained = copy.deepcopy(OUTPUT_SCHEMA)
    for field in ("quantiles", "evidence_refs", "fact_codes"):
        abstained["properties"][field] = {"const": []}
    abstained["properties"]["state"] = {"const": "abstained"}
    abstained["properties"]["abstention_reason"] = {
        "enum": [
            "conflicting_numeric_evidence",
            "insufficient_numeric_evidence",
            "unsupported_context",
        ]
    }
    if not references:
        return abstained
    committed = copy.deepcopy(OUTPUT_SCHEMA)
    committed["properties"]["state"] = {"const": "committed"}
    committed["properties"]["evidence_refs"] = {"const": list(references)}
    committed["properties"]["fact_codes"] = {"const": sorted(fact_codes)}
    committed["properties"]["abstention_reason"] = {"const": None}
    committed["properties"]["quantiles"] = {
        "type": "array",
        "minItems": 7,
        "maxItems": 7,
        "prefixItems": [
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["probability", "time_ms"],
                "properties": {
                    "probability": {"const": p},
                    "time_ms": {"type": "integer", "minimum": 1, "maximum": 600000},
                },
            }
            for p in REQUIRED_QUANTILES
        ],
    }
    return {"type": "object", "anyOf": [committed, abstained]}


def select_local_history(observations, target, *, limit=12):
    """Preserve scarce relevant history instead of taking only the latest events.

    The caller supplies exclusively prior observations. No result times, issued
    marks or evaluation outcomes influence this context-only bounded selection.
    """
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 12:
        raise ValueError("local history limit must be between one and twelve")
    ranked = sorted(
        observations,
        key=lambda o: (
            o.context.event_code == target.event_code,
            o.context.material_code == target.material_code,
            -abs(math.log(o.context.size_mm / target.size_mm)),
            o.occurred_at_utc,
            o.observation_sequence,
        ),
        reverse=True,
    )[:limit]
    return tuple(sorted(ranked, key=lambda o: (o.occurred_at_utc, o.observation_sequence)))


def add_declared_conversions(projected, evidence):
    """Supply transparent raw-fact unit/context conversions, never assessor output."""
    from decimal import ROUND_HALF_EVEN, Decimal

    from strathmark.v3.assessors.formula import FormulaManifest, _conversion

    policy = FormulaManifest.load(Path(__file__).parents[1] / "contracts/formula_manifest.json")
    if (
        policy.version != "formula:v2-bootstrap"
        or policy.digest != "2c58a9527c77a33e0b813fe938db44c6298ac0ea8b543a199b720d97baaf1354"
    ):
        raise ValueError("local declared conversion policy differs from the frozen bootstrap")
    observations = {o.observation_sequence: o for o in evidence.observations}
    supported = 0
    for row in projected["observations"]:
        original = observations[row["observation_sequence"]]
        conversion = _conversion(original.context, evidence.target_context, policy)
        row["conversion_status"] = conversion[3]
        row["target_equivalent_time_ms"] = None
        row["conversion_variance"] = str(conversion[2])
        if conversion[0] > 0:
            converted = Decimal(row["raw_time_ms"]) * conversion[4] * conversion[5] * conversion[6]
            if 1 <= converted <= 600000:
                row["target_equivalent_time_ms"] = int(
                    converted.quantize(Decimal(1), rounding=ROUND_HALF_EVEN)
                )
                supported += 1
    projected["declared_conversion_policy"] = {
        "version": "local-raw-context-conversion-v1",
        "manifest_digest": policy.digest,
        "meaning": "declared event-scale, diameter and observed-density conversion of earlier raw cuts; not a new observed cut or assessor forecast",
        "supported_observation_count": supported,
    }
    return (
        ("declared_time_conversion", "observed_raw_time") if supported else ("observed_raw_time",)
    )


class LocalCouncilClient:
    def __init__(self, profile: dict):
        self.profile = profile
        parsed = urlsplit(profile["origin"])
        if (
            parsed.scheme != "http"
            or parsed.username
            or parsed.password
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or not ipaddress.ip_address(parsed.hostname).is_loopback
        ):
            raise ValueError("local council requires a direct loopback HTTP origin")
        self.host, self.port = parsed.hostname, parsed.port or 80
        self.binary = Path(profile["runtime_binary"]).resolve(strict=True)
        self.members = tuple(
            LLMMemberSpec.candidate(
                provider_kind=ProviderKind.LOCAL,
                sampling_parameters={"seed": 1729, "temperature": "0", "top_p": "1"},
                runtime_version=profile["runtime_version"],
                runtime_digest=profile["runtime_sha256"],
                **item,
            )
            for item in profile["members"]
        )
        if (
            len(self.members) != 3
            or len({item.family for item in self.members}) != 3
            or len({item.member_id for item in self.members}) != 3
            or len({item.model_id for item in self.members}) != 3
            or len({item.model_digest for item in self.members}) != 3
        ):
            raise ValueError(
                "local diagnostic council requires three distinct families, members and models"
            )
        self.token_key = HMACTokenKey("local_council", secrets.token_bytes(32))
        self.check_pins()

    def request(self, method, path, payload=None, timeout=180):
        connection = http.client.HTTPConnection(self.host, self.port, timeout=timeout)
        try:
            connection.request(
                method,
                path,
                body=None if payload is None else canonical_bytes(payload),
                headers={"Content-Type": "application/json"},
            )
            response = connection.getresponse()
            raw = response.read(1_048_577)
            if len(raw) > 1_048_576 or response.status != 200:
                raise LocalProviderError(
                    response.status, raw[:1_048_576], truncated=len(raw) > 1_048_576
                )
            return raw
        finally:
            connection.close()

    def check_pins(self):
        if hashlib.sha256(self.binary.read_bytes()).hexdigest() != self.profile["runtime_sha256"]:
            raise ValueError("local runtime binary checksum changed")
        version = json.loads(self.request("GET", "/api/version", timeout=5))["version"]
        if "ollama:" + version != self.profile["runtime_version"]:
            raise ValueError("local server runtime version changed")
        models = {
            item["name"]: item
            for item in json.loads(self.request("GET", "/api/tags", timeout=5))["models"]
        }
        for member in self.members:
            found = models.get(member.model_id)
            if (
                found is None
                or found["digest"] != member.model_digest
                or found["details"]["quantization_level"] != member.quantization
            ):
                raise ValueError("local model or quantization differs from the pinned profile")
        return {
            "runtime_version": self.profile["runtime_version"],
            "runtime_sha256": self.profile["runtime_sha256"],
            "models": [
                {"model_id": m.model_id, "digest": m.model_digest, "family": m.family}
                for m in self.members
            ],
            "numeric_promotion": False,
        }

    def evaluate_member(self, evidence, member, *, keep_alive=0):
        self.check_pins()
        packet = build_provider_packet(evidence, member, self.token_key, scope="local_candidate")
        # Workbook imports have no authenticated issued-mark or legal-field
        # metadata. Their transport defaults are not evidence for a model.
        projected = packet.to_dict()
        unavailable = ("issued_mark", "completion_clock_ms", "placing", "gap_ms")
        for observation in projected["observations"]:
            for name in unavailable:
                observation.pop(name, None)
        projected["schema_version"] = "strathmark-v3-local-legacy-provider-packet-v2"
        projected["unavailable_fields"] = list(unavailable)
        fact_codes = add_declared_conversions(projected, evidence)
        projected["numeric_digest"] = canonical_digest(
            {
                "schema_version": projected["schema_version"],
                "unavailable_fields": projected["unavailable_fields"],
                "target_context": projected["target_context"],
                "declared_conversion_policy": projected["declared_conversion_policy"],
                "observations": [
                    {key: value for key, value in item.items() if key != "evidence_ref"}
                    for item in projected["observations"]
                ],
            }
        )
        policy = render_member_prompt(packet).decode().split("UNTRUSTED_JSON_DATA\n")[0]
        prompt = policy + "UNTRUSTED_JSON_DATA\n" + canonical_bytes(projected).decode()
        schema = member_response_schema(
            [item.evidence_ref for item in packet.observations], fact_codes=fact_codes
        )
        prompt += "\nResponse schema: " + json.dumps(schema)
        prompt += "\nQuantiles must have probabilities in exactly this order: " + ",".join(
            REQUIRED_QUANTILES
        )
        prompt += "\nTimes are RAW cutting milliseconds. Issued marks, completion clocks, legal placings and gaps are unavailable. Output no narrative."
        prompt += "\nCOMMITTED: use all seven ordered quantiles, abstention_reason=null, and cite the supplied evidence_ref values in their original order."
        prompt += "\nABSTAINED: quantiles=[], evidence_refs=[], fact_codes=[]; use a declared abstention_reason. Empty history requires abstention. Do not cite references when abstaining. Sort warnings and fact_codes alphabetically without duplicates."
        prompt += "\nUse matching event, material and diameter observations where available. Different contexts are not interchangeable. If numeric support is insufficient, keep the abstention branch. Seven quantile times must be nondecreasing."
        prompt += "\nEach target_equivalent_time_ms is an explicitly declared conversion of an earlier RAW cut into the target context, not another assessor's prediction. Null means unsupported. Use supported values only with uncertainty appropriate to sparse history and declared conversion variance. Conversions are model policy, not a universal sport rule."
        attempts = []
        validated = None
        error = None
        started = time.monotonic()
        for attempt in range(2):
            message = (
                prompt
                if attempt == 0
                else prompt
                + "\nPrevious response was invalid: "
                + error
                + ". Return corrected exact JSON."
            )
            payload = {
                "model": member.model_id,
                "prompt": message,
                "format": schema,
                "stream": False,
                "think": False,
                "keep_alive": keep_alive,
                "options": {
                    "seed": 1729,
                    "temperature": 0,
                    "top_p": 1,
                    "num_ctx": 8192,
                    "num_predict": 2048,
                },
            }
            record = {"request_digest": canonical_digest(payload)}
            attempts.append(record)
            try:
                raw = self.request(
                    "POST",
                    "/api/generate",
                    payload,
                )
                record["raw_envelope_sha256"] = hashlib.sha256(raw).hexdigest()
                try:
                    record["raw_envelope"] = raw.decode()
                except UnicodeDecodeError:
                    record["raw_envelope_base64"] = base64.b64encode(raw).decode("ascii")
                envelope = json.loads(raw)
                if not isinstance(envelope, dict):
                    raise ValueError("provider envelope must be an object")
                if envelope.get("model") != member.model_id or envelope.get("done") is not True:
                    raise ValueError("provider returned another model or incomplete output")
                if not isinstance(envelope.get("response"), str):
                    raise ValueError("provider response must be a string")
                body = envelope["response"].encode()
                record["response_sha256"] = hashlib.sha256(body).hexdigest()
                response = json.loads(body)
                if (
                    isinstance(response, dict)
                    and response.get("state") == "committed"
                    and response.get("fact_codes") != list(fact_codes)
                ):
                    raise ValueError("committed fact codes differ from the generated schema")
                validated = validate_member_output(
                    body,
                    expected_evidence_refs=[item.evidence_ref for item in packet.observations],
                    allowed_fact_codes=fact_codes,
                )
                record["validator_code"] = validated.validator_code
                error = None
                break
            except (LLMOutputError, ValueError, TypeError, KeyError, OSError) as exc:
                if isinstance(exc, LocalProviderError):
                    record["raw_envelope_sha256"] = hashlib.sha256(exc.raw).hexdigest()
                    record["raw_envelope_base64"] = base64.b64encode(exc.raw).decode("ascii")
                    record["raw_envelope_truncated"] = exc.truncated
                error = (
                    exc.code
                    if isinstance(exc, (LLMOutputError, LocalProviderError))
                    else type(exc).__name__
                )
                record["validator_code"] = error
        self.check_pins()
        return {
            "member_id": member.member_id,
            "family": member.family,
            "model_id": member.model_id,
            "model_digest": member.model_digest,
            "evidence_digest": evidence.content_digest,
            "prompt_digest": hashlib.sha256(prompt.encode()).hexdigest(),
            "provider_packet": projected,
            "latency_ms": round((time.monotonic() - started) * 1000),
            "attempts": attempts,
            "error": error,
            "distribution": None
            if validated is None or validated.distribution is None
            else validated.distribution.to_dict(),
        }

    def evaluate(self, evidence):
        outcomes = [self.evaluate_member(evidence, member) for member in self.members]
        valid = [item for item in outcomes if item["distribution"] is not None]
        # Equal-weight candidate CDF mixture. No operational promotion is granted.
        from decimal import Decimal

        from strathmark.v3.assessors.llm_council import _mixture_distribution
        from strathmark.v3.contracts.forecasts import PositiveTimeDistribution

        distribution = (
            _mixture_distribution(
                tuple(PositiveTimeDistribution.from_dict(item["distribution"]) for item in valid),
                tuple(Decimal(1) / len(valid) for _ in valid),
            )
            if len(valid) >= 2
            else None
        )
        return {
            "schema_version": "strathmark-local-council-diagnostic-v1",
            "profile_digest": canonical_digest(self.profile),
            "evidence_digest": evidence.content_digest,
            "authority_class": "local_development_candidate",
            "numeric_promotion": False,
            "approval_required": True,
            "availability": "normal"
            if len(valid) == 3
            else "degraded"
            if len(valid) == 2
            else "unavailable",
            "members": outcomes,
            "distribution": None if distribution is None else distribution.to_dict(),
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--workbook", type=Path)
    parser.add_argument("--competitor-id")
    parser.add_argument("--event", choices=("underhand", "standing_block"))
    parser.add_argument("--species")
    parser.add_argument("--size-mm", type=int)
    parser.add_argument("--cutoff-at-utc")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    client = LocalCouncilClient(json.loads(args.profile.read_text()))
    if args.status:
        print(json.dumps(client.check_pins(), indent=2))
        return
    if not all(
        (
            args.workbook,
            args.competitor_id,
            args.event,
            args.species,
            args.size_mm,
            args.cutoff_at_utc,
            args.output,
        )
    ):
        parser.error(
            "forecast requires workbook, competitor, event, species, size, cutoff and new output"
        )
    if args.output.exists():
        raise FileExistsError("council receipt exists; refusing to overwrite it")
    history = load_workbook_history(args.workbook, cutoff_at_utc=args.cutoff_at_utc)
    identity = dict(history.competitor_ids)[args.competitor_id]
    context = TargetContext(
        args.event, args.size_mm, args.species, "strathex:v1", "strathex:v1", ()
    )
    observations = select_local_history(
        (item for item in history.observations if str(item.competitor_id) == identity), context
    )
    packet = EvidencePacket.create(
        competitor_id=StableIdentifier(identity),
        target_context=context,
        observations=observations,
        taxonomy_version=context.taxonomy_version,
        conversion_version=context.conversion_version,
        historical_cutoff_key="history:" + history.source_sha256,
        tournament_epoch_id=StableIdentifier("epoch:local-council"),
        tournament_event_sequence=max(
            (item.observation_sequence for item in observations), default=0
        ),
    )
    result = client.evaluate(packet)
    args.output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with args.output.open("xb") as stream:
        stream.write(canonical_bytes(result, max_bytes=20_000_000))
    args.output.chmod(0o600)
    print(
        json.dumps(
            {key: value for key, value in result.items() if key not in {"members", "distribution"}},
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
