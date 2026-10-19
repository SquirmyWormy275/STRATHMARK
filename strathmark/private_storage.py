"""Explicit private modes for authority files; never follow a final symlink."""

import os
import stat
import tempfile
from functools import lru_cache
from pathlib import Path


def reject_links(path):
    path = Path(path).expanduser().absolute()
    for part in (path, *path.parents):
        if part.is_symlink() or getattr(part, "is_junction", lambda: False)():
            raise ValueError("Authority paths cannot contain links or junctions")
        try:
            if getattr(part.lstat(), "st_file_attributes", 0) & 0x400:
                raise ValueError("Authority paths cannot contain Windows reparse points")
        except FileNotFoundError:
            pass
    if os.name == "nt" and any(":" in part or part.endswith((".", " ")) for part in path.parts[1:]):
        raise ValueError("Authority paths cannot use alternate streams or ambiguous names")
    if os.name == "nt":
        _windows().verify_ancestors(path)
    elif os.name == "posix":
        _verify_posix_ancestors(path)
    return path


def _verify_posix_ancestors(path):
    """Reject replaceable ancestry without changing any shared directory modes."""
    # Check from the root down: each checked parent protects the next name.
    # POSIX ACL mask permissions are reflected in the group mode bits.
    for parent in reversed(path.parents):
        try:
            status = parent.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISDIR(status.st_mode):
            raise PermissionError(f"Authority ancestor is not a directory: {parent}")
        if status.st_uid not in (0, os.geteuid()):
            raise PermissionError(
                f"Authority ancestor has an unrelated owner: {parent} (uid={status.st_uid})"
            )
        if status.st_mode & 0o022 and not status.st_mode & stat.S_ISVTX:
            raise PermissionError(
                f"Authority ancestor is replaceable by another account: {parent} "
                f"(mode={stat.S_IMODE(status.st_mode):04o})"
            )


def private_directory(path):
    path = reject_links(path)
    # Only an explicitly selected dedicated root may be tightened, on either OS.
    if path in (Path.home(), *Path.home().parents, Path(tempfile.gettempdir())):
        raise PermissionError("Choose a dedicated private authority subdirectory")
    if os.name == "nt":
        missing = []
        cursor = path
        while not cursor.exists():
            missing.append(cursor)
            cursor = cursor.parent
        for directory in reversed(missing):
            _windows().directory(directory)
        _windows().directory(path)
        return path
    if os.name == "posix":
        missing = []
        cursor = path
        while not cursor.exists():
            missing.append(cursor)
            cursor = cursor.parent
        for directory in reversed(missing):
            reject_links(directory)
            directory.mkdir(mode=0o700, exist_ok=True)
        # A concurrent creator of a missing intermediate directory must not
        # acquire control of the new authority path before any secret is used.
        reject_links(path)
        status = path.stat()
        if not stat.S_ISDIR(status.st_mode):
            raise NotADirectoryError(f"Authority path is not a directory: {path}")
        if status.st_uid != os.geteuid():
            raise PermissionError("Authority directory is owned by another account")
        if status.st_mode & 0o777 != 0o700:
            path.chmod(0o700)
    else:
        raise PermissionError("Private authority storage is unsupported on this platform")
    return path


def private_file(path):
    if str(path) == ":memory:":
        return path
    path = reject_links(path)
    _private_parent(path)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    descriptor = (
        _windows().descriptor_file(path) if os.name == "nt" else os.open(path, flags, 0o600)
    )
    try:
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode):
            raise PermissionError("Authority path is not a regular file")
        if os.name == "posix":
            if status.st_uid != os.geteuid() or status.st_nlink != 1:
                raise PermissionError("Authority file ownership or link count is unsafe")
            if status.st_mode & 0o777 != 0o600:
                os.fchmod(descriptor, 0o600)
        elif os.name != "nt":
            raise PermissionError("Private authority storage is unsupported on this platform")
    finally:
        os.close(descriptor)
    if not str(path).endswith(("-wal", "-shm", "-journal")):
        for suffix in ("-wal", "-shm", "-journal"):
            sidecar = Path(f"{path}{suffix}")
            if sidecar.exists() or sidecar.is_symlink():
                private_file(sidecar)
    return path


@lru_cache(maxsize=1)
def _windows():
    from strathmark._windows_storage import WindowsStorage

    return WindowsStorage()


def open_private_exclusive(path):
    """Return a new private writable descriptor without an ACL exposure window."""
    path = reject_links(path)
    _private_parent(path)
    if os.name == "nt":
        return _windows().descriptor_file(path, exclusive=True)
    return os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)


def _private_parent(path):
    if not path.parent.exists():
        private_directory(path.parent)
    _verify_parent(path)


def _verify_parent(path):
    if os.name == "nt":
        # A file path does not authorize changing Documents/Desktop/a shared
        # project directory. Existing parents must already be private.
        _windows().verify_directory(path.parent)
    elif os.name == "posix" and path.parent.stat().st_mode & 0o022:
        # Sticky protects existing names, but peers can still precreate SQLite
        # journal/WAL siblings before SQLite opens them by their fixed names.
        raise PermissionError(
            f"Authority parent permits another account to create sibling files: {path.parent}"
        )


def verify_authority_parent(path):
    """Check a file's existing parent without creating or changing any paths."""
    path = reject_links(path)
    _verify_parent(path)
    return path


def verify_private_directory(path):
    """Check an existing authority root without changing any permissions."""
    path = reject_links(path)
    if os.name == "nt":
        _windows().verify_directory(path)
    elif os.name == "posix":
        status = path.stat()
        if (
            not stat.S_ISDIR(status.st_mode)
            or status.st_uid != os.geteuid()
            or status.st_mode & 0o777 != 0o700
        ):
            raise PermissionError(
                "Authority directory must already be private and owner-controlled"
            )
    else:
        raise PermissionError("Private authority storage is unsupported on this platform")
    return path
