"""Deterministic tests for provider scheduling and the --once main path.

No threads, no real time, and no network: the scheduler is driven by a fake
monotonic clock and a recording stop event, and ``main`` runs with injected
fake services and stores.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import mudah_alerts
from mudah_alerts import CycleReport


class FakeMonotonic:
    """A clock that only moves when the stop event's wait advances it."""

    def __init__(self, start: float = 0.0):
        self.t = float(start)

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class RecordingStop:
    """A threading.Event stand-in that advances the clock and records waits."""

    def __init__(self, clock: FakeMonotonic, *, max_waits=None, until=None):
        self.clock = clock
        self.max_waits = max_waits
        self.until = until
        self.flag = False
        self.waits: list[float] = []

    def is_set(self) -> bool:
        return self.flag

    def set(self) -> None:
        self.flag = True

    def wait(self, timeout: float) -> bool:
        self.waits.append(timeout)
        self.clock.advance(timeout)
        if self.until is not None and self.clock.t >= self.until:
            self.flag = True
            return True
        if self.max_waits is not None and len(self.waits) >= self.max_waits:
            self.flag = True
            return True
        return False


class FakeService:
    def __init__(self, name: str, delay: float, clock: FakeMonotonic, *, crash=False):
        self.name = name
        self.delay = delay
        self.clock = clock
        self.crash = crash
        self.runs: list[float] = []

    def run_cycle(self) -> CycleReport:
        if self.crash:
            raise RuntimeError(f"{self.name} exploded")
        self.runs.append(self.clock.t)
        return CycleReport(started_at=datetime.now(timezone.utc))

    def next_delay(self) -> float:
        return self.delay


def _log() -> logging.Logger:
    return logging.getLogger("mudah_alerts.test.scheduling")


def run_scheduler(services, stop: RecordingStop, clock: FakeMonotonic) -> None:
    """Drive the real scheduler with the fake event and clock."""

    mudah_alerts._run_services_forever(
        services, stop, _log(), monotonic=clock  # type: ignore[arg-type]
    )


class SchedulingTests(unittest.TestCase):
    def test_backed_off_provider_does_not_delay_healthy_provider(self):
        clock = FakeMonotonic()
        stop = RecordingStop(clock, max_waits=2)
        slow = FakeService("slow", 3000.0, clock)
        healthy = FakeService("healthy", 60.0, clock)

        run_scheduler([slow, healthy], stop, clock)

        # The fixed healthy provider ran on its own 60s cadence; a shared
        # max(delay) schedule would have run it once at t=0 and then slept 3000s.
        self.assertEqual(slow.runs, [0.0])
        self.assertEqual(healthy.runs, [0.0, 60.0])
        self.assertEqual(stop.waits, [60.0, 60.0])

    def test_single_provider_keeps_its_own_delay(self):
        clock = FakeMonotonic()
        stop = RecordingStop(clock, max_waits=5)
        service = FakeService("only", 60.0, clock)

        run_scheduler([service], stop, clock)

        self.assertEqual(service.runs, [0.0, 60.0, 120.0, 180.0, 240.0])
        self.assertEqual(stop.waits, [60.0] * 5)

    def test_providers_keep_independent_cadences(self):
        clock = FakeMonotonic()
        stop = RecordingStop(clock, until=200.0)
        a = FakeService("a", 60.0, clock)
        b = FakeService("b", 90.0, clock)

        run_scheduler([a, b], stop, clock)

        self.assertEqual(a.runs, [0.0, 60.0, 120.0, 180.0])
        self.assertEqual(b.runs, [0.0, 90.0, 180.0])
        self.assertTrue(all(wait > 0 for wait in stop.waits), stop.waits)

    def test_no_busy_loop_even_with_a_zero_delay(self):
        clock = FakeMonotonic()
        stop = RecordingStop(clock, max_waits=3)
        service = FakeService("zero", 0.0, clock)

        run_scheduler([service], stop, clock)

        self.assertEqual(stop.waits, [mudah_alerts.MIN_SCHEDULE_WAIT] * 3)
        self.assertTrue(all(wait > 0 for wait in stop.waits), stop.waits)

    def test_no_services_returns_immediately(self):
        clock = FakeMonotonic()
        stop = RecordingStop(clock, max_waits=1)
        run_scheduler([], stop, clock)
        self.assertEqual(stop.waits, [])


class _FakeStore:
    def __init__(self):
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _CountingService:
    def __init__(self):
        self.calls = 0

    def run_cycle(self) -> CycleReport:
        self.calls += 1
        return CycleReport(started_at=datetime.now(timezone.utc))


class _CrashingService:
    def __init__(self):
        self.calls = 0

    def run_cycle(self) -> CycleReport:
        self.calls += 1
        raise RuntimeError("state table vanished")


class MainOnceTests(unittest.TestCase):
    def _write_config(self, tmp: str) -> Path:
        config_path = Path(tmp) / "config.json"
        config_path.write_text(
            json.dumps(
                {
                    "ibilik": {"searches": [{"name": "s1", "city": "cheras"}]},
                    "database_path": "alerts.db",
                }
            ),
            encoding="utf-8",
        )
        return config_path

    def _run_main(self, config_path: Path, builder):
        env = {"TELEGRAM_BOT_TOKEN": "test-token", "TELEGRAM_CHAT_ID": "test-chat"}
        with mock.patch.dict(os.environ, env), mock.patch.object(
            mudah_alerts, "_build_services", builder
        ):
            return mudah_alerts.main(["--config", str(config_path), "--once"])

    def test_once_contains_a_crash_and_returns_nonzero(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            config_path = self._write_config(tmp)
            store = _FakeStore()
            crashing = _CrashingService()
            healthy = _CountingService()

            def builder(_config, _sender):
                return [store], [crashing, healthy]

            code = self._run_main(config_path, builder)

            self.assertEqual(code, 1)
            self.assertEqual(crashing.calls, 1)
            self.assertEqual(healthy.calls, 1)  # the crash did not stop it
            self.assertTrue(store.closed)

    def test_once_returns_zero_when_all_providers_are_clean(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            config_path = self._write_config(tmp)
            store = _FakeStore()
            first = _CountingService()
            second = _CountingService()

            def builder(_config, _sender):
                return [store], [first, second]

            code = self._run_main(config_path, builder)

            self.assertEqual(code, 0)
            self.assertEqual(first.calls, 1)
            self.assertEqual(second.calls, 1)
            self.assertTrue(store.closed)


if __name__ == "__main__":
    unittest.main()
