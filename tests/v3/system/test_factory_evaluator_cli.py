from __future__ import annotations

from pathlib import Path


def test_packaged_evaluator_cli_opens_existing_cng_key_and_runs_one_request(
    tmp_path: Path, monkeypatch
) -> None:
    from strathmark.v3.factory import evaluator_cli

    opened: list[tuple[str, str]] = []
    calls: list[tuple[Path, Path, Path, object]] = []
    signer = object()

    class FakeCNGSigner:
        @staticmethod
        def open(key_name: str, *, provider_name: str):
            opened.append((key_name, provider_name))
            return signer

    def run(request, response, *, registry_path, signer):
        calls.append((request, response, registry_path, signer))

    monkeypatch.setattr(evaluator_cli, "P256WindowsCNGSigner", FakeCNGSigner)
    monkeypatch.setattr(evaluator_cli, "run_evaluator_request", run)
    request = tmp_path / "request.json"
    response = tmp_path / "response.json"
    registry = tmp_path / "registry"

    assert (
        evaluator_cli.main(
            [
                str(request),
                str(response),
                "--registry",
                str(registry),
                "--cng-key-name",
                "strathmark-v3-evaluator",
            ]
        )
        == 0
    )
    assert opened == [("strathmark-v3-evaluator", "Microsoft Software Key Storage Provider")]
    assert calls == [(request, response, registry, signer)]
