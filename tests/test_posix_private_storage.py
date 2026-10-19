"""Authority cannot be reopened through a directory another account can replace."""

import os
import stat
import tempfile
from pathlib import Path

import pytest

from strathmark.private_storage import (
    open_private_exclusive,
    private_directory,
    private_file,
    verify_private_directory,
)

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX ownership and sticky modes")


def test_shared_home_and_temporary_roots_are_never_tightened():
    for root in (Path.home(), Path.home().parent, Path(tempfile.gettempdir())):
        before = root.stat().st_mode
        with pytest.raises(PermissionError, match="dedicated"):
            private_directory(root)
        assert root.stat().st_mode == before


def test_directory_helper_rejects_a_file_without_changing_it(tmp_path):
    path = tmp_path / "file"
    path.write_bytes(b"retained file")
    path.chmod(0o600)
    with pytest.raises(NotADirectoryError, match="not a directory"):
        private_directory(path)
    assert path.read_bytes() == b"retained file"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize("mode", [0o770, 0o777, 0o1777])
@pytest.mark.parametrize("operation", [private_file, open_private_exclusive])
def test_shared_parent_is_refused_before_file_creation(tmp_path, mode, operation):
    root = tmp_path / "shared"
    root.mkdir()
    root.chmod(mode)
    target = root / "authority.sqlite3"
    with pytest.raises(PermissionError, match="replaceable.*shared|sibling files.*shared"):
        operation(target)
    assert not target.exists()
    assert stat.S_IMODE(root.stat().st_mode) == mode


def test_private_child_cannot_hide_replaceable_ancestry_or_change_existing_files(tmp_path):
    shared = tmp_path / "shared"
    root = shared / "authority"
    root.mkdir(parents=True, mode=0o700)
    target = root / "secret"
    target.write_bytes(b"retained")
    target.chmod(0o644)
    shared.chmod(0o777)
    for operation, path in (
        (private_file, target),
        (private_directory, root),
        (verify_private_directory, root),
    ):
        with pytest.raises(PermissionError, match="replaceable.*shared"):
            operation(path)
    assert target.read_bytes() == b"retained"
    assert stat.S_IMODE(target.stat().st_mode) == 0o644
    assert stat.S_IMODE(shared.stat().st_mode) == 0o777


@pytest.mark.parametrize("sticky_ancestor", [False, True])
def test_safe_parent_and_trusted_sticky_root_remain_unchanged(tmp_path, sticky_ancestor):
    from strathmark.store import ResultStore

    ancestor = tmp_path / "ancestor"
    ancestor.mkdir()
    mode = 0o1777 if sticky_ancestor else 0o755
    ancestor.chmod(mode)
    root = ancestor / "safe"
    root.mkdir()
    root.chmod(0o755)
    database = root / "results.sqlite3"
    store = ResultStore(database)
    with store._connect() as connection:
        assert connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    assert stat.S_IMODE(database.stat().st_mode) == 0o600
    assert stat.S_IMODE(root.stat().st_mode) == 0o755
    assert stat.S_IMODE(ancestor.stat().st_mode) == mode


def test_new_nested_roots_remain_private_even_under_umask_zero(tmp_path):
    from strathmark.v3.linux_lifecycle_store import LinuxLifecycleStore

    previous = os.umask(0)
    try:
        database = private_file(tmp_path / "one" / "two" / "authority.sqlite3")
        store = LinuxLifecycleStore.initialize(tmp_path / "three" / "four" / "authority")
    finally:
        os.umask(previous)
    for path in (database.parent, database.parent.parent, store.root, *store.root.parents[:2]):
        assert stat.S_IMODE(path.stat().st_mode) == 0o700
    assert stat.S_IMODE(database.stat().st_mode) == 0o600
    assert stat.S_IMODE((store.root / "installation-key.pem").stat().st_mode) == 0o600


def test_read_only_parent_verification_does_not_require_parent_write(tmp_path):
    root = private_directory(tmp_path / "authority")
    target = private_file(root / "secret")
    tmp_path.chmod(0o555)
    try:
        assert verify_private_directory(root) == root
        assert private_file(target) == target
        assert stat.S_IMODE(tmp_path.stat().st_mode) == 0o555
    finally:
        tmp_path.chmod(0o700)


def test_sticky_does_not_trust_an_unrelated_directory_owner(tmp_path, monkeypatch):
    root = tmp_path / "unrelated"
    root.mkdir()
    root.chmod(0o1777)
    original = Path.lstat

    def unrelated_owner(path, *args, **kwargs):
        status = original(path, *args, **kwargs)
        if path == root:
            fields = list(status)
            fields[4] = os.geteuid() + 1
            return os.stat_result(fields)
        return status

    monkeypatch.setattr(Path, "lstat", unrelated_owner)
    with pytest.raises(PermissionError, match="unrelated owner.*unrelated"):
        private_file(root / "secret")
    assert not (root / "secret").exists()


def test_new_intermediate_directory_is_rechecked_before_secret_creation(tmp_path, monkeypatch):
    root = tmp_path / "intermediate" / "authority"
    original = Path.mkdir

    def replaceable_intermediate(path, *args, **kwargs):
        result = original(path, *args, **kwargs)
        if path == root:
            path.parent.chmod(0o777)
        return result

    monkeypatch.setattr(Path, "mkdir", replaceable_intermediate)
    with pytest.raises(PermissionError, match="replaceable.*intermediate"):
        private_file(root / "secret")
    assert not (root / "secret").exists()
    assert stat.S_IMODE(root.parent.stat().st_mode) == 0o777


@pytest.mark.parametrize("store_kind", ["results", "ledger", "v3", "managed"])
def test_database_callers_refuse_shared_parent_before_sqlite_open(tmp_path, store_kind):
    from strathmark.events.store import EventStore
    from strathmark.ledger import PredictionLedger
    from strathmark.store import ResultStore
    from strathmark.v3.infrastructure.sqlite.connection import open_v3_connection

    root = tmp_path / "shared"
    root.mkdir()
    root.chmod(0o777)
    constructors = {
        "results": ResultStore,
        "ledger": PredictionLedger,
        "v3": open_v3_connection,
        "managed": EventStore,
    }
    with pytest.raises(PermissionError, match="replaceable"):
        constructors[store_kind](root / "authority.sqlite3")
    assert not list(root.iterdir())
    assert stat.S_IMODE(root.stat().st_mode) == 0o777


@pytest.mark.parametrize("mode", [0o777, 0o1777])
def test_existing_store_and_read_only_v3_open_recheck_parent(tmp_path, mode):
    from strathmark.store import ResultStore
    from strathmark.v3.infrastructure.sqlite.connection import open_v3_connection

    root = tmp_path / "authority"
    root.mkdir(mode=0o700)
    database = root / "results.sqlite3"
    store = ResultStore(database)
    before = database.read_bytes()
    root.chmod(mode)
    with pytest.raises(PermissionError, match="replaceable|sibling files"):
        store._connect()
    with pytest.raises(PermissionError, match="replaceable|sibling files"):
        open_v3_connection(database, read_only=True)
    assert database.read_bytes() == before


def test_managed_store_checks_raw_symlink_and_keeps_database_and_head_private(tmp_path):
    from strathmark.events.store import EventStore

    root = tmp_path / "authority"
    root.mkdir(mode=0o755)
    root.chmod(0o755)
    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)
    with pytest.raises(ValueError, match="links"):
        EventStore(alias / "managed.sqlite3")
    store = EventStore(root / "managed.sqlite3")
    for path in (store.path, store.head):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(root.stat().st_mode) == 0o755


def test_managed_transfer_rejects_unsafe_target_before_fencing_source(tmp_path):
    from strathmark.events import EventOperations, Principal
    from strathmark.events.examples import synthetic_event

    operations = EventOperations(tmp_path / "authority.sqlite3")
    actor = Principal("synthetic", frozenset({"synthetic:missoula"}), "director")
    state = operations.create(actor, "create", synthetic_event(), "cli")
    shared = tmp_path / "shared"
    shared.mkdir()
    shared.chmod(0o777)
    with pytest.raises(PermissionError, match="replaceable"):
        operations.transfer_to(
            actor,
            state["event_id"],
            shared / "transferred.sqlite3",
            request_id="transfer",
            expected_revision=state["revision"],
            editor="cli",
        )
    assert operations.read(actor, state["event_id"]) == state
    assert not list(shared.iterdir())


def test_workspace_credentials_and_new_accounts_reject_shared_parent(tmp_path, monkeypatch):
    import getpass

    from strathmark.events.contracts import EventError
    from strathmark.events.workspace import (
        add_account,
        new_session_key,
    )
    from strathmark.events.workspace import (
        private_file as workspace_file,
    )

    root = tmp_path / "shared"
    root.mkdir()
    existing = root / "operators.json"
    existing.write_text("{}")
    existing.chmod(0o600)
    root.chmod(0o777)
    with pytest.raises(EventError, match="private_configuration_path_unsafe"):
        workspace_file(existing)
    monkeypatch.setattr(getpass, "getpass", lambda *args: pytest.fail("unsafe path prompted"))
    with pytest.raises(EventError, match="private_configuration_path_unsafe"):
        add_account(root / "new-operators.json", "synthetic", "synthetic")
    with pytest.raises(EventError, match="private_configuration_path_unsafe"):
        new_session_key(root / "new-session.key")
    assert not (root / "new-operators.json").exists()
    assert not (root / "new-session.key").exists()
    assert existing.read_text() == "{}"


def test_plaintext_lifecycle_storage_rejects_unsafe_roots_and_rechecks_on_use(tmp_path):
    from strathmark.v3.linux_lifecycle_store import LinuxLifecycleError, LinuxLifecycleStore

    shared = tmp_path / "shared"
    shared.mkdir()
    shared.chmod(0o777)
    with pytest.raises(LinuxLifecycleError, match="replaceable"):
        LinuxLifecycleStore.initialize(shared / "installation")
    assert not (shared / "installation").exists()
    store = LinuxLifecycleStore.initialize(tmp_path / "safe" / "installation")
    with pytest.raises(LinuxLifecycleError, match="replaceable"):
        store.backup(shared / "backup")
    assert not (shared / "backup").exists()
    with pytest.raises(LinuxLifecycleError, match="replaceable"):
        store.archive_backup(shared)
    assert not list(shared.iterdir())
    before = (store.root / "head.json").read_bytes()
    store.root.parent.chmod(0o777)
    with pytest.raises(LinuxLifecycleError, match="replaceable"):
        store.state()
    assert (store.root / "head.json").read_bytes() == before
