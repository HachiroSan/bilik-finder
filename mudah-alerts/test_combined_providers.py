"""Offline regression tests for the combined Mudah + iBilik alert service.

These prove the behaviour the combined setup depends on:

* the default poll interval is one hour and the shipped combined examples parse,
* with ``MUDAH_AUTOMATION_ALLOWED`` off the Mudah side is dropped while iBilik
  keeps polling (and a Mudah-only config fails closed),
* one provider crashing or failing does not stop the other in ``--once``,
* ``.env`` loading is stdlib-only, real-environment-wins, and discloses no
  values,
* repeated cycles / restarts do not resend the same inventory.

No network, Telegram, or Mudah call is made: search, sender, clock, and sleep
are injected. Existing provider suites stay in ``test_mudah_alerts.py`` and
``test_ibilik_alerts.py``.
"""

from __future__ import annotations

import contextlib
import io
import json
import logging
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
_ROOT = _HERE.parent
sys.path.insert(0, str(_ROOT))

import mudah_alerts  # noqa: E402
from mudah_alerts import (  # noqa: E402
    IbilikAlertService,
    IbilikSearch,
    IbilikSentStore,
    Keyword,
    SendResult,
    parse_config,
)

import run_alerts  # noqa: E402


UTC = timezone.utc
NOW = datetime(2025, 10, 11, 6, 0, 0, tzinfo=UTC)


class FakeClock:
    def __init__(self, start: datetime = NOW):
        self.current = start

    def __call__(self) -> datetime:
        return self.current

    def advance(self, seconds: float) -> None:
        self.current = self.current + timedelta(seconds=seconds)


class FakeIbilikSearch:
    def __init__(self, pages=None):
        self.pages = dict(pages or {})
        self.calls: list[tuple[str, int]] = []

    def __call__(self, search, *, page, limit, timeout):
        self.calls.append((search.name, page))
        entry = self.pages.get(search.name, [])
        if isinstance(entry, Exception):
            raise entry
        items = entry[page - 1] if page - 1 < len(entry) else []
        return {
            "items": list(items),
            "pagination": {"page": page, "has_next": page < len(entry)},
        }


class FakeSender:
    def __init__(self, results=None):
        self.results = list(results or [])
        self.messages: list[str] = []

    def send(self, text: str) -> SendResult:
        self.messages.append(text)
        if self.results:
            return self.results.pop(0)
        return SendResult(True)


def fresh_ibilik_item(listing_id: str, *, hours_ago: float = 1.0, **kwargs) -> dict:
    published = NOW - timedelta(hours=hours_ago)
    item = {
        "id": listing_id,
        "title": "Room near rail",
        "price": 800,
        "charge_unit": "PER_MONTH",
        "state": "Kuala Lumpur",
        "city": "Cheras",
        "area": None,
        "url": f"https://www.ibilik.com/room-rentals/{listing_id}/slug",
        "published_at": published.isoformat(),
    }
    item.update(kwargs)
    return item


def combined_dict(database_path: str = "alerts.db", **overrides) -> dict:
    """The same shape the shipped combined examples use (existing defaults)."""

    base = {
        "poll_interval_seconds": 3600,
        "database_path": database_path,
        "keywords": [{"query": "Platinum Victory 3", "max_price": 1800}],
        "ibilik": {
            "initial_seed": False,
            "searches": [{"name": "cheras-rooms", "city": "cheras", "max_price": 900}],
        },
    }
    base.update(overrides)
    return base


class GateTests(unittest.TestCase):
    def test_gate_off_drops_mudah_but_keeps_ibilik_and_other_fields(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            config = parse_config(combined_dict(), base_dir=Path(tmp))
        gated = mudah_alerts._apply_mudah_gate(
            config, allowed=False, logger=logging.getLogger("test.gate")
        )
        self.assertEqual(gated.keywords, ())
        self.assertIsNotNone(gated.ibilik)
        # Ordering, DB path, and iBilik searches survive the reduction.
        self.assertEqual(gated.database_path, config.database_path)
        assert gated.ibilik is not None and config.ibilik is not None
        self.assertEqual(gated.ibilik.searches, config.ibilik.searches)

    def test_gate_on_keeps_both_providers(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            config = parse_config(combined_dict(), base_dir=Path(tmp))
        kept = mudah_alerts._apply_mudah_gate(
            config, allowed=True, logger=logging.getLogger("test.gate")
        )
        self.assertTrue(kept.keywords)
        self.assertIsNotNone(kept.ibilik)
        self.assertEqual(kept.keywords, (Keyword("Platinum Victory 3", 1800),))

    def test_ibilik_only_config_is_unaffected_by_the_gate(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            config = parse_config(
                {"ibilik": {"searches": [{"name": "cheras", "city": "cheras"}]}},
                base_dir=Path(tmp),
            )
        gated = mudah_alerts._apply_mudah_gate(
            config, allowed=False, logger=logging.getLogger("test.gate")
        )
        self.assertEqual(gated.keywords, ())
        self.assertIsNotNone(gated.ibilik)

    def test_mudah_only_config_becomes_providerless(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            config = parse_config({"keywords": ["alpha"]}, base_dir=Path(tmp))
        gated = mudah_alerts._apply_mudah_gate(
            config, allowed=False, logger=logging.getLogger("test.gate")
        )
        self.assertEqual(gated.keywords, ())
        self.assertIsNone(gated.ibilik)


class DefaultConfigTests(unittest.TestCase):
    def test_default_poll_is_one_hour(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            config = parse_config({"keywords": ["alpha"]}, base_dir=Path(tmp))
        self.assertEqual(config.poll_interval_seconds, 3600.0)

    def test_explicit_poll_still_overrides_the_default(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            config = parse_config(
                {"keywords": ["alpha"], "poll_interval_seconds": 300},
                base_dir=Path(tmp),
            )
        self.assertEqual(config.poll_interval_seconds, 300.0)

    def test_shipped_combined_examples_parse(self):
        examples = [
            _ROOT / "config.example.json",
            _ROOT / "mudah-alerts" / "config.example.json",
            _ROOT / "deploy" / "config.example.json",
        ]
        for path in examples:
            with self.subTest(path=str(path)):
                self.assertTrue(path.is_file(), path)
                raw = json.loads(path.read_text(encoding="utf-8"))
                config = parse_config(raw, base_dir=path.parent)
                self.assertEqual(config.poll_interval_seconds, 3600.0)
                self.assertTrue(config.keywords)
                self.assertIsNotNone(config.ibilik)
                assert config.ibilik is not None
                self.assertTrue(config.ibilik.searches)

    def test_ibilik_only_example_still_parses(self):
        path = _ROOT / "mudah-alerts" / "config.ibilik.example.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        config = parse_config(raw, base_dir=path.parent)
        self.assertFalse(config.keywords)
        self.assertIsNotNone(config.ibilik)


class DotenvTests(unittest.TestCase):
    def test_real_env_wins_and_only_missing_keys_are_set(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        "# comment line",
                        "TELEGRAM_BOT_TOKEN=from-file",
                        "export MUDAH_AUTOMATION_ALLOWED='0'",
                        'QUOTED="hello world"',
                        "A=B=C",
                        "BLANK=",
                        "not a pair",
                        "1INVALID=x",
                    ]
                ),
                encoding="utf-8",
            )
            environ = {"TELEGRAM_BOT_TOKEN": "from-real-env"}
            loaded = mudah_alerts.load_dotenv(env_path, environ=environ)

        self.assertEqual(environ["TELEGRAM_BOT_TOKEN"], "from-real-env")
        self.assertNotIn("TELEGRAM_BOT_TOKEN", loaded)
        self.assertEqual(environ["MUDAH_AUTOMATION_ALLOWED"], "0")
        self.assertEqual(environ["QUOTED"], "hello world")
        self.assertEqual(environ["A"], "B=C")
        self.assertEqual(environ["BLANK"], "")
        self.assertNotIn("1INVALID", environ)
        # The loader reports names, never values.
        self.assertNotIn("from-file", loaded)

    def test_missing_file_is_a_noop(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            self.assertEqual(
                mudah_alerts.load_dotenv(Path(tmp) / "absent.env", environ={}), []
            )


class CombinedCycleTests(unittest.TestCase):
    def test_combined_wiring_builds_only_ibilik_when_mudah_is_gated_off(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            config = parse_config(
                combined_dict(database_path=str(Path(tmp) / "alerts.db")),
                base_dir=Path(tmp),
            )
            gated = mudah_alerts._apply_mudah_gate(
                config, allowed=False, logger=logging.getLogger("test.combined")
            )
            stores, services = mudah_alerts._build_services(gated, sender=FakeSender())
            try:
                self.assertEqual(len(services), 1)
                self.assertIsInstance(services[0], IbilikAlertService)
            finally:
                for store in stores:
                    store.close()

    def test_gate_off_cycle_still_fetches_and_sends_ibilik(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            clock = FakeClock()
            search = FakeIbilikSearch({"cheras-rooms": [[fresh_ibilik_item("u1")]]})
            sender = FakeSender()
            config = parse_config(
                combined_dict(database_path=str(Path(tmp) / "alerts.db")),
                base_dir=Path(tmp),
            )
            gated = mudah_alerts._apply_mudah_gate(
                config, allowed=False, logger=logging.getLogger("test.combined")
            )
            store = IbilikSentStore(gated.database_path)
            try:
                assert gated.ibilik is not None
                service = IbilikAlertService(
                    gated.ibilik,
                    search=search,
                    sender=sender,
                    store=store,
                    sleep=lambda _s: None,
                    now=clock,
                    jitter=lambda _span: 0.0,
                )
                report = service.run_cycle()
            finally:
                store.close()
        self.assertTrue(search.calls)  # iBilik was actually fetched
        self.assertEqual(report.sent, ["u1"])
        self.assertEqual(len(sender.messages), 1)

    def test_main_dry_run_with_gate_off_reaches_ibilik_only(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            config_path = Path(tmp) / "config.json"
            config_path.write_text(json.dumps(combined_dict()), encoding="utf-8")
            seen = {}

            def fake_dry(config, _log):
                seen["keywords"] = config.keywords
                seen["ibilik"] = config.ibilik
                return 0

            with mock.patch.dict(
                os.environ, {"MUDAH_AUTOMATION_ALLOWED": "0"}, clear=False
            ), mock.patch.object(mudah_alerts, "_run_dry", fake_dry):
                code = mudah_alerts.main(["--config", str(config_path), "--dry-run"])
        self.assertEqual(code, 0)
        self.assertEqual(seen["keywords"], ())
        self.assertIsNotNone(seen["ibilik"])

    def test_main_mudah_only_with_gate_off_fails_closed(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            config_path = Path(tmp) / "config.json"
            config_path.write_text(
                json.dumps({"keywords": ["alpha"], "database_path": "alerts.db"}),
                encoding="utf-8",
            )
            dry_called = []
            with mock.patch.dict(
                os.environ, {"MUDAH_AUTOMATION_ALLOWED": "0"}, clear=False
            ), mock.patch.object(
                mudah_alerts, "_run_dry", lambda *_a: dry_called.append(True) or 0
            ):
                code = mudah_alerts.main(["--config", str(config_path), "--dry-run"])
        self.assertEqual(code, 2)
        self.assertEqual(dry_called, [])


class RunOnceIsolationTests(unittest.TestCase):
    def test_crashing_mudah_service_does_not_stop_ibilik(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            clock = FakeClock()
            sender = FakeSender()
            store = IbilikSentStore(Path(tmp) / "alerts.db")
            service = IbilikAlertService(
                mudah_alerts.IbilikConfig(
                    searches=(IbilikSearch(name="s1", city="cheras"),),
                    initial_seed=False,
                ),
                search=FakeIbilikSearch({"s1": [[fresh_ibilik_item("u1")]]}),
                sender=sender,
                store=store,
                sleep=lambda _s: None,
                now=clock,
                jitter=lambda _span: 0.0,
            )

            class CrashingMudah:
                def run_cycle(self):
                    raise RuntimeError("mudah state table vanished")

            try:
                code = mudah_alerts._run_once(
                    [CrashingMudah(), service],
                    logging.getLogger("test.runonce"),
                )
            finally:
                store.close()

        self.assertEqual(code, 1)  # a provider crashed
        self.assertEqual(len(sender.messages), 1)  # iBilik still ran and sent


class RepeatedCycleTests(unittest.TestCase):
    def test_repeated_cycles_and_restart_do_not_resend(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            clock = FakeClock()
            db = Path(tmp) / "alerts.db"
            search = FakeIbilikSearch({"s1": [[fresh_ibilik_item("u1")]]})
            config = mudah_alerts.IbilikConfig(
                searches=(IbilikSearch(name="s1", city="cheras"),),
                initial_seed=False,
            )

            sender = FakeSender()
            store = IbilikSentStore(db)
            service = IbilikAlertService(
                config,
                search=search,
                sender=sender,
                store=store,
                sleep=lambda _s: None,
                now=clock,
                jitter=lambda _span: 0.0,
            )
            first = service.run_cycle()
            second = service.run_cycle()
            store.close()

            # A fresh process over the same database must also stay quiet.
            sender2 = FakeSender()
            store2 = IbilikSentStore(db)
            service2 = IbilikAlertService(
                config,
                search=search,
                sender=sender2,
                store=store2,
                sleep=lambda _s: None,
                now=clock,
                jitter=lambda _span: 0.0,
            )
            third = service2.run_cycle()
            store2.close()

        self.assertEqual(first.sent, ["u1"])
        self.assertEqual(second.sent, [])
        self.assertEqual(third.sent, [])
        self.assertEqual(len(sender.messages), 1)
        self.assertEqual(sender2.messages, [])


class LauncherTests(unittest.TestCase):
    def test_default_config_path_prefers_config_then_example(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            with mock.patch.object(run_alerts, "_ROOT", root):
                self.assertEqual(
                    run_alerts.default_config_path(), root / "config.example.json"
                )
                (root / "config.json").write_text("{}", encoding="utf-8")
                self.assertEqual(
                    run_alerts.default_config_path(), root / "config.json"
                )

    def test_launcher_loads_env_injects_config_and_leaks_no_secret(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            (root / "config.json").write_text("{}", encoding="utf-8")
            (root / ".env").write_text(
                "TELEGRAM_BOT_TOKEN=top-secret-value\n", encoding="utf-8"
            )
            seen = {}

            def fake_service_main(argv):
                seen["argv"] = list(argv)
                return 0

            out, err = io.StringIO(), io.StringIO()
            with mock.patch.object(run_alerts, "_ROOT", root), mock.patch.object(
                run_alerts.mudah_alerts, "main", fake_service_main
            ), mock.patch.dict(os.environ, {}, clear=False), contextlib.redirect_stdout(
                out
            ), contextlib.redirect_stderr(err):
                code = run_alerts.main([])

        self.assertEqual(code, 0)
        self.assertEqual(seen["argv"], ["--config", str(root / "config.json")])
        self.assertNotIn("top-secret-value", out.getvalue() + err.getvalue())


if __name__ == "__main__":
    unittest.main()
