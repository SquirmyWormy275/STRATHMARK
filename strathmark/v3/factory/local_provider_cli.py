"""Start or stop the app-owned, pinned Linux loopback diagnostic provider."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import socket
import stat
import subprocess
import time
from pathlib import Path

from strathmark.input_limits import read_bounded_file
from strathmark.private_storage import private_file, reject_links, verify_private_directory
from strathmark.v3.factory.local_council_cli import LocalCouncilClient


def _owned_input(path):
    path = reject_links(path)
    status = path.lstat()
    if (
        not stat.S_ISREG(status.st_mode)
        or status.st_uid not in (0, os.geteuid())
        or status.st_mode & 0o022
    ):
        raise PermissionError("provider configuration and executable must be owner-controlled")
    return path


def _process_identity(pid):
    root = Path("/proc") / str(pid)
    # Fields after the final ')' begin with field 3, so offset 19 is starttime.
    ticks = (root / "stat").read_text().rsplit(")", 1)[1].split()[19]
    return {"pid": pid, "start_ticks": ticks, "binary": str((root / "exe").resolve(strict=True))}


def _owned_process(state, binary):
    recorded = json.loads(read_bounded_file(_owned_input(state), maximum=16384))
    observed = _process_identity(recorded["pid"])
    if observed != recorded or observed["binary"] != str(binary):
        raise ValueError("provider process identity changed; refusing to signal another process")
    return observed


def manage(profile_path: Path, operation: str):
    if os.name != "posix" or not Path("/proc").is_dir():
        raise ValueError("app-owned provider management requires Linux")
    profile_path = _owned_input(profile_path)
    directory = verify_private_directory(profile_path.parent)
    profile = json.loads(read_bounded_file(profile_path, maximum=16384))
    binary = _owned_input(Path(profile["runtime_binary"]).resolve(strict=True))
    if (
        hashlib.sha256(read_bounded_file(binary, maximum=512 * 1024 * 1024)).hexdigest()
        != profile["runtime_sha256"]
    ):
        raise ValueError("provider binary differs from its pinned identity")
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
    log_path = private_file(directory / "provider.log")
    descriptor = os.open(log_path, os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "ab") as log:
        process = subprocess.Popen(
            [str(binary), "serve"],
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
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
            descriptor = os.open(state, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(descriptor, "w") as stream:
                json.dump(observed, stream)
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
        print(json.dumps(manage(args.profile.expanduser().absolute(), args.operation), indent=2))
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        parser.exit(2, f"{exc}\n")


if __name__ == "__main__":
    main()
