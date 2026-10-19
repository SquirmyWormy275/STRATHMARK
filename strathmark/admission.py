"""Finite, immediate work admission shared by optional HTTP front ends."""

import threading
import time


class WorkAdmission:
    """Reserve bounded work without a queue or retained idle caller keys."""

    def __init__(self, maximum, per_caller):
        self.maximum = maximum
        self.per_caller = per_caller
        self.active = 0
        self._callers = {}
        self._lock = threading.Lock()

    def acquire(self, caller):
        with self._lock:
            if self.active >= self.maximum or self._callers.get(caller, 0) >= self.per_caller:
                return False
            self.active += 1
            self._callers[caller] = self._callers.get(caller, 0) + 1
            return True

    def release(self, caller):
        with self._lock:
            self.active -= 1
            remaining = self._callers[caller] - 1
            if remaining:
                self._callers[caller] = remaining
            else:
                del self._callers[caller]


class LoginAttempts:
    """A fixed-size attempt window, independent of account existence."""

    def __init__(self, *, window=60, total=40, per_account=8, per_source=16):
        self.window = window
        self.total = total
        self.per_account = per_account
        self.per_source = per_source
        self._started = time.monotonic()
        self._attempts = 0
        self._accounts = {}
        self._sources = {}
        self._lock = threading.Lock()

    def admit(self, account, source):
        with self._lock:
            now = time.monotonic()
            if now - self._started >= self.window:
                self._started = now
                self._attempts = 0
                self._accounts.clear()
                self._sources.clear()
            if (
                self._attempts >= self.total
                or self._accounts.get(account, 0) >= self.per_account
                or self._sources.get(source, 0) >= self.per_source
            ):
                return False
            self._attempts += 1
            self._accounts[account] = self._accounts.get(account, 0) + 1
            self._sources[source] = self._sources.get(source, 0) + 1
            return True
