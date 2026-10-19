"""Explicit private modes for authority files; never follow a final symlink."""

import os
import stat
import warnings
from pathlib import Path


def reject_links(path):
    path = Path(path).expanduser().absolute()
    for part in (path, *path.parents):
        if part.is_symlink() or getattr(part, "is_junction", lambda: False)():
            raise ValueError("Authority paths cannot contain links or junctions")
    return path


def private_directory(path):
    path = reject_links(path)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name == "posix":
        if path.stat().st_uid != os.getuid():
            raise PermissionError("Authority directory is owned by another account")
        if path.stat().st_mode & 0o777 != 0o700:
            path.chmod(0o700)
    else:
        warnings.warn(
            "Authority storage requires an independently verified owner-only Windows ACL",
            RuntimeWarning,
            stacklevel=2,
        )
    return path


def private_file(path):
    if str(path) == ":memory:":
        return path
    path = reject_links(path)
    if not path.parent.exists():
        private_directory(path.parent)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode):
            raise PermissionError("Authority path is not a regular file")
        if os.name == "posix":
            if status.st_uid != os.getuid() or status.st_nlink != 1:
                raise PermissionError("Authority file ownership or link count is unsafe")
            if status.st_mode & 0o777 != 0o600:
                os.fchmod(descriptor, 0o600)
        else:
            warnings.warn(
                "Authority storage requires an independently verified owner-only Windows ACL",
                RuntimeWarning,
                stacklevel=2,
            )
    finally:
        os.close(descriptor)
    if not str(path).endswith(("-wal", "-shm", "-journal")):
        for suffix in ("-wal", "-shm", "-journal"):
            sidecar = Path(f"{path}{suffix}")
            if sidecar.exists() or sidecar.is_symlink():
                private_file(sidecar)
    return path
