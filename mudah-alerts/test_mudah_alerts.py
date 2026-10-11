from __future__ import annotations

import io
import json
import logging
import sqlite3
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import mudah_alerts
from mudah_alerts import (
    AlertConfig,
    AlertService,
    ConfigError,
    Keyword,
    LockError,
    SendResult,
    SentStore,
    SingleInstanceLock,
    TelegramSender,
    build_message,
    is_fresh,
    listing_published_at,
    load_config,
    parse_config,
    parse_timestamp,
    sender_from_env,
)

# Importing mudah_alerts puts the sibling mudah-finder directory on sys.path.
import mudah  # type: ignore[import-not-found]  # noqa: E402


UTC = timezone.utc
NOW = datetime(2025, 10, 11, 6, 0, 0, tzinfo=UTC)


def make_item(
    listing_id: str,
    *,
    title: str = "Room near rail",
    price: object = 900,
    url: str = "https://www.mudah.my/example-1.htm",
    published_at: str | None = None,
    site_date: str | None = None,
    modified_at: str | None = None,
) -> dict:
    return {
        "id": listing_id,
        "title": title,
        "price": price,
        "url": url,
        "published_at": published_at,
        "site_date": site_date,
        "modified_at": modified_at,
    }


class FakeClock:
    def __init__(self, start: datetime):
        self.current = start

    def __call__(self) -> datetime:
        return self.current

    def advance(self, seconds: float) -> None:
        self.current = self.current + timedelta(seconds=seconds)


class ScriptedClock:
    """Return each scripted time in order, then repeat the last one."""

    def __init__(self, times):
        self._times = list(times)
        self.last = self._times[-1]

    def __call__(self) -> datetime:
        if self._times:
            self.last = self._times.pop(0)
        return self.last


class PagedSearch:
    """Serve a page map; an exception value is raised for that page."""

    def __init__(self, pages):
        self.pages = pages
        self.calls: list[int] = []

    def __call__(self, **kwargs):
        page = int(kwargs.get("page", 1))
        self.calls.append(page)
        entry = self.pages.get(page)
        if isinstance(entry, Exception):
            raise entry
        if entry is None:
            return {"items": [], "pagination": {"has_next": False, "organic_items_on_page": 0}}
        return entry


class BrokenStore:
    """A store whose read fails, to prove the service does not fail open."""

    def sent_ids(self):
        raise sqlite3.OperationalError("database is locked")


class FakeSearch:
    """Return canned pages keyed by keyword query."""

    def __init__(self, pages=None, error: Exception | None = None):
        self.pages = pages or {}
        self.error = error
        self.calls: list[dict] = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        query = kwargs.get("query")
        page = int(kwargs.get("page", 1))
        pages = self.pages.get(query, [])
        items = pages[page - 1] if page - 1 < len(pages) else []
        return {
            "items": list(items),
            "pagination": {
                "has_next": page < len(pages),
                "organic_items_on_page": len(items),
            },
        }


class FakeSender:
    def __init__(self, results=None, default: SendResult | None = None):
        self.results = list(results or [])
        self.default = default or SendResult(True)
        self.messages: list[str] = []

    def send(self, text: str) -> SendResult:
        self.messages.append(text)
        if self.results:
            return self.results.pop(0)
        return self.default


def make_config(tmp_dir: str, **overrides) -> AlertConfig:
    base = {
        "keywords": (Keyword("kw"),),
        "database_path": Path(tmp_dir) / "alerts.db",
        "poll_interval_seconds": 120.0,
        "freshness_hours": 48.0,
        "max_pages_per_keyword": 1,
    }
    base.update(overrides)
    return AlertConfig(**base)


def fresh_item(listing_id: str, clock: FakeClock, *, hours_ago: float = 1.0, **kwargs):
    published = clock.current - timedelta(hours=hours_ago)
    return make_item(
        listing_id, published_at=published.isoformat(), **kwargs
    )


class ConfigTests(unittest.TestCase):
    def test_defaults_apply_and_paths_resolve_from_config_dir(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            config = parse_config({"keywords": ["alpha"]}, base_dir=Path(tmp))
        self.assertEqual(config.poll_interval_seconds, 3600.0)
        self.assertEqual(config.freshness_hours, 48.0)
        self.assertEqual(config.max_pages_per_keyword, 2)
        self.assertEqual(config.database_path, (Path(tmp) / "mudah_alerts.db").resolve())
        self.assertEqual(config.lock_path, Path(str(config.database_path) + ".lock"))
        self.assertEqual(config.location, "malaysia")
        self.assertEqual(config.rental_type, "room")
        self.assertEqual(config.keywords, (Keyword("alpha"),))

    def test_poll_interval_below_sixty_is_rejected(self):
        with self.assertRaises(ConfigError):
            parse_config(
                {"keywords": ["alpha"], "poll_interval_seconds": 59},
                base_dir=Path("."),
            )
        config = parse_config(
            {"keywords": ["alpha"], "poll_interval_seconds": 60},
            base_dir=Path("."),
        )
        self.assertEqual(config.poll_interval_seconds, 60.0)

    def test_non_finite_numbers_are_rejected(self):
        for field in ("poll_interval_seconds", "freshness_hours", "page_pacing_seconds"):
            with self.subTest(field=field), self.assertRaises(ConfigError):
                parse_config(
                    {"keywords": ["alpha"], field: float("inf")},
                    base_dir=Path("."),
                )
            with self.subTest(field=field, kind="nan"), self.assertRaises(ConfigError):
                parse_config(
                    {"keywords": ["alpha"], field: float("nan")},
                    base_dir=Path("."),
                )

    def test_telegram_secrets_in_config_are_rejected(self):
        for key in ("telegram_bot_token", "TELEGRAM_CHAT_ID", "token"):
            with self.subTest(key=key), self.assertRaises(ConfigError):
                parse_config(
                    {"keywords": ["alpha"], key: "secret"},
                    base_dir=Path("."),
                )

    def test_keyword_forms_and_validation(self):
        config = parse_config(
            {
                "keywords": [
                    "Platinum Victory 3",
                    {"query": "Putra Villa Condominium", "max_price": 1800},
                ]
            },
            base_dir=Path("."),
        )
        self.assertEqual(config.keywords[0], Keyword("Platinum Victory 3", None))
        self.assertEqual(config.keywords[1], Keyword("Putra Villa Condominium", 1800))

        for bad in ([], [""], [{"no_query": 1}], [123], [{"query": "x", "extra": 1}]):
            with self.subTest(bad=bad), self.assertRaises(ConfigError):
                parse_config({"keywords": bad}, base_dir=Path("."))

    def test_max_price_and_rental_type_are_validated(self):
        with self.assertRaises(ConfigError):
            parse_config(
                {"keywords": ["a"], "max_price": -1}, base_dir=Path(".")
            )
        with self.assertRaises(ConfigError):
            parse_config(
                {"keywords": ["a"], "rental_type": "spaceship"}, base_dir=Path(".")
            )
        with self.assertRaises(ConfigError):
            parse_config({"keywords": ["a"], "location": "Kuala Lumpur"}, base_dir=Path("."))

    def test_load_config_reads_file_and_resolves_relative_paths(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            config_path = Path(tmp) / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "keywords": ["alpha"],
                        "database_path": "data/alerts.db",
                    }
                ),
                encoding="utf-8",
            )
            config = load_config(config_path)
        self.assertEqual(
            config.database_path, (Path(tmp) / "data" / "alerts.db").resolve()
        )

    def test_load_config_reports_missing_file(self):
        with self.assertRaises(ConfigError):
            load_config(Path(tempfile.gettempdir()) / "does-not-exist-mudah.json")


class TimestampTests(unittest.TestCase):
    def test_relative_hours_and_minutes(self):
        self.assertEqual(
            parse_timestamp("2 hours ago", now=NOW), NOW - timedelta(hours=2)
        )
        self.assertEqual(
            parse_timestamp("45 minutes ago", now=NOW), NOW - timedelta(minutes=45)
        )
        self.assertEqual(
            parse_timestamp("an hour ago", now=NOW), NOW - timedelta(hours=1)
        )
        self.assertEqual(
            parse_timestamp("3 days ago", now=NOW), NOW - timedelta(days=3)
        )

    def test_just_now_variants(self):
        for text in ("just now", "now", "moments ago", "a moment ago"):
            with self.subTest(text=text):
                self.assertEqual(parse_timestamp(text, now=NOW), NOW)

    def test_today_and_yesterday_use_malaysian_wall_clock(self):
        # 2025-10-10T16:00Z is exactly 2025-10-11 00:00 in UTC+8.
        clock = datetime(2025, 10, 10, 16, 0, 0, tzinfo=UTC)
        today = parse_timestamp("today 00:30", now=clock)
        yesterday = parse_timestamp("yesterday 23:30", now=clock)
        self.assertEqual(today, datetime(2025, 10, 10, 16, 30, tzinfo=UTC))
        self.assertTrue(today > clock)  # still in the future, so rejected
        self.assertEqual(yesterday, datetime(2025, 10, 10, 15, 30, tzinfo=UTC))
        self.assertTrue(is_fresh(today, now=clock, freshness_hours=48) is False)
        self.assertTrue(is_fresh(yesterday, now=clock, freshness_hours=48))

    def test_explicit_dates_iso_and_clock(self):
        self.assertEqual(
            parse_timestamp("10 Oct 2025 08:00", now=NOW),
            datetime(2025, 10, 10, 0, 0, tzinfo=UTC),
        )
        self.assertEqual(
            parse_timestamp("2025-10-10T04:30:00+00:00", now=NOW),
            datetime(2025, 10, 10, 4, 30, tzinfo=UTC),
        )
        self.assertEqual(
            parse_timestamp("10 Oct 2025", now=NOW),
            datetime(2025, 10, 9, 16, 0, tzinfo=UTC),
        )
        self.assertEqual(
            parse_timestamp("10/10/2025", now=NOW),
            datetime(2025, 10, 9, 16, 0, tzinfo=UTC),
        )

    def test_epoch_numbers_and_bad_values(self):
        self.assertEqual(
            parse_timestamp(1_790_000_000, now=NOW),
            datetime.fromtimestamp(1_790_000_000, tz=UTC),
        )
        for bad in (None, "", "   ", "not a date", object(), True, {}):
            with self.subTest(bad=bad):
                self.assertIsNone(parse_timestamp(bad, now=NOW))

    def test_published_at_priority_and_modified_at_ignored(self):
        item = make_item(
            "1",
            published_at="2025-10-11T04:00:00+00:00",
            site_date="2 hours ago",
            modified_at="2025-10-11T05:59:00+00:00",
        )
        self.assertEqual(
            listing_published_at(item, now=NOW),
            datetime(2025, 10, 11, 4, 0, tzinfo=UTC),
        )

        only_modified = make_item(
            "2",
            published_at=None,
            site_date=None,
            modified_at="2025-10-11T05:59:00+00:00",
        )
        self.assertIsNone(listing_published_at(only_modified, now=NOW))

        fallback = make_item("3", published_at="garbage", site_date="3 hours ago")
        self.assertEqual(
            listing_published_at(fallback, now=NOW), NOW - timedelta(hours=3)
        )


class FreshnessTests(unittest.TestCase):
    def test_boundary_is_inclusive_at_forty_eight_hours(self):
        self.assertTrue(
            is_fresh(NOW - timedelta(hours=48), now=NOW, freshness_hours=48)
        )
        self.assertFalse(
            is_fresh(NOW - timedelta(hours=48, seconds=1), now=NOW, freshness_hours=48)
        )

    def test_future_and_missing_are_rejected(self):
        self.assertFalse(
            is_fresh(NOW + timedelta(seconds=1), now=NOW, freshness_hours=48)
        )
        self.assertFalse(is_fresh(None, now=NOW, freshness_hours=48))
        self.assertFalse(is_fresh("2025-10-11", now=NOW, freshness_hours=48))


class MessageTests(unittest.TestCase):
    def test_message_escapes_html_and_links_valid_url(self):
        item = make_item(
            "1",
            title="<b>Cheap</b> & cheerful",
            price=850,
            url="https://www.mudah.my/listing-1.htm",
        )
        message = build_message(item, published_at=NOW - timedelta(hours=2), now=NOW)
        assert message is not None
        self.assertIn("&lt;b&gt;Cheap&lt;/b&gt; &amp; cheerful", message)
        self.assertIn('href="https://www.mudah.my/listing-1.htm"', message)
        self.assertIn("RM 850/month", message)
        self.assertIn("Posted 2 h ago", message)

    def test_invalid_or_foreign_urls_return_none(self):
        for url in (
            "http://www.mudah.my/listing.htm",
            "https://evil.example/listing.htm",
            "javascript:alert(1)",
            "",
            None,
        ):
            with self.subTest(url=url):
                self.assertIsNone(build_message(make_item("1", url=url or "")))

    def test_message_length_is_bounded(self):
        item = make_item("1", title="x" * 10_000)
        message = build_message(item, limit=200)
        assert message is not None
        self.assertLessEqual(len(message), 200)
        self.assertTrue(message.endswith("Open on Mudah</a>") or "…" in message)


class StoreTests(unittest.TestCase):
    def test_sent_state_persists_across_reopen(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = Path(tmp) / "alerts.db"
            item = make_item("1")
            with SentStore(path) as store:
                store.upsert_candidate(
                    listing_id="1",
                    keyword="alpha",
                    item=item,
                    published_at=NOW,
                    now_iso=NOW.isoformat(),
                )
                self.assertFalse(store.is_sent("1"))
                store.mark_failure("1", "boom", NOW.isoformat())
                self.assertEqual(store.failure_count("1"), 1)
                store.mark_sent("1", NOW.isoformat())

            with SentStore(path) as reopened:
                self.assertTrue(reopened.is_sent("1"))
                self.assertIn("1", reopened.sent_ids())
                # A sent listing keeps its failure history for auditability.
                self.assertEqual(reopened.failure_count("1"), 1)

    def test_read_only_store_has_no_ids_for_missing_file(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            with SentStore(Path(tmp) / "absent.db", read_only=True) as store:
                self.assertEqual(store.sent_ids(), set())


class ServiceDedupeTests(unittest.TestCase):
    def _service(self, tmp, search, sender, clock, config=None, **kwargs):
        store = kwargs.pop("store", None)
        if store is None:
            store = SentStore(Path(tmp) / "alerts.db")
        config = config or make_config(tmp)
        service = AlertService(
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

    def test_sends_once_and_skips_after_restart(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            clock = FakeClock(NOW)
            search = FakeSearch({"kw": [[fresh_item("42", clock)]]})
            sender = FakeSender()
            service, store = self._service(tmp, search, sender, clock)
            config = service.config
            first = service.run_cycle()
            self.assertEqual(first.sent, ["42"])
            self.assertEqual(len(sender.messages), 1)
            store.close()

            # A brand-new service over the same database must not resend.
            search2 = FakeSearch({"kw": [[fresh_item("42", clock)]]})
            sender2 = FakeSender()
            store2 = SentStore(Path(tmp) / "alerts.db")
            service2 = AlertService(
                config,
                search=search2,
                sender=sender2,
                store=store2,
                sleep=lambda _s: None,
                now=clock,
            )
            second = service2.run_cycle()
            self.assertEqual(second.sent, [])
            self.assertEqual(sender2.messages, [])
            store2.close()

    def test_duplicate_across_keywords_is_sent_once(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            clock = FakeClock(NOW)
            item = fresh_item("42", clock)
            search = FakeSearch({"alpha": [[item]], "beta": [[dict(item)]]})
            sender = FakeSender()
            service, store = self._service(
                tmp,
                search,
                sender,
                clock,
                config=make_config(tmp, keywords=(Keyword("alpha"), Keyword("beta"))),
            )
            report = service.run_cycle()
            self.assertEqual(report.sent, ["42"])
            self.assertEqual(len(sender.messages), 1)
            store.close()

    def test_freshness_and_price_filters_apply(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            clock = FakeClock(NOW)
            items = [
                fresh_item("fresh-ok", clock, hours_ago=1, price=900),
                make_item(
                    "stale",
                    published_at=(NOW - timedelta(hours=49)).isoformat(),
                    price=900,
                ),
                fresh_item("too-expensive", clock, hours_ago=1, price=5000),
                make_item("no-price", published_at=fresh_item("x", clock)["published_at"], price=None),
                make_item(
                    "no-time",
                    published_at=None,
                    site_date=None,
                    price=900,
                ),
            ]
            search = FakeSearch({"kw": [items]})
            sender = FakeSender()
            service, store = self._service(
                tmp, search, sender, clock, config=make_config(tmp, max_price=2000)
            )
            report = service.run_cycle()
            self.assertEqual(report.sent, ["fresh-ok"])
            self.assertEqual(report.skipped_stale, 1)
            self.assertEqual(report.skipped_over_price, 1)
            self.assertEqual(report.skipped_missing_price, 1)
            self.assertEqual(report.skipped_invalid_timestamp, 1)
            store.close()

    def test_per_keyword_price_override(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            clock = FakeClock(NOW)
            item = fresh_item("pricey", clock, hours_ago=1, price=2500)
            search = FakeSearch({"cheap": [[item]], "premium": [[dict(item)]]})
            sender = FakeSender()
            config = make_config(
                tmp,
                keywords=(
                    Keyword("cheap"),
                    Keyword("premium", max_price=3000),
                ),
                max_price=2000,
            )
            service, store = self._service(tmp, search, sender, clock, config=config)
            report = service.run_cycle()
            self.assertEqual(report.sent, ["pricey"])
            self.assertEqual(report.skipped_over_price, 1)
            store.close()

    def test_dry_run_sends_and_writes_nothing(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            clock = FakeClock(NOW)
            search = FakeSearch({"kw": [[fresh_item("42", clock)]]})
            sender = FakeSender()
            db = Path(tmp) / "alerts.db"
            with SentStore(db):
                pass
            read_only = SentStore(db, read_only=True)
            service = AlertService(
                make_config(tmp),
                search=search,
                sender=sender,
                store=read_only,
                sleep=lambda _s: None,
                now=clock,
                dry_run=True,
            )
            report = service.run_cycle()
            self.assertEqual(report.would_send, ["42"])
            self.assertEqual(report.sent, [])
            self.assertEqual(sender.messages, [])
            read_only.close()
            with SentStore(db) as check:
                self.assertEqual(check.sent_ids(), set())

    def test_dry_run_skips_already_sent_listings(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            clock = FakeClock(NOW)
            db = Path(tmp) / "alerts.db"
            with SentStore(db) as store:
                store.upsert_candidate(
                    listing_id="42",
                    keyword="kw",
                    item=fresh_item("42", clock),
                    published_at=clock.current,
                    now_iso=clock.current.isoformat(),
                )
                store.mark_sent("42", clock.current.isoformat())
            sender = FakeSender()
            read_only = SentStore(db, read_only=True)
            service = AlertService(
                make_config(tmp),
                search=FakeSearch({"kw": [[fresh_item("42", clock)]]}),
                sender=sender,
                store=read_only,
                sleep=lambda _s: None,
                now=clock,
                dry_run=True,
            )
            report = service.run_cycle()
            self.assertEqual(report.would_send, [])
            self.assertEqual(sender.messages, [])
            read_only.close()


class TelegramBehaviourTests(unittest.TestCase):
    def test_failed_send_is_retried_and_only_success_marks_sent(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            clock = FakeClock(NOW)
            search = FakeSearch({"kw": [[fresh_item("42", clock)]]})
            sender = FakeSender(results=[SendResult(False, "temporary network error")])
            db = Path(tmp) / "alerts.db"
            store = SentStore(db)
            service = AlertService(
                make_config(tmp),
                search=search,
                sender=sender,
                store=store,
                sleep=lambda _s: None,
                now=clock,
            )
            first = service.run_cycle()
            self.assertEqual(first.failed, ["42"])
            self.assertEqual(first.sent, [])
            store.close()

            with SentStore(db) as check:
                self.assertFalse(check.is_sent("42"))
                self.assertEqual(check.failure_count("42"), 1)

            # Next poll retries the same listing and succeeds.
            sender2 = FakeSender()
            store2 = SentStore(db)
            service2 = AlertService(
                make_config(tmp),
                search=FakeSearch({"kw": [[fresh_item("42", clock)]]}),
                sender=sender2,
                store=store2,
                sleep=lambda _s: None,
                now=clock,
            )
            second = service2.run_cycle()
            self.assertEqual(second.sent, ["42"])
            self.assertEqual(len(sender2.messages), 1)
            with SentStore(db) as check:
                self.assertTrue(check.is_sent("42"))
            store2.close()

    def test_rate_limit_stops_sends_and_extends_next_delay(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            clock = FakeClock(NOW)
            items = [fresh_item(str(i), clock) for i in range(3)]
            search = FakeSearch({"kw": [items]})
            sender = FakeSender(results=[SendResult(False, "rate limited", 30.0)])
            store = SentStore(Path(tmp) / "alerts.db")
            service = AlertService(
                make_config(tmp),
                search=search,
                sender=sender,
                store=store,
                sleep=lambda _s: None,
                now=clock,
                jitter=lambda _span: 0.0,
            )
            report = service.run_cycle()
            self.assertEqual(len(sender.messages), 1)
            self.assertEqual(len(report.failed), 1)
            self.assertGreaterEqual(service.next_delay(), 31.0)
            # The retry window is consumed once, so a clean cycle returns to polling.
            self.assertEqual(service.next_delay(), 120.0)
            store.close()

    def test_blocked_search_backs_off_then_recovers(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            clock = FakeClock(NOW)
            blocked = FakeSearch(error=mudah.MudahBlockedError("challenge page"))
            sender = FakeSender()
            store = SentStore(Path(tmp) / "alerts.db")
            service = AlertService(
                make_config(tmp),
                search=blocked,
                sender=sender,
                store=store,
                sleep=lambda _s: None,
                now=clock,
                jitter=lambda _span: 0.0,
            )
            service.run_cycle()
            first_delay = service.next_delay()
            self.assertGreaterEqual(first_delay, mudah_alerts.BACKOFF_BASE_SECONDS)
            # A second failed cycle doubles the backoff.
            service.run_cycle()
            self.assertGreaterEqual(service.next_delay(), 2 * mudah_alerts.BACKOFF_BASE_SECONDS)

            # A healthy cycle clears the backoff.
            recovering_store = SentStore(Path(tmp) / "alerts.db")
            recovering = AlertService(
                make_config(tmp),
                search=FakeSearch({"kw": [[fresh_item("1", clock)]]}),
                sender=sender,
                store=recovering_store,
                sleep=lambda _s: None,
                now=clock,
                jitter=lambda _span: 0.0,
            )
            recovering.run_cycle()
            self.assertEqual(recovering.next_delay(), 120.0)
            store.close()
            recovering_store.close()

    def test_fetch_failure_recovers_on_the_next_cycle(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            clock = FakeClock(NOW)
            flaky = FlakySearch(
                [mudah.MudahParseError("schema changed"), {"kw": [[fresh_item("7", clock)]]}]
            )
            sender = FakeSender()
            store = SentStore(Path(tmp) / "alerts.db")
            service = AlertService(
                make_config(tmp),
                search=flaky,
                sender=sender,
                store=store,
                sleep=lambda _s: None,
                now=clock,
            )
            first = service.run_cycle()
            self.assertEqual(first.errors.__len__(), 1)
            self.assertEqual(first.sent, [])
            second = service.run_cycle()
            self.assertEqual(second.sent, ["7"])
            store.close()

    def test_invalid_listing_url_is_skipped_without_state(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            clock = FakeClock(NOW)
            item = fresh_item("1", clock, url="https://evil.example/a")
            sender = FakeSender()
            db = Path(tmp) / "alerts.db"
            store = SentStore(db)
            service = AlertService(
                make_config(tmp),
                search=FakeSearch({"kw": [[item]]}),
                sender=sender,
                store=store,
                sleep=lambda _s: None,
                now=clock,
            )
            report = service.run_cycle()
            self.assertEqual(report.skipped_invalid_url, 1)
            self.assertEqual(sender.messages, [])
            store.close()
            with SentStore(db) as check:
                self.assertEqual(check.sent_ids(), set())


class FlakySearch:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)

    def __call__(self, **kwargs):
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        pages = outcome.get(kwargs.get("query"), [])
        page = int(kwargs.get("page", 1))
        items = pages[page - 1] if page - 1 < len(pages) else []
        return {
            "items": list(items),
            "pagination": {
                "has_next": page < len(pages),
                "organic_items_on_page": len(items),
            },
        }


class TelegramSenderTests(unittest.TestCase):
    class _Response:
        def __init__(self, payload: bytes):
            self.payload = payload

        def read(self, _n=-1):
            return self.payload

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def test_successful_send_returns_ok(self):
        sender = TelegramSender(
            "123:secret-token",
            "-100123",
            opener=lambda request, timeout=None: self._Response(b'{"ok": true}'),
        )
        self.assertTrue(sender.send("hello").ok)

    def test_rate_limit_surfaces_retry_after(self):
        body = json.dumps(
            {"ok": False, "parameters": {"retry_after": 12}}
        ).encode("utf-8")

        def opener(request, timeout=None):
            raise urllib.error.HTTPError(
                request.full_url,
                429,
                "Too Many Requests",
                {},  # type: ignore[arg-type]
                io.BytesIO(body),
            )

        sender = TelegramSender("token", "chat", opener=opener)
        result = sender.send("hello")
        self.assertFalse(result.ok)
        self.assertEqual(result.retry_after, 12.0)

    def test_token_is_redacted_from_errors(self):
        token = "123:super-secret"

        def opener(request, timeout=None):
            raise urllib.error.URLError(f"failed to reach {request.full_url}")

        sender = TelegramSender(token, "chat", opener=opener)
        result = sender.send("hello")
        self.assertFalse(result.ok)
        assert result.error is not None
        self.assertNotIn(token, result.error)
        self.assertIn("***", result.error)

    def test_non_ok_response_description_is_redacted(self):
        token = "123:super-secret"
        body = json.dumps(
            {"ok": False, "description": f"bad token {token}"}
        ).encode("utf-8")

        def opener(request, timeout=None):
            return self._Response(body)

        sender = TelegramSender(token, "chat", opener=opener)
        result = sender.send("hello")
        self.assertFalse(result.ok)
        assert result.error is not None
        self.assertNotIn(token, result.error)

    def test_env_loader_requires_both_values(self):
        with self.assertRaises(ConfigError):
            sender_from_env({})
        with self.assertRaises(ConfigError):
            sender_from_env({"TELEGRAM_BOT_TOKEN": "x"})
        sender = sender_from_env(
            {"TELEGRAM_BOT_TOKEN": "x", "TELEGRAM_CHAT_ID": "y"}
        )
        self.assertIsInstance(sender, TelegramSender)


class LoggingSecrecyTests(unittest.TestCase):
    def test_service_logs_never_contain_the_token(self):
        token = "123:should-not-appear"
        records: list[str] = []

        class Capture(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        logger = logging.getLogger("mudah_alerts.test.secrecy")
        logger.handlers = [Capture()]
        logger.setLevel(logging.DEBUG)
        logger.propagate = False

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            clock = FakeClock(NOW)

            def opener(request, timeout=None):
                # A realistic failure whose message embeds the API URL.
                raise urllib.error.URLError(f"unreachable: {request.full_url}")

            sender = TelegramSender(token, "chat", opener=opener)
            store = SentStore(Path(tmp) / "alerts.db")
            service = AlertService(
                make_config(tmp),
                search=FakeSearch({"kw": [[fresh_item("1", clock)]]}),
                sender=sender,
                store=store,
                sleep=lambda _s: None,
                now=clock,
                logger=logger,
            )
            service.run_cycle()
            store.close()

        joined = "\n".join(records)
        self.assertNotIn(token, joined)
        self.assertTrue(records)


class LockTests(unittest.TestCase):
    def test_second_lock_is_rejected_then_reacquired(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = Path(tmp) / "alerts.lock"
            first = SingleInstanceLock(path)
            second = SingleInstanceLock(path)
            first.acquire()
            try:
                with self.assertRaises(LockError):
                    second.acquire()
            finally:
                first.release()
            second.acquire()
            second.release()

    def test_lock_reports_readable_error(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = Path(tmp) / "alerts.lock"
            with SingleInstanceLock(path):
                other = SingleInstanceLock(path)
                with self.assertRaises(LockError) as ctx:
                    other.acquire()
            self.assertIn("another mudah-alerts process", str(ctx.exception))


class FreshnessCapTests(unittest.TestCase):
    def test_config_rejects_window_above_48_hours(self):
        with self.assertRaises(ConfigError):
            parse_config(
                {"keywords": ["a"], "freshness_hours": 49}, base_dir=Path(".")
            )
        config = parse_config(
            {"keywords": ["a"], "freshness_hours": 48}, base_dir=Path(".")
        )
        self.assertEqual(config.freshness_hours, 48.0)

    def test_runtime_hard_cap_ignores_larger_programmatic_window(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            clock = FakeClock(NOW)
            items = [
                make_item(
                    "recent", published_at=(NOW - timedelta(hours=47)).isoformat()
                ),
                make_item(
                    "aged", published_at=(NOW - timedelta(hours=50)).isoformat()
                ),
            ]
            sender = FakeSender()
            store = SentStore(Path(tmp) / "alerts.db")
            service = AlertService(
                make_config(tmp, freshness_hours=100.0),
                search=FakeSearch({"kw": [items]}),
                sender=sender,
                store=store,
                sleep=lambda _s: None,
                now=clock,
            )
            report = service.run_cycle()
            self.assertEqual(report.sent, ["recent"])
            self.assertEqual(report.skipped_stale, 1)
            store.close()


class BackoffStreakTests(unittest.TestCase):
    def test_failure_streak_grows_and_resets_on_a_healthy_cycle(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            clock = FakeClock(NOW)
            store = SentStore(Path(tmp) / "alerts.db")
            service = AlertService(
                make_config(tmp),
                search=FakeSearch(error=mudah.MudahBlockedError("blocked")),
                sender=FakeSender(),
                store=store,
                sleep=lambda _s: None,
                now=clock,
                jitter=lambda _span: 0.0,
            )
            service.run_cycle()
            self.assertGreaterEqual(
                service.next_delay(), mudah_alerts.BACKOFF_BASE_SECONDS
            )
            service.run_cycle()
            self.assertGreaterEqual(
                service.next_delay(), 2 * mudah_alerts.BACKOFF_BASE_SECONDS
            )
            # A healthy cycle on the same service clears the streak.
            service.search = FakeSearch({"kw": [[fresh_item("1", clock)]]})
            service.run_cycle()
            self.assertEqual(service.next_delay(), 120.0)
            store.close()


class ProviderHaltTests(unittest.TestCase):
    def _service(self, tmp, search, clock, keywords=("a", "b"), sleeps=None):
        store = SentStore(Path(tmp) / "alerts.db")
        service = AlertService(
            make_config(tmp, keywords=tuple(Keyword(k) for k in keywords)),
            search=search,
            sender=FakeSender(),
            store=store,
            sleep=(sleeps.append if sleeps is not None else (lambda _s: None)),
            now=clock,
            jitter=lambda _span: 0.0,
        )
        return service, store

    def test_block_stops_remaining_keywords(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            search = FakeSearch(error=mudah.MudahBlockedError("challenge"))
            service, store = self._service(tmp, search, FakeClock(NOW))
            report = service.run_cycle()
            self.assertEqual(len(search.calls), 1)
            self.assertEqual(report.keywords_searched, 0)
            self.assertEqual(len(report.errors), 1)
            store.close()

    def test_mudah_429_stops_and_honours_retry_after(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            err = urllib.error.HTTPError(
                "https://www.mudah.my/x",
                429,
                "Too Many Requests",
                {"Retry-After": "300"},  # type: ignore[arg-type]
                None,
            )
            service, store = self._service(tmp, FakeSearch(error=err), FakeClock(NOW))
            service.run_cycle()
            self.assertEqual(len(service.search.calls), 1)
            self.assertGreaterEqual(service.next_delay(), 300.0)
            store.close()

    def test_keyword_requests_are_paced(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            clock = FakeClock(NOW)
            sleeps: list[float] = []
            search = FakeSearch({"a": [[fresh_item("1", clock)]], "b": [[]]})
            service, store = self._service(tmp, search, clock, sleeps=sleeps)
            service.run_cycle()
            self.assertIn(2.0, sleeps)
            store.close()


class PageTimingTests(unittest.TestCase):
    def test_first_page_is_delivered_before_a_later_page_error(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            clock = FakeClock(NOW)
            page1 = {
                "items": [fresh_item("p1", clock)],
                "pagination": {"has_next": True, "organic_items_on_page": 40},
            }
            search = PagedSearch({1: page1, 2: ValueError("schema broke")})
            sender = FakeSender()
            store = SentStore(Path(tmp) / "alerts.db")
            service = AlertService(
                make_config(tmp, max_pages_per_keyword=2),
                search=search,
                sender=sender,
                store=store,
                sleep=lambda _s: None,
                now=clock,
            )
            report = service.run_cycle()
            self.assertEqual(report.sent, ["p1"])
            self.assertEqual(len(sender.messages), 1)
            self.assertEqual(len(report.errors), 1)
            self.assertEqual(search.calls, [1, 2])
            store.close()

    def test_freshness_is_rechecked_with_the_current_clock_before_send(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            t0 = NOW
            # First two clock reads are cycle-time; the send-time read is 2h later.
            clock = ScriptedClock(
                [t0, t0, t0 + timedelta(hours=2), t0 + timedelta(hours=2)]
            )
            item = make_item(
                "aged", published_at=(t0 - timedelta(hours=47)).isoformat(), price=900
            )
            sender = FakeSender()
            store = SentStore(Path(tmp) / "alerts.db")
            service = AlertService(
                make_config(tmp),
                search=FakeSearch({"kw": [[item]]}),
                sender=sender,
                store=store,
                sleep=lambda _s: None,
                now=clock,
            )
            report = service.run_cycle()
            self.assertEqual(report.sent, [])
            self.assertEqual(sender.messages, [])
            self.assertGreaterEqual(report.skipped_stale, 1)
            store.close()

    def test_malformed_timestamp_item_is_skipped_without_aborting(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            clock = FakeClock(NOW)
            bad = make_item("bad", published_at={"unexpected": "type"}, price=900)  # type: ignore[arg-type]
            good = fresh_item("good", clock)
            sender = FakeSender()
            store = SentStore(Path(tmp) / "alerts.db")
            service = AlertService(
                make_config(tmp),
                search=FakeSearch({"kw": [[bad, good]]}),
                sender=sender,
                store=store,
                sleep=lambda _s: None,
                now=clock,
            )
            report = service.run_cycle()
            self.assertEqual(report.sent, ["good"])
            self.assertEqual(report.skipped_invalid_timestamp, 1)
            store.close()


class Telegram429NoRetryAfterTests(unittest.TestCase):
    def test_default_cooldown_halts_sends(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            clock = FakeClock(NOW)
            items = [fresh_item(str(i), clock) for i in range(3)]
            sender = FakeSender(
                results=[
                    SendResult(
                        False,
                        "telegram rate limited (HTTP 429)",
                        None,
                        rate_limited=True,
                    )
                ]
            )
            store = SentStore(Path(tmp) / "alerts.db")
            service = AlertService(
                make_config(tmp, poll_interval_seconds=60.0),
                search=FakeSearch({"kw": [items]}),
                sender=sender,
                store=store,
                sleep=lambda _s: None,
                now=clock,
                jitter=lambda _span: 0.0,
            )
            report = service.run_cycle()
            self.assertEqual(len(sender.messages), 1)
            self.assertEqual(len(report.failed), 1)
            self.assertGreaterEqual(
                service.next_delay(), mudah_alerts.TELEGRAM_429_DEFAULT_COOLDOWN
            )
            store.close()

    def test_429_stops_later_pages(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            clock = FakeClock(NOW)
            page1 = {
                "items": [fresh_item("1", clock)],
                "pagination": {"has_next": True, "organic_items_on_page": 40},
            }
            search = PagedSearch(
                {
                    1: page1,
                    2: {
                        "items": [],
                        "pagination": {"has_next": False, "organic_items_on_page": 0},
                    },
                }
            )
            sender = FakeSender(
                results=[SendResult(False, "rate limited", None, rate_limited=True)]
            )
            store = SentStore(Path(tmp) / "alerts.db")
            service = AlertService(
                make_config(tmp, max_pages_per_keyword=2),
                search=search,
                sender=sender,
                store=store,
                sleep=lambda _s: None,
                now=clock,
            )
            report = service.run_cycle()
            self.assertEqual(search.calls, [1])
            self.assertEqual(report.pages_fetched, 1)
            store.close()


class TimestampStrictnessTests(unittest.TestCase):
    def test_invalid_clocks_are_rejected_not_defaulted(self):
        for text in ("today 25:99", "yesterday 24:00", "13pm", "25:99", "today 13:00pm"):
            with self.subTest(text=text):
                self.assertIsNone(parse_timestamp(text, now=NOW))

    def test_relative_requires_a_whole_string(self):
        self.assertIsNotNone(parse_timestamp("2 hours ago", now=NOW))
        for text in ("2 hours ago and more", "extra 2 hours ago", "in 2 hours"):
            with self.subTest(text=text):
                self.assertIsNone(parse_timestamp(text, now=NOW))

    def test_huge_relative_amount_is_safe(self):
        for text in (
            "999999999999999999 seconds ago",
            "9" * 40 + " days ago",
        ):
            with self.subTest(text=text[:24]):
                self.assertIsNone(parse_timestamp(text, now=NOW))
        self.assertIsNotNone(parse_timestamp("100 days ago", now=NOW))

    def test_month_first_comma_and_dotted_dates(self):
        expected = datetime(2025, 10, 9, 16, 0, tzinfo=UTC)  # 2025-10-10 00:00 MYT
        for text in (
            "Oct 10, 2025",
            "October 10 2025",
            "10.10.2025",
            "10/10/2025",
        ):
            with self.subTest(text=text):
                self.assertEqual(parse_timestamp(text, now=NOW), expected)

    def test_leap_day_without_a_year_does_not_crash(self):
        non_leap = datetime(2025, 6, 1, 0, 0, tzinfo=UTC)
        self.assertIsNone(parse_timestamp("29 Feb", now=non_leap))
        self.assertEqual(
            parse_timestamp("29 Feb 2024", now=datetime(2024, 3, 1, tzinfo=UTC)),
            datetime(2024, 2, 28, 16, 0, tzinfo=UTC),
        )


class UrlPriceTests(unittest.TestCase):
    def test_href_escapes_quote_characters(self):
        item = make_item("1", url='https://www.mudah.my/a"b.htm', price=900)
        message = build_message(item)
        assert message is not None
        self.assertIn('href="https://www.mudah.my/a&quot;b.htm"', message)

    def test_oversized_and_malformed_urls_are_skipped_safely(self):
        huge = "https://www.mudah.my/" + "a" * 3000
        for url in (huge, "https://", "https:///path", "https://[::1", "not a url", 12345):  # type: ignore[list-item]
            with self.subTest(url=str(url)[:32]):
                self.assertIsNone(build_message(make_item("1", url=url)))  # type: ignore[arg-type]

    def test_price_is_formatted_accurately(self):
        for price, label in ((1234.5, "RM 1,234.50/month"), (900, "RM 900/month"), (0, "RM 0/month")):
            with self.subTest(price=price):
                message = build_message(make_item("1", price=price))
                assert message is not None
                self.assertIn(label, message)

    def test_invalid_price_never_builds_a_message(self):
        for price in (None, -1, float("nan"), float("inf"), "900", True):
            with self.subTest(price=str(price)):
                self.assertIsNone(build_message(make_item("1", price=price)))

    def test_service_skips_invalid_price(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            clock = FakeClock(NOW)
            bad = make_item(
                "bad",
                published_at=fresh_item("x", clock)["published_at"],
                price=float("nan"),
            )
            sender = FakeSender()
            store = SentStore(Path(tmp) / "alerts.db")
            service = AlertService(
                make_config(tmp),
                search=FakeSearch({"kw": [[bad]]}),
                sender=sender,
                store=store,
                sleep=lambda _s: None,
                now=clock,
            )
            report = service.run_cycle()
            self.assertEqual(report.sent, [])
            self.assertEqual(report.skipped_missing_price, 1)
            self.assertEqual(sender.messages, [])
            store.close()


class KeywordValidationTests(unittest.TestCase):
    def test_non_string_query_is_rejected(self):
        for bad in (123, None, 1.5, ["a"], {"q": 1}):
            with self.subTest(bad=str(bad)), self.assertRaises(ConfigError):
                parse_config({"keywords": [{"query": bad}]}, base_dir=Path("."))


class LockDerivationTests(unittest.TestCase):
    def test_lock_path_is_derived_and_the_override_is_rejected(self):
        with self.assertRaises(ConfigError):
            parse_config(
                {"keywords": ["a"], "lock_path": "custom.lock"}, base_dir=Path(".")
            )
        config = AlertConfig(
            keywords=(Keyword("a"),), database_path=Path("state/alerts.db")
        )
        self.assertEqual(config.lock_path, Path("state/alerts.db.lock"))

    def test_lock_path_is_not_a_constructor_parameter(self):
        with self.assertRaises(TypeError):
            AlertConfig(
                keywords=(Keyword("a"),),
                database_path=Path("a.db"),
                lock_path=Path("x.lock"),  # type: ignore[call-arg]
            )


class DatabaseErrorTests(unittest.TestCase):
    def test_state_read_error_aborts_without_sending(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            clock = FakeClock(NOW)
            sender = FakeSender()
            service = AlertService(
                make_config(tmp),
                search=FakeSearch({"kw": [[fresh_item("1", clock)]]}),
                sender=sender,
                store=BrokenStore(),  # type: ignore[arg-type]
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

    def test_sent_store_does_not_swallow_read_errors(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            store = SentStore(Path(tmp) / "alerts.db")
            assert store._conn is not None
            store._conn.execute("DROP TABLE sent")
            store._conn.commit()
            with self.assertRaises(sqlite3.Error):
                store.sent_ids()
            store.close()


class SchemaExceptionTests(unittest.TestCase):
    def test_value_type_and_attribute_errors_recover_next_cycle(self):
        for exc in (
            ValueError("bad schema"),
            TypeError("bad type"),
            AttributeError("missing field"),
        ):
            with self.subTest(exc=type(exc).__name__), tempfile.TemporaryDirectory(
                ignore_cleanup_errors=True
            ) as tmp:
                clock = FakeClock(NOW)
                sender = FakeSender()
                store = SentStore(Path(tmp) / "alerts.db")
                service = AlertService(
                    make_config(tmp),
                    search=FlakySearch([exc, {"kw": [[fresh_item("7", clock)]]}]),
                    sender=sender,
                    store=store,
                    sleep=lambda _s: None,
                    now=clock,
                )
                first = service.run_cycle()
                self.assertEqual(first.sent, [])
                self.assertEqual(len(first.errors), 1)
                second = service.run_cycle()
                self.assertEqual(second.sent, ["7"])
                store.close()


if __name__ == "__main__":
    unittest.main()
