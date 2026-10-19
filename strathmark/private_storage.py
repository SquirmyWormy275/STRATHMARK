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
    return path


def private_directory(path):
    path = reject_links(path)
    if os.name == "nt":
        # Only the explicitly selected authority root is tightened. Never
        # change the user's home, its ancestors, or the shared temporary root.
        if path in (Path.home(), *Path.home().parents, Path(tempfile.gettempdir())):
            raise PermissionError("Choose a dedicated private authority subdirectory")
        missing = []
        cursor = path
        while not cursor.exists():
            missing.append(cursor)
            cursor = cursor.parent
        for directory in reversed(missing):
            _windows().directory(directory)
        _windows().directory(path)
        return path
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name == "posix":
        if path.stat().st_uid != os.getuid():
            raise PermissionError("Authority directory is owned by another account")
        if path.stat().st_mode & 0o777 != 0o700:
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
            if status.st_uid != os.getuid() or status.st_nlink != 1:
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
    elif os.name == "nt":
        # A file path does not authorize changing Documents/Desktop/a shared
        # project directory. Existing parents must already be private.
        _windows().verify_directory(path.parent)
