"""Start or stop the app-owned, pinned Linux loopback diagnostic provider."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import socket
import subprocess
import time
from pathlib import Path

from strathmark.v3.factory.local_council_cli import LocalCouncilClient


def _process_identity(pid):
    root = Path("/proc") / str(pid)
    # Fields after the final ')' begin with field 3, so offset 19 is starttime.
    ticks = (root / "stat").read_text().rsplit(")", 1)[1].split()[19]
    return {"pid": pid, "start_ticks": ticks, "binary": str((root / "exe").resolve(strict=True))}


def _owned_process(state, binary):
    recorded = json.loads(state.read_text())
    observed = _process_identity(recorded["pid"])
    if observed != recorded or observed["binary"] != str(binary):
        raise ValueError("provider process identity changed; refusing to signal another process")
    return observed


def manage(profile_path: Path, operation: str):
    if os.name != "posix" or not Path("/proc").is_dir():
        raise ValueError("app-owned provider management requires Linux")
    profile = json.loads(profile_path.read_text())
    binary = Path(profile["runtime_binary"]).resolve(strict=True)
    if hashlib.sha256(binary.read_bytes()).hexdigest() != profile["runtime_sha256"]:
        raise ValueError("provider binary differs from its pinned identity")
    directory = profile_path.parent
    state = directory / "provider-process.json"
    if operation == "status":
        return LocalCouncilClient(profile).check_pins()
    if operation == "stop":
        if not state.exists():
            raise ValueError("no app-owned provider process is recorded")
        owned = _owned_process(state, binary)
        os.kill(owned["pid"], signal.SIGTERM)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and Path(f"/proc/{owned['pid']}").exists():
            time.sleep(0.1)
        if Path(f"/proc/{owned['pid']}").exists():
            raise ValueError("provider has not stopped; its process record was preserved")
        state.unlink()
        return {"state": "stopped", "numeric_authority_changed": False}
    if state.exists():
        try:
            _owned_process(state, binary)
        except FileNotFoundError:
            state.unlink()
        else:
            return LocalCouncilClient(profile).check_pins()
    # Parse and enforce the origin using the same no-proxy transport boundary.
    import ipaddress
    from urllib.parse import urlsplit

    origin = urlsplit(profile["origin"])
    if (
        origin.scheme != "http"
        or origin.path not in {"", "/"}
        or origin.username
        or origin.password
        or origin.query
        or origin.fragment
        or not ipaddress.ip_address(origin.hostname).is_loopback
    ):
        raise ValueError("provider management requires an explicit loopback origin")
    probe = socket.socket(socket.AF_INET6 if ":" in origin.hostname else socket.AF_INET)
    try:
        probe.bind((origin.hostname, origin.port or 80))
    except OSError as exc:
        raise ValueError("the provider port is occupied by an unowned process") from exc
    finally:
        probe.close()
    environment = {key: value for key, value in os.environ.items() if not key.startswith("OLLAMA_")}
    environment.update(
        OLLAMA_HOST=origin.netloc,
        OLLAMA_MODELS=str(directory / "models"),
        OLLAMA_NO_CLOUD="1",
        OLLAMA_MAX_LOADED_MODELS="1",
        OLLAMA_NUM_PARALLEL="1",
    )
    with (directory / "provider.log").open("ab") as log:
        process = subprocess.Popen(
            [str(binary), "serve"],
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
    (directory / "provider.log").chmod(0o600)
    try:
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise ValueError("provider exited before its pinned models were ready")
            try:
                result = LocalCouncilClient(profile).check_pins()
            except (OSError, ValueError):
                time.sleep(0.2)
                continue
            # The executable may still be entering exec() when first spawned.
            observed = _process_identity(process.pid)
            if observed["binary"] != str(binary):
                raise ValueError("provider executable identity changed during startup")
            with state.open("x") as stream:
                json.dump(observed, stream)
            state.chmod(0o600)
            return result
        raise ValueError("provider did not become ready within 45 seconds")
    except Exception:
        process.terminate()
        process.wait(timeout=15)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("operation", choices=("start", "status", "stop"))
    args = parser.parse_args()
    try:
        print(json.dumps(manage(args.profile.resolve(strict=True), args.operation), indent=2))
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        parser.exit(2, f"{exc}\n")


if __name__ == "__main__":
    main()
