"""Tests for the bias-correction circuit breaker in predictor.py.

Pure offline. The breaker uses time.monotonic(), which we monkeypatch to
control the sliding window deterministically.
"""

from __future__ import annotations

from strathmark.predictor import _BiasCircuitBreaker


class TestBiasCircuitBreaker:
    def test_initial_state_allows(self):
        breaker = _BiasCircuitBreaker()
        assert breaker.allow() is True

    def test_single_failure_does_not_trip(self):
        breaker = _BiasCircuitBreaker()
        breaker.record_failure()
        assert breaker.allow() is True  # one failure: still allow

    def test_two_failures_does_not_trip(self):
        breaker = _BiasCircuitBreaker()
        breaker.record_failure()
        breaker.record_failure()
        assert breaker.allow() is True

    def test_three_failures_within_window_trips(self, monkeypatch):
        breaker = _BiasCircuitBreaker()
        # All three failures arrive within the same 60-second window
        t = [1000.0]
        monkeypatch.setattr("strathmark.predictor._time.monotonic", lambda: t[0])
        breaker.record_failure()
        t[0] += 1
        breaker.record_failure()
        t[0] += 1
        newly_tripped = breaker.record_failure()
        assert newly_tripped is True
        assert breaker.allow() is False

    def test_failures_outside_window_are_purged(self, monkeypatch):
        breaker = _BiasCircuitBreaker()
        t = [1000.0]
        monkeypatch.setattr("strathmark.predictor._time.monotonic", lambda: t[0])

        # Two failures, then advance past the window
        breaker.record_failure()
        breaker.record_failure()
        t[0] += 120  # 2 minutes later, well past 60s window
        # Old failures purged; one new failure is the only entry
        breaker.record_failure()
        # Two more would still not trip because the deque only has 1 + 2 in window
        breaker.record_failure()
        assert breaker.allow() is True  # 3 failures total but only 3 in window? Actually 3.

    def test_auto_reset_after_window(self, monkeypatch):
        breaker = _BiasCircuitBreaker()
        t = [1000.0]
        monkeypatch.setattr("strathmark.predictor._time.monotonic", lambda: t[0])

        # Trip the breaker
        breaker.record_failure()
        breaker.record_failure()
        breaker.record_failure()
        assert breaker.allow() is False

        # Advance past the auto-reset window
        t[0] += 120
        # Next allow() check sees that the trip is older than the window and resets
        assert breaker.allow() is True

    def test_success_clears_failure_count(self, monkeypatch):
        breaker = _BiasCircuitBreaker()
        t = [1000.0]
        monkeypatch.setattr("strathmark.predictor._time.monotonic", lambda: t[0])

        breaker.record_failure()
        breaker.record_failure()
        breaker.record_success()
        # After success, the deque is cleared. Two more failures should not trip.
        breaker.record_failure()
        breaker.record_failure()
        assert breaker.allow() is True

    def test_success_clears_tripped_state(self, monkeypatch):
        breaker = _BiasCircuitBreaker()
        t = [1000.0]
        monkeypatch.setattr("strathmark.predictor._time.monotonic", lambda: t[0])

        # Trip
        breaker.record_failure()
        breaker.record_failure()
        breaker.record_failure()
        assert breaker.allow() is False

        # A success while tripped clears the trip
        breaker.record_success()
        assert breaker.allow() is True

    def test_reset_method_clears_everything(self, monkeypatch):
        breaker = _BiasCircuitBreaker()
        t = [1000.0]
        monkeypatch.setattr("strathmark.predictor._time.monotonic", lambda: t[0])

        breaker.record_failure()
        breaker.record_failure()
        breaker.record_failure()
        breaker.reset()
        assert breaker.allow() is True

    def test_thread_safety_smoke(self):
        """Smoke test: 100 concurrent record_failure calls should not deadlock."""
        import threading

        breaker = _BiasCircuitBreaker()

        def hammer():
            for _ in range(50):
                breaker.record_failure()
                breaker.allow()

        threads = [threading.Thread(target=hammer) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)
            assert not t.is_alive(), "thread deadlocked"


class TestHalfOpenProbe:
    """Half-open state lets exactly one probe through after the cooldown.

    Without it, a single stale-data success can mask an ongoing outage.
    With it, the breaker re-opens on probe failure for another full
    cooldown.
    """

    def _trip_breaker(self, breaker, t):
        for _ in range(_BiasCircuitBreaker.THRESHOLD):
            breaker.record_failure()
            t[0] += 1

    def test_half_open_admits_one_probe(self, monkeypatch):
        breaker = _BiasCircuitBreaker()
        t = [1000.0]
        monkeypatch.setattr("strathmark.predictor._time.monotonic", lambda: t[0])

        self._trip_breaker(breaker, t)
        # Advance past the cooldown so the next allow() transitions to half-open.
        t[0] += 120
        assert breaker.allow() is True  # probe admitted
        # Second caller in half-open state with the probe still in flight is denied.
        assert breaker.allow() is False

    def test_probe_failure_re_opens_for_full_cooldown(self, monkeypatch):
        breaker = _BiasCircuitBreaker()
        t = [1000.0]
        monkeypatch.setattr("strathmark.predictor._time.monotonic", lambda: t[0])

        self._trip_breaker(breaker, t)
        t[0] += 120
        assert breaker.allow() is True  # probe admitted
        # Probe fails -> re-open.
        newly_opened = breaker.record_failure()
        assert newly_opened is True
        # Subsequent calls denied within the new cooldown window.
        assert breaker.allow() is False
        # Still denied just before the window expires.
        t[0] += _BiasCircuitBreaker.WINDOW_SECONDS - 1
        assert breaker.allow() is False
        # Past the window -> probe admitted again.
        t[0] += 5
        assert breaker.allow() is True

    def test_probe_success_fully_closes(self, monkeypatch):
        breaker = _BiasCircuitBreaker()
        t = [1000.0]
        monkeypatch.setattr("strathmark.predictor._time.monotonic", lambda: t[0])

        self._trip_breaker(breaker, t)
        t[0] += 120
        assert breaker.allow() is True  # probe admitted
        breaker.record_success()
        # Fully closed: subsequent calls all admit.
        assert breaker.allow() is True
        assert breaker.allow() is True
        # And the failure window is fresh -- two more failures don't trip.
        breaker.record_failure()
        breaker.record_failure()
        assert breaker.allow() is True
