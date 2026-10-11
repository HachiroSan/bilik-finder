"""Offline tests for the opt-in iBilik side of the alert service.

No Mudah or iBilik request is made: the search client, sender, clock, and sleep
are all injected. Mudah's own suite stays in ``test_mudah_alerts.py``.
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import mudah_alerts
from mudah_alerts import (
    AlertService,
    ConfigError,
    IbilikAlertService,
    IbilikConfig,
    IbilikSearch,
    IbilikSentStore,
    Keyword,
    SendResult,
    SentStore,
    build_ibilik_message,
    parse_config,
)

# Importing mudah_alerts puts the sibling finder directories on sys.path.
import mudah  # type: ignore[import-not-found]  # noqa: E402

UTC = timezone.utc
NOW = datetime(2025, 10, 11, 6, 0, 0, tzinfo=UTC)
IBILIK_URL = "https://www.ibilik.com/room-rentals/u1/some-slug"


def ibilik_item(
    listing_id: str,
    *,
    title: str = "Room near LRT",
    price: object = 800,
    charge_unit: str | None = "PER_MONTH",
    state: str | None = "Selangor",
    city: str | None = "Cheras",
    area: str | None = "Bandar Mahkota Cheras",
    url: str | None = None,
    published_at: str | None = None,
    contact_phone: str | None = "0123456789",
    whatsapp: bool = True,
) -> dict:
    return {
        "id": listing_id,
        "title": title,
        "price": price,
        "charge_unit": charge_unit,
        "state": state,
        "city": city,
        "area": area,
        "url": url if url is not None else f"https://www.ibilik.com/room-rentals/{listing_id}/slug",
        "published_at": published_at,
        "contact_phone": contact_phone,
        "whatsapp": whatsapp,
    }


def fresh_item(listing_id: str, *, hours_ago: float = 1.0, **kwargs) -> dict:
    published = NOW - timedelta(hours=hours_ago)
    return ibilik_item(listing_id, published_at=published.isoformat(), **kwargs)


class FakeClock:
    def __init__(self, start: datetime):
        self.current = start

    def __call__(self) -> datetime:
        return self.current

    def advance(self, seconds: float) -> None:
        self.current = self.current + timedelta(seconds=seconds)


class FakeIbilikSearch:
    """Serve a page map keyed by search name.

    A value may be a list of page-item-lists, or an exception instance to raise
    for the whole search.
    """

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
            "pagination": {
                "page": page,
                "total_pages": len(entry),
                "has_next": page < len(entry),
            },
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


class BrokenIbilikStore:
    def known_ids(self):
        raise sqlite3.OperationalError("database is locked")

    def initialized_searches(self):
        raise sqlite3.OperationalError("database is locked")


class OneShotStop:
    """A stop event that lets exactly one loop iteration through."""

    def __init__(self):
        self.flag = False

    def is_set(self):
        return self.flag

    def wait(self, _delay):
        self.flag = True
        return True


def make_ibilik_config(tmp_dir: str, **overrides) -> IbilikConfig:
    base = {
        "searches": (IbilikSearch(name="s1", city="cheras"),),
        "poll_interval_seconds": 120.0,
        "freshness_hours": 48.0,
        "max_pages_per_search": 2,
        "initial_seed": True,
    }
    base.update(overrides)
    return IbilikConfig(**base)


def make_service(tmp, search, sender, clock, *, config=None, store=None, **kwargs):
    if store is None:
        store = IbilikSentStore(Path(tmp) / "alerts.db")
    config = config or make_ibilik_config(tmp)
    service = IbilikAlertService(
        config,
        search=search,
        sender=sender,
        store=store,
        sleep=lambda _seconds: None,
        now=clock,
        jitter=lambda _span: 0.0,
        **kwargs,
    )
    return service, store


class ConfigTests(unittest.TestCase):
    def test_existing_mudah_config_defaults_to_no_ibilik(self):
        config = parse_config({"keywords": ["alpha"]}, base_dir=Path("."))
        self.assertIsNone(config.ibilik)
        self.assertEqual(config.keywords, (Keyword("alpha"),))
        self.assertTrue(mudah_alerts._mudah_gate_required(config))

    def test_ibilik_only_config_is_allowed_and_skips_the_mudah_gate(self):
        config = parse_config(
            {"ibilik": {"searches": [{"name": "cheras", "city": "cheras"}]}},
            base_dir=Path("."),
        )
        self.assertEqual(config.keywords, ())
        ibilik = config.ibilik
        assert ibilik is not None
        self.assertEqual(ibilik.searches[0].city, "cheras")
        self.assertFalse(mudah_alerts._mudah_gate_required(config))

    def test_missing_both_providers_is_rejected(self):
        with self.assertRaises(ConfigError):
            parse_config({}, base_dir=Path("."))
        with self.assertRaises(ConfigError):
            parse_config({"ibilik": {"enabled": False}}, base_dir=Path("."))

    def test_explicit_empty_keywords_still_rejected(self):
        with self.assertRaises(ConfigError):
            parse_config(
                {"keywords": [], "ibilik": {"searches": [{"name": "a", "city": "cheras"}]}},
                base_dir=Path("."),
            )

    def test_ibilik_search_validation(self):
        bad_searches = [
            [{"name": "Cheras rooms", "city": "cheras"}],
            [{"name": "a", "city": "Kuala Lumpur"}],
            [{"name": "a", "room_type": "LOFTY"}],
            [{"name": "a", "bathroom_type": "ENSUITE"}],
            [{"name": "a", "unit_type": "TENT"}],
            [{"name": "a", "floor_level": "BASEMENT"}],
            [{"name": "a", "min_price": 900, "max_price": 500}],
            [{"name": "a", "kyc_verified": "yes"}],
            [{"name": "a", "amenities": "wifi"}],
            [{"name": "a", "unexpected": 1}],
            [{"name": "a"}, {"name": "a"}],
            {"name": "a"},
        ]
        for searches in bad_searches:
            with self.subTest(searches=searches), self.assertRaises(ConfigError):
                parse_config({"ibilik": {"searches": searches}}, base_dir=Path("."))

    def test_ibilik_block_unknown_key_and_bad_booleans_rejected(self):
        for block in (
            {"searches": [{"name": "a"}], "extra": 1},
            {"searches": [{"name": "a"}], "enabled": "yes"},
            {"searches": [{"name": "a"}], "initial_seed": 1},
            {"searches": [{"name": "a"}], "poll_interval_seconds": 5},
            {"searches": [{"name": "a"}], "max_pages_per_search": 99},
            {"searches": [{"name": "a"}], "limit_per_page": 101},
        ):
            with self.subTest(block=block), self.assertRaises(ConfigError):
                parse_config({"ibilik": block}, base_dir=Path("."))

    def test_ibilik_can_override_poll_and_be_disabled(self):
        config = parse_config(
            {
                "keywords": ["alpha"],
                "poll_interval_seconds": 300,
                "ibilik": {"searches": [{"name": "a", "area": "bandar-mahkota-cheras"}]},
            },
            base_dir=Path("."),
        )
        ibilik = config.ibilik
        assert ibilik is not None
        self.assertEqual(ibilik.poll_interval_seconds, 300.0)

        disabled = parse_config(
            {"keywords": ["alpha"], "ibilik": {"enabled": False}},
            base_dir=Path("."),
        )
        self.assertIsNone(disabled.ibilik)


class MessageTests(unittest.TestCase):
    def test_message_carries_provider_title_price_location_and_time(self):
        item = ibilik_item("u1", title="<b>Cheap</b> & near rail", price=850)
        message = build_ibilik_message(item, published_at=NOW - timedelta(hours=2), now=NOW)
        assert message is not None
        self.assertIn("&lt;b&gt;Cheap&lt;/b&gt; &amp; near rail", message)
        self.assertIn("iBilik", message)
        self.assertIn("RM 850/month", message)
        self.assertIn("Selangor / Cheras / Bandar Mahkota Cheras", message)
        self.assertIn("Posted 2 h ago", message)
        self.assertIn('href="https://www.ibilik.com/room-rentals/u1/slug"', message)
        self.assertIn("Open on iBilik", message)

    def test_message_omits_contact_number_and_whatsapp(self):
        item = ibilik_item("u1", contact_phone="0123456789", whatsapp=True)
        message = build_ibilik_message(item)
        assert message is not None
        self.assertNotIn("0123456789", message)
        self.assertNotIn("whatsapp", message.lower())
        self.assertNotIn("(WA)", message)

    def test_per_night_unit_is_labelled(self):
        item = ibilik_item("u1", price=120, charge_unit="PER_NIGHT")
        message = build_ibilik_message(item)
        assert message is not None
        self.assertIn("RM 120/night", message)

    def test_foreign_or_missing_url_returns_none(self):
        for url in (
            "http://www.ibilik.com/room-rentals/u1/slug",
            "https://evil.example/room-rentals/u1",
            "javascript:alert(1)",
            "",
        ):
            with self.subTest(url=url):
                self.assertIsNone(build_ibilik_message(ibilik_item("u1", url=url)))

    def test_missing_price_returns_none(self):
        for price in (None, -1, float("nan"), "800", True):
            with self.subTest(price=str(price)):
                self.assertIsNone(build_ibilik_message(ibilik_item("u1", price=price)))

    def test_message_length_is_bounded(self):
        item = ibilik_item("u1", title="x" * 10_000)
        message = build_ibilik_message(item, limit=200)
        assert message is not None
        self.assertLessEqual(len(message), 200)


class StoreTests(unittest.TestCase):
    def test_seed_marks_known_without_counting_as_sent(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = Path(tmp) / "alerts.db"
            with IbilikSentStore(path) as store:
                store.seed_search("s1", [(ibilik_item("u1"), NOW)], NOW.isoformat())
                self.assertFalse(store.is_sent("s1", "u1"))
                self.assertIn("u1", store.known_ids())
                self.assertEqual(store.initialized_searches(), {"s1"})

    def test_failed_row_stays_retryable(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = Path(tmp) / "alerts.db"
            with IbilikSentStore(path) as store:
                store.upsert_candidate(
                    search_name="s1",
                    listing_id="u1",
                    item=ibilik_item("u1"),
                    published_at=NOW,
                    now_iso=NOW.isoformat(),
                )
                store.mark_failure("s1", "u1", "boom", NOW.isoformat())
                self.assertNotIn("u1", store.known_ids())
                self.assertEqual(store.failure_count("s1", "u1"), 1)
                store.mark_sent("s1", "u1", NOW.isoformat())
                self.assertTrue(store.is_sent("s1", "u1"))

    def test_read_only_store_tolerates_a_mudah_only_database(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = Path(tmp) / "alerts.db"
            # A database created before iBilik existed has only the Mudah table.
            with SentStore(path):
                pass
            with IbilikSentStore(path, read_only=True) as store:
                self.assertEqual(store.known_ids(), set())
                self.assertEqual(store.initialized_searches(), set())

    def test_overlapping_ids_across_providers_do_not_collide(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = Path(tmp) / "alerts.db"
            # Mudah delivered a listing whose id happens to match an iBilik one.
            with SentStore(path) as mudah_store:
                mudah_store.upsert_candidate(
                    listing_id="shared",
                    keyword="alpha",
                    item={"id": "shared", "url": "https://www.mudah.my/x.htm", "price": 900},
                    published_at=NOW,
                    now_iso=NOW.isoformat(),
                )
                mudah_store.mark_sent("shared", NOW.isoformat())

            with IbilikSentStore(path) as ibilik_store:
                # The Mudah row is invisible to iBilik state...
                self.assertEqual(ibilik_store.known_ids(), set())
                ibilik_store.seed_search(
                    "s1", [(ibilik_item("shared"), NOW)], NOW.isoformat()
                )
                self.assertTrue(ibilik_store.is_sent("s1", "shared") is False)

            # ...and the Mudah row survives untouched beside the iBilik tables.
            with SentStore(path) as mudah_check:
                self.assertTrue(mudah_check.is_sent("shared"))
            with IbilikSentStore(path) as ibilik_check:
                self.assertIn("shared", ibilik_check.known_ids())


class ServiceTests(unittest.TestCase):
    def test_first_run_seeds_without_sending(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            clock = FakeClock(NOW)
            search = FakeIbilikSearch({"s1": [[fresh_item("u1"), fresh_item("u2")]]})
            sender = FakeSender()
            service, store = make_service(tmp, search, sender, clock)
            report = service.run_cycle()
            self.assertEqual(report.seeded, ["s1"])
            self.assertEqual(report.sent, [])
            self.assertEqual(sender.messages, [])
            self.assertEqual(store.initialized_searches(), {"s1"})
            store.close()

    def test_new_only_then_restart_dedupe(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            clock = FakeClock(NOW)
            db = Path(tmp) / "alerts.db"
            search = FakeIbilikSearch({"s1": [[fresh_item("u1")]]})
            sender = FakeSender()
            service, store = make_service(tmp, search, sender, clock, store=IbilikSentStore(db))
            service.run_cycle()  # seed

            search.pages["s1"] = [[fresh_item("u1"), fresh_item("u2")]]
            second = service.run_cycle()
            self.assertEqual(second.sent, ["u2"])
            self.assertEqual(len(sender.messages), 1)
            store.close()

            # A fresh process over the same database must not resend u2.
            sender2 = FakeSender()
            store2 = IbilikSentStore(db)
            service2 = IbilikAlertService(
                make_ibilik_config(tmp),
                search=search,
                sender=sender2,
                store=store2,
                sleep=lambda _s: None,
                now=clock,
            )
            third = service2.run_cycle()
            self.assertEqual(third.sent, [])
            self.assertEqual(sender2.messages, [])
            store2.close()

    def test_send_failure_is_retried_and_only_success_marks_sent(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            clock = FakeClock(NOW)
            db = Path(tmp) / "alerts.db"
            config = make_ibilik_config(tmp, initial_seed=False)
            sender = FakeSender(results=[SendResult(False, "temporary network error")])
            service, store = make_service(
                tmp,
                FakeIbilikSearch({"s1": [[fresh_item("u1")]]}),
                sender,
                clock,
                config=config,
            )
            first = service.run_cycle()
            self.assertEqual(first.failed, ["u1"])
            self.assertEqual(first.sent, [])
            self.assertFalse(store.is_sent("s1", "u1"))
            self.assertEqual(store.failure_count("s1", "u1"), 1)
            store.close()

            sender2 = FakeSender()
            store2 = IbilikSentStore(db)
            service2 = IbilikAlertService(
                config,
                search=FakeIbilikSearch({"s1": [[fresh_item("u1")]]}),
                sender=sender2,
                store=store2,
                sleep=lambda _s: None,
                now=clock,
            )
            second = service2.run_cycle()
            self.assertEqual(second.sent, ["u1"])
            self.assertEqual(len(sender2.messages), 1)
            self.assertTrue(store2.is_sent("s1", "u1"))
            store2.close()

    def test_first_run_with_seed_disabled_sends_immediately(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            clock = FakeClock(NOW)
            config = make_ibilik_config(tmp, initial_seed=False)
            sender = FakeSender()
            service, store = make_service(
                tmp,
                FakeIbilikSearch({"s1": [[fresh_item("u1")]]}),
                sender,
                clock,
                config=config,
            )
            report = service.run_cycle()
            self.assertEqual(report.seeded, [])
            self.assertEqual(report.sent, ["u1"])
            self.assertEqual(len(sender.messages), 1)
            store.close()

    def test_partial_first_run_fetch_does_not_seed(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            clock = FakeClock(NOW)
            pages = {
                "s1": [
                    [fresh_item("p1")],  # page 1
                    ValueError("schema broke"),  # page 2
                ]
            }
            sender = FakeSender()
            service, store = make_service(
                tmp,
                FakeIbilikSearch(pages),
                sender,
                clock,
                config=make_ibilik_config(tmp, max_pages_per_search=3),
            )
            report = service.run_cycle()
            self.assertEqual(report.seeded, [])
            self.assertEqual(report.sent, [])
            self.assertEqual(sender.messages, [])
            self.assertTrue(report.errors)
            self.assertEqual(store.known_ids(), set())
            self.assertEqual(store.initialized_searches(), set())
            store.close()

    def test_one_search_failure_does_not_stop_the_other(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            clock = FakeClock(NOW)
            config = make_ibilik_config(
                tmp,
                initial_seed=False,
                searches=(
                    IbilikSearch(name="broken", city="cheras"),
                    IbilikSearch(name="good", state="selangor"),
                ),
            )
            search = FakeIbilikSearch(
                {
                    "broken": ValueError("bad schema"),
                    "good": [[fresh_item("u9")]],
                }
            )
            sender = FakeSender()
            service, store = make_service(tmp, search, sender, clock, config=config)
            report = service.run_cycle()
            self.assertEqual(report.sent, ["u9"])
            self.assertEqual(len(sender.messages), 1)
            self.assertEqual(report.searches_searched, 1)
            self.assertEqual(len(report.errors), 1)
            store.close()

    def test_overlapping_listing_across_searches_is_sent_once(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            clock = FakeClock(NOW)
            config = make_ibilik_config(
                tmp,
                initial_seed=False,
                searches=(
                    IbilikSearch(name="a", city="cheras"),
                    IbilikSearch(name="b", state="selangor"),
                ),
            )
            item = fresh_item("same")
            search = FakeIbilikSearch({"a": [[item]], "b": [[dict(item)]]})
            sender = FakeSender()
            service, store = make_service(tmp, search, sender, clock, config=config)
            report = service.run_cycle()
            self.assertEqual(report.sent, ["same"])
            self.assertEqual(len(sender.messages), 1)
            store.close()

    def test_freshness_and_price_filters_apply(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            clock = FakeClock(NOW)
            config = make_ibilik_config(
                tmp,
                initial_seed=False,
                searches=(IbilikSearch(name="s1", city="cheras", max_price=1000),),
            )
            items = [
                fresh_item("ok", price=900),
                ibilik_item("stale", price=900, published_at=(NOW - timedelta(hours=49)).isoformat()),
                fresh_item("pricey", price=5000),
                ibilik_item("noprice", price=None, published_at=(NOW - timedelta(hours=1)).isoformat()),
                ibilik_item("notime", price=800, published_at=None),
            ]
            sender = FakeSender()
            service, store = make_service(
                tmp, FakeIbilikSearch({"s1": [items]}), sender, clock, config=config
            )
            report = service.run_cycle()
            self.assertEqual(report.sent, ["ok"])
            self.assertEqual(report.skipped_stale, 1)
            self.assertEqual(report.skipped_over_price, 1)
            self.assertEqual(report.skipped_missing_price, 1)
            self.assertEqual(report.skipped_invalid_timestamp, 1)
            store.close()

    def test_state_read_error_aborts_without_sending(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            clock = FakeClock(NOW)
            sender = FakeSender()
            service = IbilikAlertService(
                make_ibilik_config(tmp, initial_seed=False),
                search=FakeIbilikSearch({"s1": [[fresh_item("u1")]]}),
                sender=sender,
                store=BrokenIbilikStore(),  # type: ignore[arg-type]
                sleep=lambda _s: None,
                now=clock,
                jitter=lambda _span: 0.0,
            )
            report = service.run_cycle()
            self.assertEqual(sender.messages, [])
            self.assertEqual(report.sent, [])
            self.assertTrue(report.errors)
            self.assertGreaterEqual(
                service.next_delay(), mudah_alerts.BACKOFF_BASE_SECONDS
            )

    def test_dry_run_writes_nothing(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            clock = FakeClock(NOW)
            db = Path(tmp) / "alerts.db"
            with IbilikSentStore(db):
                pass
            read_only = IbilikSentStore(db, read_only=True)
            service = IbilikAlertService(
                make_ibilik_config(tmp, initial_seed=True),
                search=FakeIbilikSearch({"s1": [[fresh_item("u1")]]}),
                sender=FakeSender(),
                store=read_only,
                sleep=lambda _s: None,
                now=clock,
                dry_run=True,
            )
            report = service.run_cycle()
            self.assertEqual(report.would_seed, ["s1"])
            self.assertEqual(report.sent, [])
            read_only.close()
            with IbilikSentStore(db) as check:
                self.assertEqual(check.known_ids(), set())
                self.assertEqual(check.initialized_searches(), set())

            # With seeding off, dry-run reports what it would send and still writes nothing.
            read_only = IbilikSentStore(db, read_only=True)
            service = IbilikAlertService(
                make_ibilik_config(tmp, initial_seed=False),
                search=FakeIbilikSearch({"s1": [[fresh_item("u1")]]}),
                sender=FakeSender(),
                store=read_only,
                sleep=lambda _s: None,
                now=clock,
                dry_run=True,
            )
            report = service.run_cycle()
            self.assertEqual(report.would_send, ["u1"])
            read_only.close()


class ProviderIsolationTests(unittest.TestCase):
    def test_mudah_failure_does_not_stop_ibilik_polling(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            clock = FakeClock(NOW)
            db = Path(tmp) / "alerts.db"

            mudah_store = SentStore(db)
            mudah_service = AlertService(
                mudah_alerts.AlertConfig(
                    keywords=(Keyword("alpha"),),
                    database_path=db,
                    max_pages_per_keyword=1,
                ),
                search=FakeIbilikSearch(),  # not a valid Mudah search
                sender=FakeSender(),
                store=mudah_store,
                sleep=lambda _s: None,
                now=clock,
                jitter=lambda _span: 0.0,
            )

            ibilik_store = IbilikSentStore(db)
            ibilik_sender = FakeSender()
            ibilik_service = IbilikAlertService(
                make_ibilik_config(tmp, initial_seed=False),
                search=FakeIbilikSearch({"s1": [[fresh_item("u1")]]}),
                sender=ibilik_sender,
                store=ibilik_store,
                sleep=lambda _s: None,
                now=clock,
                jitter=lambda _span: 0.0,
            )

            # Make the Mudah fetch fail the way a real parse error would.
            def broken_search(**_kwargs):
                raise mudah.MudahParseError("schema changed")

            mudah_service.search = broken_search

            stop = OneShotStop()
            mudah_alerts._run_services_forever(
                [mudah_service, ibilik_service],
                stop,  # type: ignore[arg-type]
                mudah_alerts.logging.getLogger("test"),
            )

            self.assertEqual(len(ibilik_sender.messages), 1)
            ibilik_store.close()
            mudah_store.close()


if __name__ == "__main__":
    unittest.main()
