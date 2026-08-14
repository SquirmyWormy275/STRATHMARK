"""Small SQLite lifecycle helpers shared by local authoritative stores."""

from __future__ import annotations

import sqlite3
from types import TracebackType
from typing import Optional, Type


class ClosingConnection(sqlite3.Connection):
    """Commit/rollback like ``sqlite3.Connection`` and then always close."""

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc_value: Optional[BaseException],
        traceback: Optional[TracebackType],
    ) -> bool:
        try:
            return bool(super().__exit__(exc_type, exc_value, traceback))
        finally:
            self.close()


__all__ = ["ClosingConnection"]
