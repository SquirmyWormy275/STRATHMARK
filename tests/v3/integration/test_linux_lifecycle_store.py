from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from strathmark.v3.linux_lifecycle_store import LinuxLifecycleError, LinuxLifecycleStore

NOW = "2026-10-02T21:00:00.000Z"


def commit(store, *, value=1, fail=False):
    def transition(state):
        state["roots"]["tournament:synthetic"] = {"value": value}
        if fail:
            raise RuntimeError("interrupted command")
        return {"accepted": value}

    return store.execute(
        command_id="command:synthetic-open",
        operation="open",
        scope_id="tournament:synthetic",
        request={"value": value},
        occurred_at=NOW,
        transition=transition,
    )


def test_restart_exact_retry_and_changed_request(store_path):
    store = LinuxLifecycleStore.initialize(store_path)
    identity = store.signer.identity
    assert commit(store) == {"accepted": 1}
    reopened = LinuxLifecycleStore(store_path)
    assert reopened.signer.identity == identity
    assert commit(reopened) == {"accepted": 1}
    with pytest.raises(LinuxLifecycleError, match="changed authority or request"):
        commit(reopened, value=2)
    assert reopened.state()["roots"]["tournament:synthetic"] == {"value": 1}


def test_failed_transition_leaves_no_authority_or_partial_state(store_path):
    store = LinuxLifecycleStore.initialize(store_path)
    with pytest.raises(RuntimeError, match="interrupted"):
        commit(store, fail=True)
    assert LinuxLifecycleStore(store_path).state() == {"roots": {}}
    assert commit(store) == {"accepted": 1}


def test_missing_key_cannot_be_replaced_for_existing_competition(store_path):
    store = LinuxLifecycleStore.initialize(store_path)
    commit(store)
    (store_path / "installation-key.pem").unlink()
    with pytest.raises(LinuxLifecycleError, match="incomplete installation"):
        LinuxLifecycleStore.initialize(store_path)


def test_signed_log_rejects_tampered_authority_and_rollback(store_path):
    store = LinuxLifecycleStore.initialize(store_path)
    commit(store)
    with sqlite3.connect(store.database) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("DELETE FROM commands")
        connection.execute("DROP TRIGGER commands_no_delete")
        connection.execute("DELETE FROM commands")
    with pytest.raises(LinuxLifecycleError, match="rollback"):
        LinuxLifecycleStore(store_path)


def test_independent_backup_restores_keys_chain_and_exact_retry(store_path, tmp_path):
    store = LinuxLifecycleStore.initialize(store_path)
    commit(store)
    backup = tmp_path / "independent-backup"
    report = store.backup(backup)
    assert report["verified"] is True
    restored = LinuxLifecycleStore(backup)
    assert restored.signer.identity == store.signer.identity
    assert restored.state() == store.state()
    assert commit(restored) == {"accepted": 1}


def test_process_interrupt_after_commit_recovers_response_without_running_transition_again(
    store_path, monkeypatch
):
    from strathmark.v3 import linux_lifecycle_store as module

    store = LinuxLifecycleStore.initialize(store_path)
    original = module._write_private

    def interrupted(path, raw, *, replace=False):
        if replace:
            raise OSError("simulated process interruption after commit")
        original(path, raw, replace=replace)

    monkeypatch.setattr(module, "_write_private", interrupted)
    with pytest.raises(OSError, match="after commit"):
        commit(store)
    monkeypatch.setattr(module, "_write_private", original)
    reopened = LinuxLifecycleStore(store_path)
    # This handler raises if the exact committed transition is accidentally run again.
    assert commit(reopened, fail=True) == {"accepted": 1}


def test_concurrent_writers_keep_one_complete_signed_state_and_monotonic_head(store_path):
    LinuxLifecycleStore.initialize(store_path)

    def append(index):
        store = LinuxLifecycleStore(store_path)

        def transition(state):
            state["roots"][f"tournament:synthetic-{index}"] = {"value": index}
            return {"accepted": index}

        return store.execute(
            command_id=f"command:synthetic-{index}",
            operation="open",
            scope_id=f"tournament:synthetic-{index}",
            request={"value": index},
            occurred_at=NOW,
            transition=transition,
        )

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(append, range(12)))
    assert len(results) == 12
    reopened = LinuxLifecycleStore(store_path)
    assert len(reopened.state()["roots"]) == 12
    import json

    assert json.loads((store_path / "head.json").read_text())["sequence"] == 12


@pytest.fixture
def store_path(tmp_path):
    return tmp_path / "synthetic-linux-installation"


def test_archive_readback_restores_and_detects_corruption(store_path, tmp_path):
    import tarfile

    store = LinuxLifecycleStore.initialize(store_path)
    commit(store)
    recovery = tmp_path / "independent"
    recovery.mkdir()
    archive_path = store.archive_backup(recovery)
    assert store.archive_backup(recovery) == archive_path
    restored = tmp_path / "restored"
    with tarfile.open(archive_path) as archive:
        archive.extractall(restored, filter="data")
    (restored / "installation").chmod(0o700)
    reopened = LinuxLifecycleStore(restored / "installation")
    assert reopened.signer.identity == store.signer.identity
    assert commit(reopened) == {"accepted": 1}
    raw = bytearray(archive_path.read_bytes())
    raw[len(raw) // 2] ^= 0xFF
    archive_path.write_bytes(raw)
    with pytest.raises((LinuxLifecycleError, OSError, tarfile.TarError)):
        store.archive_backup(recovery)


def test_linux_installation_key_never_satisfies_windows_production(store_path):
    from strathmark.v3.infrastructure.integrity import IntegrityError, require_production_cng_signer

    store = LinuxLifecycleStore.initialize(store_path)
    with pytest.raises(IntegrityError, match="Windows CNG"):
        require_production_cng_signer(store.signer)
