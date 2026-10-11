#!/usr/bin/env python3
"""Combined Telegram alert service for Mudah and iBilik searches.

The service reuses the sibling ``mudah-finder/mudah.py`` and
``ibilik-finder/ibilik.py`` clients. Adding those directories to ``sys.path`` at
import time avoids copying either scraper, and no change to the finders is
required. Mudah keyword searches and an ``ibilik`` block of saved searches run
in one process and share one Telegram sender. Each provider keeps its own SQLite
state, so an iBilik listing id can never collide with a Mudah one and a fetch
failure on one provider leaves the other polling.

Mudah's robots.txt says automated access needs permission. Set
``MUDAH_AUTOMATION_ALLOWED=1`` only after you are authorised. When it is not
``1`` the service logs a warning and skips the Mudah side, while an ``ibilik``
block keeps polling (iBilik is a public API). Telegram credentials are read from
``TELEGRAM_BOT_TOKEN`` and ``TELEGRAM_CHAT_ID``.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import random
import re
import signal
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Reuse the sibling finders instead of copying their scrapers.
_FINDER_DIR = Path(__file__).resolve().parent.parent / "mudah-finder"
if str(_FINDER_DIR) not in sys.path:
    sys.path.insert(0, str(_FINDER_DIR))
_IBILIK_FINDER_DIR = Path(__file__).resolve().parent.parent / "ibilik-finder"
if str(_IBILIK_FINDER_DIR) not in sys.path:
    sys.path.insert(0, str(_IBILIK_FINDER_DIR))

import ibilik  # type: ignore[import-not-found]  # noqa: E402  (import after sys.path setup)
import mudah  # type: ignore[import-not-found]  # noqa: E402  (import after sys.path setup)


__all__ = [
    "AlertConfig",
    "AlertService",
    "ConfigError",
    "CycleReport",
    "IbilikAlertService",
    "IbilikConfig",
    "IbilikCycleReport",
    "IbilikSearch",
    "IbilikSentStore",
    "Keyword",
    "LockError",
    "SentStore",
    "SendResult",
    "SingleInstanceLock",
    "TelegramSender",
    "build_ibilik_message",
    "build_message",
    "is_fresh",
    "listing_published_at",
    "load_config",
    "load_dotenv",
    "parse_timestamp",
    "sender_from_env",
]

MYT = timezone(timedelta(hours=8))

# One provider cycle per hour by default: room alerts are not urgent, and a
# gentler cadence is kinder to both sites. Config may still choose 60s..24h.
DEFAULT_POLL_SECONDS = 3600.0
MIN_POLL_SECONDS = 60.0
DEFAULT_FRESHNESS_HOURS = 48.0
# Mudah freshness is capped at the hard 48-hour rule in both config and runtime.
MAX_FRESHNESS_HOURS = 48.0
MAX_PAGES_PER_KEYWORD = 5
DEFAULT_PAGE_PACING_SECONDS = 2.0
MAX_PAGE_PACING_SECONDS = 60.0
DEFAULT_SEARCH_TIMEOUT = 30.0
MAX_SEARCH_TIMEOUT = 300.0

BACKOFF_BASE_SECONDS = 30.0
BACKOFF_CAP_SECONDS = 30.0 * 60.0
# Smallest wait the provider scheduler will request. Real poll intervals are at
# least MIN_POLL_SECONDS; this only keeps a zero-length delay from spinning.
MIN_SCHEDULE_WAIT = 0.05
TELEGRAM_API_ROOT = "https://api.telegram.org"
TELEGRAM_429_DEFAULT_COOLDOWN = 60.0
MESSAGE_HARD_LIMIT = 4096
MESSAGE_SOFT_LIMIT = 4000
MAX_LISTING_URL_LENGTH = 2048
_MAX_RELATIVE_DIGITS = 12
_MAX_RELATIVE_SECONDS = 100 * 365 * 86400

# iBilik has no published 48-hour rule, so its window is broader but still
# capped, and polling stays gentle: at most ten 100-item pages per search.
DEFAULT_IBILIK_FRESHNESS_HOURS = 48.0
MAX_IBILIK_FRESHNESS_HOURS = 24 * 7
DEFAULT_IBILIK_PAGES_PER_SEARCH = 3
MAX_IBILIK_PAGES_PER_SEARCH = 10
DEFAULT_IBILIK_LIMIT = 100
MAX_IBILIK_LIMIT = 100
MAX_IBILIK_AMENITIES = 20
_IBILIK_SEARCH_NAME_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")

DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS sent (
    listing_id     TEXT PRIMARY KEY,
    keyword        TEXT,
    url            TEXT,
    title          TEXT,
    price          REAL,
    published_at   TEXT,
    first_seen_at  TEXT NOT NULL,
    sent_at        TEXT,
    failure_count  INTEGER NOT NULL DEFAULT 0,
    last_error     TEXT,
    last_attempt_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_mudah_alerts_sent_at ON sent(sent_at);
"""

# iBilik state lives in its own tables in the same database file. The composite
# key (search_name, listing_id) keeps two saved searches and the Mudah table
# independent, so a UUID from iBilik can never shadow a Mudah row.
DB_IBILIK_SCHEMA = """
CREATE TABLE IF NOT EXISTS ibilik_sent (
    search_name    TEXT NOT NULL,
    listing_id     TEXT NOT NULL,
    keyword        TEXT,
    url            TEXT,
    title          TEXT,
    price          REAL,
    published_at   TEXT,
    first_seen_at  TEXT NOT NULL,
    seeded_at      TEXT,
    sent_at        TEXT,
    failure_count  INTEGER NOT NULL DEFAULT 0,
    last_error     TEXT,
    last_attempt_at TEXT,
    PRIMARY KEY (search_name, listing_id)
);
CREATE INDEX IF NOT EXISTS idx_ibilik_sent_at ON ibilik_sent(sent_at);
CREATE TABLE IF NOT EXISTS ibilik_searches (
    search_name    TEXT PRIMARY KEY,
    initialized_at TEXT
);
"""

_SECRET_KEYS = {
    "bot_token",
    "telegram_bot_token",
    "token",
    "chat_id",
    "telegram_chat_id",
    "api_token",
}
_UNIT_SECONDS = {
    "second": 1,
    "minute": 60,
    "hour": 3600,
    "day": 86400,
    "week": 7 * 86400,
    "month": 30 * 86400,
}
_NOW_WORDS = {
    "now",
    "just now",
    "moments ago",
    "a moment ago",
    "a few moments ago",
}
_RELATIVE_RE = re.compile(
    r"^(?P<num>\d+|a|an)\s*(?P<unit>second|minute|hour|day|week|month)s?\s*ago$",
    re.IGNORECASE,
)
_DATE_PREFIX_RE = re.compile(r"^(today|yesterday)\b[\s,]*", re.IGNORECASE)
# Colon is the only clock separator. A dot would collide with dotted dates
# such as 10.10.2025, which previously parsed as a time and broke that format.
_CLOCK_RE = re.compile(r"(\d{1,2}):(\d{2})(?:\s*([ap]\.?m\.?))?", re.IGNORECASE)
_CLOCK_HOUR_RE = re.compile(r"(\d{1,2})\s*([ap]\.?m\.?)", re.IGNORECASE)
_INVALID_CLOCK = (-1, -1)
_DATE_FORMATS_WITH_YEAR = (
    "%d %b %Y",
    "%d %B %Y",
    "%b %d %Y",
    "%B %d %Y",
    "%d/%m/%Y",
    "%d-%m-%Y",
    "%Y-%m-%d",
    "%d.%m.%Y",
)
_DATE_FORMATS_NO_YEAR = (
    "%d %b",
    "%d %B",
    "%b %d",
    "%B %d",
)


class AlertsError(Exception):
    """Base class for service failures."""


class ConfigError(AlertsError):
    """Raised when configuration is missing or invalid."""


class LockError(AlertsError):
    """Raised when another process holds the singleton lock."""


@dataclass(frozen=True)
class SendResult:
    """Outcome of one Telegram send attempt."""

    ok: bool
    error: str | None = None
    retry_after: float | None = None
    rate_limited: bool = False


# --------------------------------------------------------------------------
# Timestamp parsing and freshness
# --------------------------------------------------------------------------


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _from_epoch(number: float) -> datetime | None:
    try:
        return datetime.fromtimestamp(float(number), timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _parse_clock(text: str) -> tuple[int, int] | None:
    """Return ``(hour, minute)``, ``None`` when no clock is present, or the
    ``_INVALID_CLOCK`` sentinel when a clock-like token is present but invalid."""

    match = _CLOCK_RE.search(text)
    if match is not None:
        hour = int(match.group(1))
        minute = int(match.group(2))
        meridiem = match.group(3)
    else:
        match = _CLOCK_HOUR_RE.search(text)
        if match is None:
            return None
        hour = int(match.group(1))
        minute = 0
        meridiem = match.group(2)
    if meridiem:
        marker = meridiem.lower().replace(".", "")
        if marker == "pm" and 1 <= hour <= 11:
            hour += 12
        elif marker == "pm" and hour == 12:
            hour = 12
        elif marker == "am" and hour == 12:
            hour = 0
        elif hour > 12:
            return _INVALID_CLOCK
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return _INVALID_CLOCK
    return hour, minute


def _local_datetime(year, month, day, hour, minute, tz) -> datetime | None:
    try:
        return datetime(year, month, day, hour, minute, tzinfo=tz).astimezone(
            timezone.utc
        )
    except ValueError:
        return None


def _parse_explicit_date(text: str, now: datetime, tz: timezone) -> datetime | None:
    clock = _parse_clock(text)
    if clock == _INVALID_CLOCK:
        return None
    stripped = _CLOCK_RE.sub(" ", text)
    stripped = _CLOCK_HOUR_RE.sub(" ", stripped)
    stripped = stripped.replace(",", " ")
    stripped = re.sub(r"\b(at|on)\b", " ", stripped, flags=re.IGNORECASE)
    stripped = re.sub(r"\s+", " ", stripped).strip()
    local_now = _as_utc(now).astimezone(tz)

    if not stripped:
        if clock is None:
            return None
        hour, minute = clock
        return _local_datetime(
            local_now.year, local_now.month, local_now.day, hour, minute, tz
        )

    for fmt in _DATE_FORMATS_WITH_YEAR:
        try:
            parsed = datetime.strptime(stripped, fmt)
        except ValueError:
            continue
        local_dt = parsed.replace(tzinfo=tz)
        if clock is not None:
            local_dt = local_dt.replace(hour=clock[0], minute=clock[1])
        return local_dt.astimezone(timezone.utc)

    # No-year formats parse against a fixed placeholder year. Python 3.14 and
    # later warn on ambiguous year-less strptime input, so avoid it. Leap-day
    # inputs parsed against 2000 (a leap year) are rejected when the target
    # year has no 29 February.
    for fmt in _DATE_FORMATS_NO_YEAR:
        try:
            parsed = datetime.strptime(f"{stripped} 2000", f"{fmt} %Y")
        except ValueError:
            continue
        year = local_now.year
        if clock is not None:
            hour, minute = clock
        else:
            hour, minute = 0, 0
        candidate = _local_datetime(year, parsed.month, parsed.day, hour, minute, tz)
        if candidate is None:
            return None
        if candidate.astimezone(tz) > local_now:
            candidate = _local_datetime(
                year - 1, parsed.month, parsed.day, hour, minute, tz
            )
            if candidate is None:
                return None
        return candidate
    return None


def parse_timestamp(value, *, now: datetime | None = None, tz: timezone = MYT):
    """Turn a Mudah date field into an aware UTC datetime, or ``None``.

    Handles epoch numbers, ``just now``, ``2 hours ago``, ``today``/``yesterday``
    with an optional local (UTC+8) clock, explicit dates, and ISO strings.
    Relative and Malaysian wall-clock values resolve against ``now``.
    """

    if now is None:
        now = datetime.now(timezone.utc)
    moment = _as_utc(now)

    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        return _as_utc(value)
    if isinstance(value, (int, float)):
        return _from_epoch(value)
    if not isinstance(value, str):
        return None

    text = value.strip()
    if not text:
        return None
    lowered = text.lower()

    if lowered in _NOW_WORDS:
        return moment

    relative = _RELATIVE_RE.fullmatch(lowered)
    if relative is not None:
        raw_num = relative.group("num").lower()
        if raw_num in ("a", "an"):
            amount = 1
        elif len(raw_num) > _MAX_RELATIVE_DIGITS:
            return None
        else:
            amount = int(raw_num)
        seconds = _UNIT_SECONDS[relative.group("unit").lower()] * amount
        if seconds > _MAX_RELATIVE_SECONDS:
            return None
        try:
            return moment - timedelta(seconds=seconds)
        except OverflowError:
            return None

    prefix = _DATE_PREFIX_RE.match(text)
    if prefix is not None:
        clock = _parse_clock(text)
        if clock == _INVALID_CLOCK:
            return None
        offset = 0 if prefix.group(1).lower() == "today" else -1
        local_now = moment.astimezone(tz)
        day = (local_now + timedelta(days=offset)).date()
        hour, minute = clock if clock is not None else (0, 0)
        return _local_datetime(day.year, day.month, day.day, hour, minute, tz)

    iso_text = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
    if not text.isdigit():
        try:
            parsed = datetime.fromisoformat(iso_text)
        except (ValueError, OverflowError):
            parsed = None
        if parsed is not None:
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=tz)
            return parsed.astimezone(timezone.utc)

    return _parse_explicit_date(text, moment, tz)


def listing_published_at(item: dict, *, now: datetime | None = None):
    """Prefer ``published_at``; fall back to ``site_date``. Never ``modified_at``.

    A malformed field on one listing yields ``None`` instead of raising, so a
    single bad ad cannot abort the cycle.
    """

    for key in ("published_at", "site_date"):
        try:
            parsed = parse_timestamp(item.get(key), now=now)
        except (ValueError, TypeError, OverflowError, AttributeError, OSError):
            parsed = None
        if parsed is not None:
            return parsed
    return None


def is_fresh(published, *, now: datetime, freshness_hours: float) -> bool:
    """True when ``published`` is in the past and no older than the window."""

    if not isinstance(published, datetime):
        return False
    moment = _as_utc(now)
    published_utc = _as_utc(published)
    if published_utc > moment:
        return False
    age = (moment - published_utc).total_seconds()
    return age <= freshness_hours * 3600


# --------------------------------------------------------------------------
# Message formatting
# --------------------------------------------------------------------------


def _escape_html(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#x27;")
    )


def _validated_listing_url(url) -> str | None:
    if not isinstance(url, str):
        return None
    candidate = url.strip()
    if not candidate or len(candidate) > MAX_LISTING_URL_LENGTH:
        return None
    try:
        parsed = urllib.parse.urlparse(candidate)
        host = parsed.hostname
    except ValueError:
        return None
    if parsed.scheme != "https" or not host:
        return None
    host = host.lower()
    if host != "mudah.my" and not host.endswith(".mudah.my"):
        return None
    return candidate


def _numeric_price(price) -> float | None:
    """Return a finite, non-negative rent as a float, or ``None`` otherwise."""

    if isinstance(price, bool) or not isinstance(price, (int, float)):
        return None
    if not math.isfinite(price) or price < 0:
        return None
    return float(price)


def _format_price(price: float) -> str:
    if float(price).is_integer():
        return f"RM {int(price):,}"
    return f"RM {price:,.2f}"


def _format_my_time(published: datetime) -> str:
    """Absolute posting time in Malaysia time, e.g. ``12 Oct 2026, 3:45 PM``."""

    local = _as_utc(published).astimezone(MYT)
    hour = local.hour % 12 or 12
    meridiem = "AM" if local.hour < 12 else "PM"
    return f"{local.day} {local.strftime('%b %Y')}, {hour}:{local.minute:02d} {meridiem}"


def _format_posted(published: datetime, now: datetime | None) -> str:
    """Absolute (MYT) posting time plus a relative age, e.g.
    ``12 Oct 2026, 3:45 PM (2 h ago)``.

    The absolute half is always present so the reader can see when a listing was
    posted even when the relative window has aged out; the parenthesised half
    keeps the at-a-glance freshness that the old format had.
    """

    if now is None:
        now = datetime.now(timezone.utc)
    moment = _as_utc(now)
    delta = (moment - _as_utc(published)).total_seconds()
    if delta < 60:
        relative = "just now"
    elif delta < 3600:
        relative = f"{max(1, int(delta // 60))} min ago"
    elif delta < 86400:
        relative = f"{int(delta // 3600)} h ago"
    else:
        relative = f"{int(delta // 86400)} d ago"
    return f"{_format_my_time(published)} ({relative})"


def _fit_title(raw: str, reserved: int, limit: int) -> str:
    budget = limit - reserved
    if budget < 2:
        return "\u2026"
    escaped = _escape_html(raw)
    if len(escaped) <= budget:
        return escaped
    trimmed = raw
    while trimmed and len(_escape_html(trimmed)) + 1 > budget:
        trimmed = trimmed[:-1]
    return _escape_html(trimmed.rstrip()) + "\u2026"


def build_message(
    item: dict,
    *,
    published_at: datetime | None = None,
    now: datetime | None = None,
    limit: int = MESSAGE_SOFT_LIMIT,
) -> str | None:
    """Build a clickable Telegram HTML message.

    Returns ``None`` when the listing has no usable Mudah URL or no valid
    asking price, so a malformed ad is skipped rather than sent with missing
    details. The result is always built within ``limit`` and never sliced
    mid-markup.
    """

    url = _validated_listing_url(item.get("url"))
    if url is None:
        return None

    price = _numeric_price(item.get("price"))
    if price is None:
        return None

    raw_title = str(item.get("title") or "Mudah listing").strip() or "Mudah listing"
    tail = [f"Mudah · {_format_price(price)}/month"]
    if isinstance(published_at, datetime):
        tail.append(f"Posted {_format_posted(published_at, now)}")
    link = f'<a href="{_escape_html(url)}">Open on Mudah</a>'

    reserved = sum(len(line) + 1 for line in tail) + len(link) + len("<b></b>") + 2
    if reserved >= limit:
        return None
    title_html = _fit_title(raw_title, reserved, limit)
    text = "\n".join([f"<b>{title_html}</b>", *tail, link])
    if len(text) > MESSAGE_HARD_LIMIT:
        return None
    return text


def _validated_ibilik_url(url) -> str | None:
    if not isinstance(url, str):
        return None
    candidate = url.strip()
    if not candidate or len(candidate) > MAX_LISTING_URL_LENGTH:
        return None
    try:
        parsed = urllib.parse.urlparse(candidate)
        host = parsed.hostname
    except ValueError:
        return None
    if parsed.scheme != "https" or not host:
        return None
    host = host.lower()
    if host != "ibilik.com" and not host.endswith(".ibilik.com"):
        return None
    return candidate


def _ibilik_location(item: dict) -> str:
    parts = [item.get("state"), item.get("city"), item.get("area")]
    return " / ".join(str(part) for part in parts if part)


def _ibilik_price_unit(charge_unit) -> str:
    if charge_unit == "PER_MONTH":
        return "/month"
    if charge_unit == "PER_NIGHT":
        return "/night"
    return ""


def build_ibilik_message(
    item: dict,
    *,
    published_at: datetime | None = None,
    now: datetime | None = None,
    limit: int = MESSAGE_SOFT_LIMIT,
) -> str | None:
    """Build a clickable Telegram HTML message for one iBilik listing.

    The body names the provider, then the title, rent, location, and a relative
    posted time when the listing has one. Contact numbers and WhatsApp flags are
    deliberately dropped: they belong to real people and do not belong in an
    alert. Returns ``None`` for a listing without an iBilik URL or a usable
    price, so a malformed ad is skipped rather than sent half-empty.
    """

    url = _validated_ibilik_url(item.get("url"))
    if url is None:
        return None

    price = _numeric_price(item.get("price"))
    if price is None:
        return None

    raw_title = str(item.get("title") or "iBilik listing").strip() or "iBilik listing"
    tail = [
        f"iBilik · {_format_price(price)}{_ibilik_price_unit(item.get('charge_unit'))}"
    ]
    location = _ibilik_location(item)
    if location:
        tail.append(_escape_html(location))
    if isinstance(published_at, datetime):
        tail.append(f"Posted {_format_posted(published_at, now)}")
    link = f'<a href="{_escape_html(url)}">Open on iBilik</a>'

    reserved = sum(len(line) + 1 for line in tail) + len(link) + len("<b></b>") + 2
    if reserved >= limit:
        return None
    title_html = _fit_title(raw_title, reserved, limit)
    text = "\n".join([f"<b>{title_html}</b>", *tail, link])
    if len(text) > MESSAGE_HARD_LIMIT:
        return None
    return text


# --------------------------------------------------------------------------
# Telegram sending
# --------------------------------------------------------------------------


class TelegramSender:
    """Minimal Telegram Bot API client using only the standard library."""

    def __init__(
        self,
        token: str,
        chat_id: str,
        *,
        timeout: float = 15.0,
        opener=urllib.request.urlopen,
    ):
        if not token or not chat_id:
            raise ConfigError("Telegram token and chat id are required")
        self._token = token
        self._chat_id = chat_id
        self._timeout = float(timeout)
        self._opener = opener

    def _redact(self, message: str) -> str:
        return message.replace(self._token, "***")

    def send(self, text: str) -> SendResult:
        endpoint = f"{TELEGRAM_API_ROOT}/bot{self._token}/sendMessage"
        payload = urllib.parse.urlencode(
            {
                "chat_id": self._chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": "true",
            }
        ).encode("utf-8")
        request = urllib.request.Request(endpoint, data=payload, method="POST")
        try:
            with self._opener(request, timeout=self._timeout) as response:
                body = response.read(1_000_000)
        except urllib.error.HTTPError as err:
            return self._http_error(err)
        except (urllib.error.URLError, TimeoutError, OSError) as err:
            return SendResult(
                False, f"telegram request failed: {self._redact(str(err))}"
            )

        try:
            decoded = json.loads(body.decode("utf-8", errors="replace"))
        except (ValueError, UnicodeError):
            return SendResult(False, "telegram response was not readable JSON")
        if isinstance(decoded, dict) and decoded.get("ok") is True:
            return SendResult(True)
        description = decoded.get("description") if isinstance(decoded, dict) else None
        return SendResult(
            False, f"telegram rejected the message: {self._redact(str(description))}"
        )

    def _http_error(self, err: urllib.error.HTTPError) -> SendResult:
        retry_after: float | None = None
        description: str | None = None
        try:
            raw = err.read()
        except (OSError, ValueError):
            raw = b""
        finally:
            try:
                err.close()
            except OSError:
                pass
        try:
            payload = json.loads(raw.decode("utf-8", errors="replace"))
        except (ValueError, UnicodeError):
            payload = None
        if isinstance(payload, dict):
            description = payload.get("description")
            parameters = payload.get("parameters")
            if isinstance(parameters, dict):
                value = parameters.get("retry_after")
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    retry_after = max(0.0, float(value))
        if err.code == 429:
            return SendResult(
                False,
                "telegram rate limited (HTTP 429)",
                retry_after,
                rate_limited=True,
            )
        detail = description or err.reason or "unknown error"
        return SendResult(
            False, f"telegram HTTP {err.code}: {self._redact(str(detail))}", retry_after
        )


def sender_from_env(env=None, *, timeout: float = 15.0) -> TelegramSender:
    source = os.environ if env is None else env
    token = (source.get("TELEGRAM_BOT_TOKEN") or "").strip()
    chat_id = (source.get("TELEGRAM_CHAT_ID") or "").strip()
    if not token or not chat_id:
        raise ConfigError(
            "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set in the "
            "environment; they never belong in the config file"
        )
    return TelegramSender(token, chat_id, timeout=timeout)


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Keyword:
    """One saved search keyword and an optional per-keyword rent ceiling."""

    query: str
    max_price: int | None = None


@dataclass(frozen=True)
class IbilikSearch:
    """One saved iBilik search.

    ``name`` namespaces its dedupe state, so two searches never share rows even
    when they return the same listing. The remaining fields map straight onto
    the documented iBilik filter parameters; location values are iBilik codes
    such as ``cheras``, ``selangor`` or a property code like ``the-troika``,
    not display names.
    """

    name: str
    query: str | None = None
    state: str | None = None
    city: str | None = None
    area: str | None = None
    property: str | None = None
    min_price: int | None = None
    max_price: int | None = None
    room_type: str | None = None
    bathroom_type: str | None = None
    unit_type: str | None = None
    floor_level: str | None = None
    min_sqft: int | None = None
    max_sqft: int | None = None
    kyc_verified: bool | None = None
    amenities: tuple[str, ...] = ()

    def as_filters(self) -> dict:
        return {
            "query": self.query,
            "state": self.state,
            "city": self.city,
            "area": self.area,
            "property": self.property,
            "min_price": self.min_price,
            "max_price": self.max_price,
            "room_type": self.room_type,
            "bathroom_type": self.bathroom_type,
            "unit_type": self.unit_type,
            "floor_level": self.floor_level,
            "min_sqft": self.min_sqft,
            "max_sqft": self.max_sqft,
            "kyc_verified": self.kyc_verified,
            "amenities": list(self.amenities),
        }


@dataclass(frozen=True)
class IbilikConfig:
    """Validated opt-in iBilik polling settings."""

    searches: tuple[IbilikSearch, ...]
    poll_interval_seconds: float = DEFAULT_POLL_SECONDS
    freshness_hours: float = DEFAULT_IBILIK_FRESHNESS_HOURS
    max_pages_per_search: int = DEFAULT_IBILIK_PAGES_PER_SEARCH
    limit_per_page: int = DEFAULT_IBILIK_LIMIT
    page_pacing_seconds: float = DEFAULT_PAGE_PACING_SECONDS
    search_timeout_seconds: float = DEFAULT_SEARCH_TIMEOUT
    initial_seed: bool = True


@dataclass(frozen=True)
class AlertConfig:
    """Validated service configuration.

    The lock path is always derived from ``database_path`` so a config cannot
    point two processes at one database with different locks and bypass the
    singleton guard.
    """

    keywords: tuple[Keyword, ...]
    database_path: Path
    poll_interval_seconds: float = DEFAULT_POLL_SECONDS
    freshness_hours: float = DEFAULT_FRESHNESS_HOURS
    location: str = "malaysia"
    rental_type: str = "room"
    max_price: int | None = None
    max_pages_per_keyword: int = 2
    page_pacing_seconds: float = DEFAULT_PAGE_PACING_SECONDS
    search_timeout_seconds: float = DEFAULT_SEARCH_TIMEOUT
    ibilik: IbilikConfig | None = None

    @property
    def lock_path(self) -> Path:
        return Path(str(self.database_path) + ".lock")


def _check_no_secrets(node, path: str = "config") -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            if str(key).lower() in _SECRET_KEYS:
                raise ConfigError(
                    f"{path}.{key}: Telegram credentials must come from "
                    "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID, never the config file"
                )
            _check_no_secrets(value, f"{path}.{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            _check_no_secrets(value, f"{path}[{index}]")


def _finite_number(name: str, value, minimum: float, maximum: float) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{name} must be a number")
    number = float(value)
    if not math.isfinite(number):
        raise ConfigError(f"{name} must be a finite number")
    if number < minimum or number > maximum:
        raise ConfigError(f"{name} must be between {minimum:g} and {maximum:g}")
    return number


def _whole_number(name: str, value, minimum: int, maximum: int) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{name} must be a whole number")
    if value < minimum or value > maximum:
        raise ConfigError(f"{name} must be between {minimum} and {maximum}")
    return value


def _required_number(name: str, value, minimum: float, maximum: float) -> float:
    checked = _finite_number(name, value, minimum, maximum)
    if checked is None:
        raise ConfigError(f"{name} is required")
    return checked


def _required_int(name: str, value, minimum: int, maximum: int) -> int:
    checked = _whole_number(name, value, minimum, maximum)
    if checked is None:
        raise ConfigError(f"{name} is required")
    return checked


def _parse_keywords(raw) -> tuple[Keyword, ...]:
    if not isinstance(raw, list) or not raw:
        raise ConfigError("keywords must be a non-empty list")
    keywords: list[Keyword] = []
    for index, entry in enumerate(raw, start=1):
        if isinstance(entry, str):
            query = entry.strip()
            max_price = None
        elif isinstance(entry, dict):
            unknown = set(entry) - {"query", "max_price"}
            if unknown:
                raise ConfigError(
                    f"keywords[{index}] has unknown keys: {', '.join(sorted(unknown))}"
                )
            raw_query = entry.get("query")
            if not isinstance(raw_query, str):
                raise ConfigError(f"keywords[{index}].query must be a string")
            query = raw_query.strip()
            max_price = _whole_number(
                f"keywords[{index}].max_price",
                entry.get("max_price"),
                0,
                10_000_000,
            )
        else:
            raise ConfigError(
                f"keywords[{index}] must be a string or an object with a query"
            )
        if not query:
            raise ConfigError(f"keywords[{index}] needs a non-empty query")
        if len(query) > 100:
            raise ConfigError(f"keywords[{index}] query must be at most 100 characters")
        keywords.append(Keyword(query=query, max_price=max_price))
    return tuple(keywords)


_IBILIK_SEARCH_KEYS = {
    "name",
    "query",
    "state",
    "city",
    "area",
    "property",
    "url",
    "min_price",
    "max_price",
    "room_type",
    "bathroom_type",
    "unit_type",
    "floor_level",
    "min_sqft",
    "max_sqft",
    "kyc_verified",
    "amenities",
}


def _optional_text(name: str, value, maximum: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConfigError(f"{name} must be a string")
    text = value.strip()
    if not text:
        return None
    if len(text) > maximum:
        raise ConfigError(f"{name} must be at most {maximum} characters")
    return text


def _location_code(name: str, value) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not re.fullmatch(
        r"[a-z0-9]+(?:-[a-z0-9]+)*", value.strip().lower()
    ):
        raise ConfigError(
            f"{name} must be a lowercase iBilik location code such as 'cheras'"
        )
    return value.strip().lower()


def _choice(name: str, value, allowed) -> str | None:
    if value is None:
        return None
    if value not in allowed:
        raise ConfigError(f"{name} must be one of: " + ", ".join(sorted(allowed)))
    return value


def _parse_ibilik_amenities(name: str, value) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ConfigError(f"{name} must be a list of amenity codes")
    if len(value) > MAX_IBILIK_AMENITIES:
        raise ConfigError(f"{name} accepts at most {MAX_IBILIK_AMENITIES} codes")
    seen: set[str] = set()
    cleaned: list[str] = []
    for code in value:
        if not isinstance(code, str):
            raise ConfigError(f"{name} must be a list of strings")
        code = code.strip()
        if code and code not in seen:
            seen.add(code)
            cleaned.append(code)
    return tuple(cleaned)


_IBILIK_LOCATION_KEYS = {"query", "state", "city", "area", "property"}


def _parse_ibilik_search_url(name: str, value) -> dict:
    """Turn an ibilik.com search URL into an iBilik filter mapping.

    The website encodes filters in the query string: ``?property=``, ``?city=``,
    ``?area=`` and ``?state=`` map to those filters, and a free-text ``?search=``
    or ``?q=`` becomes the ``query`` filter. Only an ibilik.com host is
    accepted, so a pasted URL cannot redirect the fetch somewhere else.
    """

    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{name} must be a non-empty iBilik search URL")
    parts = urllib.parse.urlsplit(value.strip())
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("http", "https") or not (
        host == "ibilik.com" or host.endswith(".ibilik.com")
    ):
        raise ConfigError(f"{name} must be an ibilik.com URL")
    params = urllib.parse.parse_qs(parts.query, keep_blank_values=False)
    filters: dict = {}
    for key in ("property", "city", "area", "state"):
        if key in params and params[key][0].strip():
            filters[key] = params[key][0].strip()
    for key in ("search", "q"):
        if key in params and params[key][0].strip():
            filters["query"] = params[key][0].strip()
            break
    if not filters:
        raise ConfigError(
            f"{name} has no supported filter "
            "(property, city, area, state, or search)"
        )
    return filters


def _parse_ibilik_searches(raw) -> tuple[IbilikSearch, ...]:
    if not isinstance(raw, list) or not raw:
        raise ConfigError("ibilik.searches must be a non-empty list")
    searches: list[IbilikSearch] = []
    names: set[str] = set()
    for index, entry in enumerate(raw, start=1):
        prefix = f"ibilik.searches[{index}]"
        if not isinstance(entry, dict):
            raise ConfigError(f"{prefix} must be an object")
        unknown = set(entry) - _IBILIK_SEARCH_KEYS
        if unknown:
            raise ConfigError(f"{prefix} has unknown keys: {', '.join(sorted(unknown))}")
        name = entry.get("name")
        if not isinstance(name, str) or not _IBILIK_SEARCH_NAME_RE.fullmatch(name.strip()):
            raise ConfigError(f"{prefix}.name must match [A-Za-z0-9_-]{{1,64}}")
        name = name.strip()
        if name in names:
            raise ConfigError(f"{prefix}.name duplicates {name!r}")
        names.add(name)

        url_value = entry.get("url")
        if url_value is not None:
            conflicting = sorted(set(entry) & _IBILIK_LOCATION_KEYS)
            if conflicting:
                raise ConfigError(
                    f"{prefix}.url cannot be combined with "
                    + ", ".join(conflicting)
                )
            url_filters = _parse_ibilik_search_url(f"{prefix}.url", url_value)
        else:
            url_filters = {}

        def _loc(key):
            return url_filters.get(key) if url_value is not None else entry.get(key)

        min_price = _whole_number(f"{prefix}.min_price", entry.get("min_price"), 0, 10_000_000)
        max_price = _whole_number(f"{prefix}.max_price", entry.get("max_price"), 0, 10_000_000)
        if min_price is not None and max_price is not None and min_price > max_price:
            raise ConfigError(f"{prefix}.min_price must be <= max_price")

        kyc = entry.get("kyc_verified")
        if kyc is not None and not isinstance(kyc, bool):
            raise ConfigError(f"{prefix}.kyc_verified must be true or false")

        searches.append(
            IbilikSearch(
                name=name,
                query=_optional_text(f"{prefix}.query", _loc("query"), 100),
                state=_location_code(f"{prefix}.state", _loc("state")),
                city=_location_code(f"{prefix}.city", _loc("city")),
                area=_location_code(f"{prefix}.area", _loc("area")),
                property=_location_code(f"{prefix}.property", _loc("property")),
                min_price=min_price,
                max_price=max_price,
                room_type=_choice(f"{prefix}.room_type", entry.get("room_type"), ibilik.ROOM_TYPES),
                bathroom_type=_choice(
                    f"{prefix}.bathroom_type",
                    entry.get("bathroom_type"),
                    ibilik.BATHROOM_TYPES,
                ),
                unit_type=_choice(f"{prefix}.unit_type", entry.get("unit_type"), ibilik.UNIT_TYPES),
                floor_level=_choice(
                    f"{prefix}.floor_level", entry.get("floor_level"), ibilik.FLOOR_LEVELS
                ),
                min_sqft=_whole_number(f"{prefix}.min_sqft", entry.get("min_sqft"), 0, 10_000_000),
                max_sqft=_whole_number(f"{prefix}.max_sqft", entry.get("max_sqft"), 0, 10_000_000),
                kyc_verified=kyc,
                amenities=_parse_ibilik_amenities(f"{prefix}.amenities", entry.get("amenities")),
            )
        )
    return tuple(searches)


def _parse_ibilik(raw_ibilik, *, default_poll: float) -> IbilikConfig | None:
    if raw_ibilik is None:
        return None
    if not isinstance(raw_ibilik, dict):
        raise ConfigError("ibilik must be a JSON object")
    allowed = {
        "enabled",
        "searches",
        "poll_interval_seconds",
        "freshness_hours",
        "max_pages_per_search",
        "limit_per_page",
        "page_pacing_seconds",
        "search_timeout_seconds",
        "initial_seed",
    }
    unknown = set(raw_ibilik) - allowed
    if unknown:
        raise ConfigError(f"ibilik has unknown keys: {', '.join(sorted(unknown))}")
    enabled = raw_ibilik.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ConfigError("ibilik.enabled must be true or false")
    if not enabled:
        return None

    searches = _parse_ibilik_searches(raw_ibilik.get("searches"))
    poll = _required_number(
        "ibilik.poll_interval_seconds",
        raw_ibilik.get("poll_interval_seconds", default_poll),
        MIN_POLL_SECONDS,
        24 * 3600.0,
    )
    freshness = _required_number(
        "ibilik.freshness_hours",
        raw_ibilik.get("freshness_hours", DEFAULT_IBILIK_FRESHNESS_HOURS),
        1.0,
        MAX_IBILIK_FRESHNESS_HOURS,
    )
    max_pages = _required_int(
        "ibilik.max_pages_per_search",
        raw_ibilik.get("max_pages_per_search", DEFAULT_IBILIK_PAGES_PER_SEARCH),
        1,
        MAX_IBILIK_PAGES_PER_SEARCH,
    )
    limit = _required_int(
        "ibilik.limit_per_page",
        raw_ibilik.get("limit_per_page", DEFAULT_IBILIK_LIMIT),
        1,
        MAX_IBILIK_LIMIT,
    )
    pacing = _required_number(
        "ibilik.page_pacing_seconds",
        raw_ibilik.get("page_pacing_seconds", DEFAULT_PAGE_PACING_SECONDS),
        0.0,
        MAX_PAGE_PACING_SECONDS,
    )
    timeout = _required_number(
        "ibilik.search_timeout_seconds",
        raw_ibilik.get("search_timeout_seconds", DEFAULT_SEARCH_TIMEOUT),
        1.0,
        MAX_SEARCH_TIMEOUT,
    )
    initial_seed = raw_ibilik.get("initial_seed", True)
    if not isinstance(initial_seed, bool):
        raise ConfigError("ibilik.initial_seed must be true or false")

    return IbilikConfig(
        searches=searches,
        poll_interval_seconds=poll,
        freshness_hours=freshness,
        max_pages_per_search=max_pages,
        limit_per_page=limit,
        page_pacing_seconds=pacing,
        search_timeout_seconds=timeout,
        initial_seed=initial_seed,
    )


def parse_config(raw: dict, *, base_dir: Path) -> AlertConfig:
    if not isinstance(raw, dict):
        raise ConfigError("config root must be a JSON object")
    _check_no_secrets(raw)
    if "lock_path" in raw:
        raise ConfigError(
            "lock_path is derived from database_path and cannot be overridden; "
            "remove it so one database always maps to one singleton lock"
        )

    # Keywords stay optional only when an iBilik block carries the work. An
    # explicit empty list is still rejected, matching the old contract.
    raw_keywords = raw.get("keywords")
    keywords = () if raw_keywords is None else _parse_keywords(raw_keywords)

    poll = _required_number(
        "poll_interval_seconds",
        raw.get("poll_interval_seconds", DEFAULT_POLL_SECONDS),
        MIN_POLL_SECONDS,
        24 * 3600.0,
    )
    freshness = _required_number(
        "freshness_hours",
        raw.get("freshness_hours", DEFAULT_FRESHNESS_HOURS),
        1.0,
        MAX_FRESHNESS_HOURS,
    )

    location = raw.get("location", "malaysia")
    if not isinstance(location, str) or not re.fullmatch(
        r"[a-z0-9]+(?:-[a-z0-9]+)*", location.strip().lower()
    ):
        raise ConfigError("location must be a lowercase Mudah slug such as 'malaysia'")
    location = location.strip().lower()

    rental_type = raw.get("rental_type", "room")
    if rental_type not in mudah.CATEGORY_PATHS:
        raise ConfigError(
            "rental_type must be one of: " + ", ".join(mudah.CATEGORY_PATHS)
        )

    max_price = _whole_number("max_price", raw.get("max_price"), 0, 10_000_000)
    max_pages = _required_int(
        "max_pages_per_keyword",
        raw.get("max_pages_per_keyword", 2),
        1,
        MAX_PAGES_PER_KEYWORD,
    )
    pacing = _required_number(
        "page_pacing_seconds",
        raw.get("page_pacing_seconds", DEFAULT_PAGE_PACING_SECONDS),
        0.0,
        MAX_PAGE_PACING_SECONDS,
    )
    timeout = _required_number(
        "search_timeout_seconds",
        raw.get("search_timeout_seconds", DEFAULT_SEARCH_TIMEOUT),
        1.0,
        MAX_SEARCH_TIMEOUT,
    )

    base_dir = Path(base_dir).resolve()
    database_path = _resolve_path(raw.get("database_path"), base_dir, "mudah_alerts.db")

    ibilik_config = _parse_ibilik(raw.get("ibilik"), default_poll=poll)
    if not keywords and ibilik_config is None:
        raise ConfigError(
            "config must define Mudah keywords and/or an enabled ibilik block"
        )

    return AlertConfig(
        keywords=keywords,
        database_path=database_path,
        poll_interval_seconds=poll,
        freshness_hours=freshness,
        location=location,
        rental_type=rental_type,
        max_price=max_price,
        max_pages_per_keyword=max_pages,
        page_pacing_seconds=pacing,
        search_timeout_seconds=timeout,
        ibilik=ibilik_config,
    )


def _resolve_path(value, base_dir: Path, default_name: str) -> Path:
    if value is None:
        return base_dir / default_name
    if not isinstance(value, str) or not value.strip():
        raise ConfigError("path values must be non-empty strings")
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = base_dir / candidate
    return candidate.resolve()


def load_config(path=None) -> AlertConfig:
    """Load and validate a JSON config. Relative paths resolve from the file."""

    if path is None:
        source = Path(__file__).resolve().parent / "config.json"
    else:
        source = Path(path).expanduser()
        if not source.is_absolute():
            source = Path.cwd() / source
    source = source.resolve()
    if not source.is_file():
        raise ConfigError(f"config file not found: {source}")
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except json.JSONDecodeError as err:
        raise ConfigError(f"invalid JSON in {source}: {err}") from err
    except OSError as err:
        raise ConfigError(f"could not read {source}: {err}") from err
    return parse_config(raw, base_dir=source.parent)


def load_dotenv(path, *, environ=None) -> list[str]:
    """Load simple ``KEY=VALUE`` pairs from a ``.env`` file, stdlib only.

    Real environment variables always win: a key already present in the target
    mapping is left untouched, and ``.env`` only fills what is unset. Only the
    first ``=`` splits a line, so values may contain ``=``. Blank lines and
    ``#`` comments are ignored, a leading ``export `` is tolerated, and a value
    wrapped in matching single or double quotes is unwrapped. There is no
    interpolation and no third-party dependency.

    Returns the list of key *names* that were set, never their values, so callers
    can report progress without disclosing secrets. A missing file is a no-op.
    """

    target = os.environ if environ is None else environ
    loaded: list[str] = []
    source = Path(path)
    if not source.is_file():
        return loaded
    try:
        text = source.read_text(encoding="utf-8")
    except OSError:
        return loaded
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key in target:
            continue
        target[key] = value
        loaded.append(key)
    return loaded


# --------------------------------------------------------------------------
# Persistent dedupe store
# --------------------------------------------------------------------------


class SentStore:
    """SQLite-backed record of sent listings and failed send attempts."""

    def __init__(self, path, *, read_only: bool = False):
        self.path = Path(path)
        self.read_only = read_only
        self._conn: sqlite3.Connection | None = None
        if read_only:
            if self.path.exists():
                target = self.path.resolve().as_posix()
                self._conn = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(self.path))
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(DB_SCHEMA)
            self._conn.commit()

    def _require_writable(self) -> sqlite3.Connection:
        if self._conn is None or self.read_only:
            raise AlertsError("store is read-only")
        return self._conn

    def sent_ids(self) -> set[str]:
        """Return ids already delivered.

        Read errors propagate. Treating a failed read as "nothing sent" would
        risk re-sending every listing, so callers must handle the error instead
        of continuing blind.
        """

        if self._conn is None:
            return set()
        rows = self._conn.execute(
            "SELECT listing_id FROM sent WHERE sent_at IS NOT NULL"
        )
        return {str(row[0]) for row in rows}

    def is_sent(self, listing_id: str) -> bool:
        if self._conn is None:
            return False
        row = self._conn.execute(
            "SELECT 1 FROM sent WHERE listing_id = ? AND sent_at IS NOT NULL",
            (listing_id,),
        ).fetchone()
        return row is not None

    def upsert_candidate(
        self,
        *,
        listing_id: str,
        keyword: str | None,
        item: dict,
        published_at: datetime | None,
        now_iso: str,
    ) -> None:
        conn = self._require_writable()
        with conn:
            conn.execute(
                "INSERT OR IGNORE INTO sent"
                "(listing_id, keyword, url, title, price, published_at,"
                " first_seen_at, sent_at, failure_count) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, NULL, 0)",
                (
                    listing_id,
                    keyword,
                    item.get("url"),
                    item.get("title"),
                    item.get("price"),
                    published_at.isoformat() if published_at else None,
                    now_iso,
                ),
            )

    def mark_sent(self, listing_id: str, now_iso: str) -> None:
        conn = self._require_writable()
        with conn:
            conn.execute(
                "UPDATE sent SET sent_at = ?, last_attempt_at = ?, last_error = NULL "
                "WHERE listing_id = ?",
                (now_iso, now_iso, listing_id),
            )

    def mark_failure(self, listing_id: str, error: str | None, now_iso: str) -> None:
        conn = self._require_writable()
        with conn:
            conn.execute(
                "UPDATE sent SET failure_count = failure_count + 1, "
                "last_error = ?, last_attempt_at = ? WHERE listing_id = ?",
                (error, now_iso, listing_id),
            )

    def failure_count(self, listing_id: str) -> int:
        if self._conn is None:
            return 0
        row = self._conn.execute(
            "SELECT failure_count FROM sent WHERE listing_id = ?", (listing_id,)
        ).fetchone()
        return int(row[0]) if row else 0

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> "SentStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def _missing_table(err: sqlite3.Error) -> bool:
    return "no such table" in str(err).lower()


class IbilikSentStore:
    """SQLite state for iBilik searches, in its own tables.

    Kept separate from :class:`SentStore` so iBilik UUIDs never collide with
    Mudah ids and an existing Mudah database upgrades in place without its rows
    being touched. A row counts as known once it is seeded or sent; a failed row
    keeps both timestamps null so the next cycle retries it.
    """

    def __init__(self, path, *, read_only: bool = False):
        self.path = Path(path)
        self.read_only = read_only
        self._conn: sqlite3.Connection | None = None
        if read_only:
            if self.path.exists():
                target = self.path.resolve().as_posix()
                self._conn = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(self.path))
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(DB_IBILIK_SCHEMA)
            self._conn.commit()

    def _require_writable(self) -> sqlite3.Connection:
        if self._conn is None or self.read_only:
            raise AlertsError("store is read-only")
        return self._conn

    def known_ids(self) -> set[str]:
        """Listing ids already seeded or delivered for any iBilik search.

        Read errors propagate; treating a failed read as "nothing known" would
        re-send listings that were already delivered or suppressed. The one
        exception is a read-only store on a database that predates the iBilik
        tables: absent tables mean no iBilik state, not corruption.
        """

        if self._conn is None:
            return set()
        try:
            rows = self._conn.execute(
                "SELECT listing_id FROM ibilik_sent "
                "WHERE seeded_at IS NOT NULL OR sent_at IS NOT NULL"
            )
        except sqlite3.OperationalError as err:
            if self.read_only and _missing_table(err):
                return set()
            raise
        return {str(row[0]) for row in rows}

    def is_sent(self, search_name: str, listing_id: str) -> bool:
        if self._conn is None:
            return False
        row = self._conn.execute(
            "SELECT 1 FROM ibilik_sent WHERE search_name = ? AND listing_id = ? "
            "AND sent_at IS NOT NULL",
            (search_name, listing_id),
        ).fetchone()
        return row is not None

    def initialized_searches(self) -> set[str]:
        if self._conn is None:
            return set()
        try:
            rows = self._conn.execute(
                "SELECT search_name FROM ibilik_searches WHERE initialized_at IS NOT NULL"
            )
        except sqlite3.OperationalError as err:
            if self.read_only and _missing_table(err):
                return set()
            raise
        return {str(row[0]) for row in rows}

    def seed_search(
        self,
        search_name: str,
        rows: list[tuple[dict, datetime | None]],
        now_iso: str,
    ) -> None:
        """Record a whole search's current listings and mark it initialised.

        One transaction: a partial seed cannot survive a failure, so the next
        cycle re-seeds the search from a clean slate.
        """

        conn = self._require_writable()
        with conn:
            for item, published_at in rows:
                conn.execute(
                    "INSERT OR IGNORE INTO ibilik_sent"
                    "(search_name, listing_id, keyword, url, title, price,"
                    " published_at, first_seen_at, seeded_at, sent_at, failure_count) "
                    "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 0)",
                    (
                        search_name,
                        item.get("id"),
                        search_name,
                        item.get("url"),
                        item.get("title"),
                        item.get("price"),
                        published_at.isoformat() if published_at else None,
                        now_iso,
                        now_iso,
                    ),
                )
            conn.execute(
                "INSERT INTO ibilik_searches(search_name, initialized_at) "
                "VALUES(?, ?) "
                "ON CONFLICT(search_name) DO UPDATE SET initialized_at = excluded.initialized_at",
                (search_name, now_iso),
            )

    def upsert_candidate(
        self,
        *,
        search_name: str,
        listing_id: str,
        item: dict,
        published_at: datetime | None,
        now_iso: str,
    ) -> None:
        conn = self._require_writable()
        with conn:
            conn.execute(
                "INSERT OR IGNORE INTO ibilik_sent"
                "(search_name, listing_id, keyword, url, title, price,"
                " published_at, first_seen_at, seeded_at, sent_at, failure_count) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, 0)",
                (
                    search_name,
                    listing_id,
                    search_name,
                    item.get("url"),
                    item.get("title"),
                    item.get("price"),
                    published_at.isoformat() if published_at else None,
                    now_iso,
                ),
            )

    def mark_sent(self, search_name: str, listing_id: str, now_iso: str) -> None:
        conn = self._require_writable()
        with conn:
            conn.execute(
                "UPDATE ibilik_sent SET sent_at = ?, last_attempt_at = ?, "
                "last_error = NULL WHERE search_name = ? AND listing_id = ?",
                (now_iso, now_iso, search_name, listing_id),
            )

    def mark_failure(
        self, search_name: str, listing_id: str, error: str | None, now_iso: str
    ) -> None:
        conn = self._require_writable()
        with conn:
            conn.execute(
                "UPDATE ibilik_sent SET failure_count = failure_count + 1, "
                "last_error = ?, last_attempt_at = ? "
                "WHERE search_name = ? AND listing_id = ?",
                (error, now_iso, search_name, listing_id),
            )

    def failure_count(self, search_name: str, listing_id: str) -> int:
        if self._conn is None:
            return 0
        row = self._conn.execute(
            "SELECT failure_count FROM ibilik_sent "
            "WHERE search_name = ? AND listing_id = ?",
            (search_name, listing_id),
        ).fetchone()
        return int(row[0]) if row else 0

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> "IbilikSentStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


# --------------------------------------------------------------------------
# Singleton process lock
# --------------------------------------------------------------------------


class SingleInstanceLock:
    """Advisory cross-platform lock that the OS releases if the process dies."""

    def __init__(self, path):
        self.path = Path(path)
        self._handle = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+b")
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as err:
            handle.close()
            raise LockError(
                f"another mudah-alerts process holds {self.path}; "
                "start only one service per database"
            ) from err
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()).encode("ascii", errors="ignore"))
        handle.flush()
        self._handle = handle

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            return
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            handle.close()
            self._handle = None

    def __enter__(self) -> "SingleInstanceLock":
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()


# --------------------------------------------------------------------------
# Service
# --------------------------------------------------------------------------


@dataclass
class Candidate:
    listing_id: str
    keyword: str
    item: dict
    published_at: datetime


@dataclass
class CycleReport:
    started_at: datetime
    finished_at: datetime | None = None
    keywords_searched: int = 0
    pages_fetched: int = 0
    listings_seen: int = 0
    fresh_candidates: int = 0
    sent: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    would_send: list[str] = field(default_factory=list)
    skipped_stale: int = 0
    skipped_invalid_timestamp: int = 0
    skipped_over_price: int = 0
    skipped_missing_price: int = 0
    skipped_invalid_url: int = 0
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"searched={self.keywords_searched} pages={self.pages_fetched} "
            f"seen={self.listings_seen} candidates={self.fresh_candidates} "
            f"sent={len(self.sent)} failed={len(self.failed)} "
            f"would_send={len(self.would_send)} errors={len(self.errors)} "
            f"stale={self.skipped_stale} bad_time={self.skipped_invalid_timestamp} "
            f"over_price={self.skipped_over_price} no_price={self.skipped_missing_price} "
            f"bad_url={self.skipped_invalid_url}"
        )


class AlertService:
    """One polling service over a set of keyword searches."""

    def __init__(
        self,
        config: AlertConfig,
        *,
        search=None,
        sender=None,
        store: SentStore | None = None,
        sleep=time.sleep,
        now=None,
        jitter=None,
        logger=None,
        dry_run: bool = False,
    ):
        self.config = config
        self.search = search or mudah.search_rentals
        self.sender = sender
        self.store = store
        self.sleep = sleep
        self.now = now or (lambda: datetime.now(timezone.utc))
        self._jitter = jitter or (lambda span: random.uniform(0.0, span))
        self.log = logger or logging.getLogger("mudah_alerts")
        self.dry_run = dry_run
        self._consecutive_failures = 0
        self._telegram_retry_after = 0.0
        self._provider_retry_after = 0.0
        self._sent_ids: set[str] = set()
        self._cycle_ids: set[str] = set()
        self._cycle_halted = False
        self._telegram_halted = False

    def effective_max_price(self, keyword: Keyword) -> int | None:
        if keyword.max_price is not None:
            return keyword.max_price
        return self.config.max_price

    def _freshness_hours(self) -> float:
        # Hard runtime cap, independent of any programmatic config value.
        return min(self.config.freshness_hours, MAX_FRESHNESS_HOURS)

    def _iter_search_pages(self, keyword: Keyword):
        """Yield ``(page, result)`` for one keyword, pausing between pages.

        The caller consumes each page before the next request is made, so a
        later page failure cannot discard results already delivered.
        """

        for page in range(1, self.config.max_pages_per_keyword + 1):
            result = self.search(
                location=self.config.location,
                rental_type=self.config.rental_type,
                query=keyword.query,
                max_price=self.effective_max_price(keyword),
                page=page,
                sort_by="latest",
                timeout=self.config.search_timeout_seconds,
            )
            yield page, result
            pagination = result.get("pagination") or {}
            if not pagination.get("has_next"):
                return
            if (pagination.get("organic_items_on_page") or 0) < mudah.SITE_PAGE_SIZE:
                return
            self.sleep(self.config.page_pacing_seconds)

    def _record_provider_error(self, err: Exception) -> None:
        headers = getattr(err, "headers", None)
        if headers is None:
            return
        try:
            value = headers.get("Retry-After")
        except AttributeError:
            return
        seconds = _retry_after_seconds(value)
        if seconds:
            self._provider_retry_after = max(self._provider_retry_after, seconds)

    def _process_page(
        self, keyword: Keyword, items: list[dict], report: CycleReport
    ) -> None:
        now = self.now()
        max_price = self.effective_max_price(keyword)
        for item in items:
            report.listings_seen += 1
            if not isinstance(item, dict):
                continue
            listing_id = item.get("id")
            if not listing_id or listing_id in self._cycle_ids:
                continue
            published = listing_published_at(item, now=now)
            if published is None:
                report.skipped_invalid_timestamp += 1
                continue
            if not is_fresh(
                published, now=now, freshness_hours=self._freshness_hours()
            ):
                report.skipped_stale += 1
                continue
            price = _numeric_price(item.get("price"))
            if price is None:
                report.skipped_missing_price += 1
                continue
            if max_price is not None and price > max_price:
                report.skipped_over_price += 1
                continue
            self._cycle_ids.add(listing_id)
            self._deliver_one(
                Candidate(
                    listing_id=listing_id,
                    keyword=keyword.query,
                    item=item,
                    published_at=published,
                ),
                report,
            )
            if self._cycle_halted:
                return

    def _deliver_one(self, candidate: Candidate, report: CycleReport) -> None:
        if self._telegram_halted or self._cycle_halted:
            return
        if candidate.listing_id in self._sent_ids:
            return
        if self.dry_run:
            report.would_send.append(candidate.listing_id)
            return

        # Recheck against the current clock, not the cycle-start time, so a
        # listing that aged past the window during fetching is not sent.
        current = self.now()
        if not is_fresh(
            candidate.published_at,
            now=current,
            freshness_hours=self._freshness_hours(),
        ):
            report.skipped_stale += 1
            return

        if self.store is None:
            raise AlertsError("a writable store is required to send alerts")
        sender = self.sender
        if sender is None:
            raise AlertsError("a Telegram sender is required to send alerts")

        message = build_message(
            candidate.item, published_at=candidate.published_at, now=current
        )
        if message is None:
            report.skipped_invalid_url += 1
            self.log.warning(
                "listing %s has no usable alert payload; skipping",
                candidate.listing_id,
            )
            return

        now_iso = current.astimezone(timezone.utc).isoformat()
        try:
            self.store.upsert_candidate(
                listing_id=candidate.listing_id,
                keyword=candidate.keyword,
                item=candidate.item,
                published_at=candidate.published_at,
                now_iso=now_iso,
            )
        except sqlite3.Error as err:
            self._halt_on_state_error(report, candidate.listing_id, err)
            return

        result = sender.send(message)
        if result.ok:
            try:
                self.store.mark_sent(candidate.listing_id, now_iso)
            except sqlite3.Error as err:
                self._halt_on_state_error(report, candidate.listing_id, err)
                return
            self._sent_ids.add(candidate.listing_id)
            report.sent.append(candidate.listing_id)
            self.log.info("sent alert for listing %s", candidate.listing_id)
            return

        try:
            self.store.mark_failure(candidate.listing_id, result.error, now_iso)
        except sqlite3.Error as err:
            self._halt_on_state_error(report, candidate.listing_id, err)
            return
        report.failed.append(candidate.listing_id)
        self.log.warning(
            "telegram send failed for %s: %s", candidate.listing_id, result.error
        )
        if result.rate_limited or result.retry_after is not None:
            cooldown = (
                result.retry_after
                if result.retry_after is not None
                else TELEGRAM_429_DEFAULT_COOLDOWN
            )
            self._telegram_retry_after = max(self._telegram_retry_after, cooldown)
            # Stop sending for the rest of the cycle once the API pushes back,
            # even without an explicit retry_after.
            self._telegram_halted = True
            self.log.warning(
                "telegram rate limited; stopping sends this cycle for %.0fs",
                cooldown,
            )

    def _halt_on_state_error(
        self, report: CycleReport, listing_id: str, err: sqlite3.Error
    ) -> None:
        report.errors.append(f"state write failed for {listing_id}: {err}")
        self.log.error("could not write alert state for %s: %s", listing_id, err)
        self._cycle_halted = True

    def run_cycle(self) -> CycleReport:
        report = CycleReport(started_at=self.now())
        if self.store is not None:
            # Read errors propagate deliberately; a failed read must not look
            # like "nothing sent" or every listing would be re-sent.
            try:
                self._sent_ids = self.store.sent_ids()
            except sqlite3.Error as err:
                self._consecutive_failures += 1
                report.errors.append(f"state read failed: {err}")
                self.log.error(
                    "could not read sent state; skipping sends this cycle: %s", err
                )
                report.finished_at = self.now()
                return report

        self._cycle_ids = set()
        self._cycle_halted = False
        self._telegram_halted = False
        cycle_failed = False
        provider_halted = False

        keywords = list(self.config.keywords)
        for index, keyword in enumerate(keywords):
            if provider_halted or self._cycle_halted or self._telegram_halted:
                break
            try:
                for _page, result in self._iter_search_pages(keyword):
                    report.pages_fetched += 1
                    self._process_page(keyword, result.get("items") or [], report)
                    if self._cycle_halted or self._telegram_halted:
                        break
            except (
                mudah.MudahBlockedError,
                mudah.MudahError,
                urllib.error.HTTPError,
                urllib.error.URLError,
                TimeoutError,
                OSError,
                ValueError,
                TypeError,
                AttributeError,
                KeyError,
            ) as err:
                cycle_failed = True
                report.errors.append(f"{keyword.query}: {err}")
                self._record_provider_error(err)
                self.log.warning("keyword %r failed: %s", keyword.query, err)
                # Do not keep hitting the provider after a block or a 429.
                if isinstance(err, mudah.MudahBlockedError) or (
                    isinstance(err, urllib.error.HTTPError) and err.code == 429
                ):
                    provider_halted = True
            else:
                report.keywords_searched += 1
            if (
                index < len(keywords) - 1
                and not provider_halted
                and not self._cycle_halted
                and not self._telegram_halted
            ):
                # Pace keyword requests, not only pages.
                self.sleep(self.config.page_pacing_seconds)

        if cycle_failed:
            # Persist the failing-cycle streak so backoff grows until a healthy
            # cycle resets it.
            self._consecutive_failures += 1
        else:
            self._consecutive_failures = 0
        report.fresh_candidates = len(self._cycle_ids)
        report.finished_at = self.now()
        return report

    def next_delay(self) -> float:
        """Seconds to wait before the next poll, with backoff when unhealthy."""

        delay = self.config.poll_interval_seconds
        if self._consecutive_failures:
            exponent = min(self._consecutive_failures - 1, 16)
            backoff = min(
                BACKOFF_BASE_SECONDS * (2**exponent), BACKOFF_CAP_SECONDS
            )
            delay = max(delay, backoff) + self._jitter(backoff * 0.25)
        if self._provider_retry_after:
            delay = max(delay, self._provider_retry_after)
            self._provider_retry_after = 0.0
        if self._telegram_retry_after:
            delay = max(delay, self._telegram_retry_after + 1.0)
            self._telegram_retry_after = 0.0
        return delay

    def run_forever(self, stop_event: threading.Event) -> None:
        while not stop_event.is_set():
            report = self.run_cycle()
            delay = self.next_delay()
            self.log.info("cycle done: %s; next poll in %.1fs", report.summary(), delay)
            if stop_event.wait(delay):
                break


def _retry_after_seconds(value) -> float | None:
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------
# iBilik service
# --------------------------------------------------------------------------

_IBILIK_FETCH_ERRORS = (
    ibilik.IbilikError,
    urllib.error.HTTPError,
    urllib.error.URLError,
    TimeoutError,
    OSError,
    ValueError,
    TypeError,
    AttributeError,
    KeyError,
)


def _default_ibilik_search(search: IbilikSearch, *, page: int, limit: int, timeout: float) -> dict:
    """Fetch and normalise one iBilik page via the shared finder client."""

    return ibilik.search_page(
        search.as_filters(), page=page, limit=limit, timeout=timeout
    )


@dataclass
class IbilikCycleReport:
    started_at: datetime
    finished_at: datetime | None = None
    searches_searched: int = 0
    pages_fetched: int = 0
    listings_seen: int = 0
    fresh_candidates: int = 0
    seeded: list[str] = field(default_factory=list)
    seeded_count: int = 0
    would_seed: list[str] = field(default_factory=list)
    sent: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    would_send: list[str] = field(default_factory=list)
    skipped_stale: int = 0
    skipped_invalid_timestamp: int = 0
    skipped_over_price: int = 0
    skipped_missing_price: int = 0
    skipped_invalid_url: int = 0
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"searches={self.searches_searched} pages={self.pages_fetched} "
            f"seen={self.listings_seen} candidates={self.fresh_candidates} "
            f"seeded={len(self.seeded)} would_seed={len(self.would_seed)} "
            f"sent={len(self.sent)} failed={len(self.failed)} "
            f"would_send={len(self.would_send)} errors={len(self.errors)} "
            f"stale={self.skipped_stale} bad_time={self.skipped_invalid_timestamp} "
            f"over_price={self.skipped_over_price} no_price={self.skipped_missing_price} "
            f"bad_url={self.skipped_invalid_url}"
        )


class IbilikAlertService:
    """One polling service over the opt-in iBilik searches.

    Runs independently of the Mudah service: state is in its own tables, a
    fetch failure only fails that search (or halts iBilik on a 429), and it can
    operate with no Mudah keywords and no ``MUDAH_AUTOMATION_ALLOWED``.
    """

    def __init__(
        self,
        config: IbilikConfig,
        *,
        search=None,
        sender=None,
        store: IbilikSentStore | None = None,
        sleep=time.sleep,
        now=None,
        jitter=None,
        logger=None,
        dry_run: bool = False,
    ):
        self.config = config
        self.search = search or _default_ibilik_search
        self.sender = sender
        self.store = store
        self.sleep = sleep
        self.now = now or (lambda: datetime.now(timezone.utc))
        self._jitter = jitter or (lambda span: random.uniform(0.0, span))
        self.log = logger or logging.getLogger("mudah_alerts.ibilik")
        self.dry_run = dry_run
        self._consecutive_failures = 0
        self._telegram_retry_after = 0.0
        self._provider_retry_after = 0.0
        self._known_ids: set[str] = set()
        self._initialized: set[str] = set()
        self._cycle_ids: set[str] = set()
        self._cycle_halted = False
        self._telegram_halted = False

    def _freshness_hours(self) -> float:
        return min(self.config.freshness_hours, MAX_IBILIK_FRESHNESS_HOURS)

    def _iter_pages(self, search: IbilikSearch):
        """Yield ``(page, result)`` for one search, pausing between pages.

        The caller consumes each page before the next request, so a later-page
        failure cannot discard results already delivered.
        """

        for page in range(1, self.config.max_pages_per_search + 1):
            result = self.search(
                search,
                page=page,
                limit=self.config.limit_per_page,
                timeout=self.config.search_timeout_seconds,
            )
            yield page, result
            pagination = result.get("pagination") or {}
            if not pagination.get("has_next"):
                return
            self.sleep(self.config.page_pacing_seconds)

    def _record_provider_error(self, err: Exception) -> None:
        headers = getattr(err, "headers", None)
        if headers is None:
            return
        try:
            value = headers.get("Retry-After")
        except AttributeError:
            return
        seconds = _retry_after_seconds(value)
        if seconds:
            self._provider_retry_after = max(self._provider_retry_after, seconds)

    def _seed_search(self, search: IbilikSearch, report: IbilikCycleReport) -> None:
        """Fetch every page first, then seed in one transaction.

        Nothing is written until every page of the search has been fetched, so
        a partial fetch leaves the search uninitialised instead of silently
        hiding the listings that were missed.
        """

        items: list[dict] = []
        seen: set[str] = set()
        for _page, result in self._iter_pages(search):
            report.pages_fetched += 1
            for item in result.get("items") or []:
                if not isinstance(item, dict):
                    continue
                listing_id = item.get("id")
                if not listing_id or listing_id in seen:
                    continue
                seen.add(listing_id)
                items.append(item)

        if self.dry_run:
            report.would_seed.append(search.name)
            report.seeded_count += len(items)
            return
        if self.store is None:
            raise AlertsError("a writable store is required to seed iBilik searches")

        now = self.now()
        now_iso = now.astimezone(timezone.utc).isoformat()
        rows = [(item, listing_published_at(item, now=now)) for item in items]
        try:
            self.store.seed_search(search.name, rows, now_iso)
        except sqlite3.Error as err:
            self._halt_on_state_error(report, search.name, err)
            return
        self._initialized.add(search.name)
        self._known_ids.update(seen)
        report.seeded.append(search.name)
        report.seeded_count += len(seen)
        self.log.info("seeded %d iBilik listing(s) for %r (no alerts)", len(seen), search.name)

    def _process_page(
        self, search: IbilikSearch, items: list[dict], report: IbilikCycleReport
    ) -> None:
        now = self.now()
        for item in items:
            report.listings_seen += 1
            if not isinstance(item, dict):
                continue
            listing_id = item.get("id")
            if not listing_id or listing_id in self._cycle_ids or listing_id in self._known_ids:
                continue
            published = listing_published_at(item, now=now)
            if published is None:
                report.skipped_invalid_timestamp += 1
                continue
            if not is_fresh(published, now=now, freshness_hours=self._freshness_hours()):
                report.skipped_stale += 1
                continue
            price = _numeric_price(item.get("price"))
            if price is None:
                report.skipped_missing_price += 1
                continue
            if search.max_price is not None and price > search.max_price:
                report.skipped_over_price += 1
                continue
            self._cycle_ids.add(listing_id)
            self._deliver_one(
                search,
                Candidate(
                    listing_id=listing_id,
                    keyword=search.name,
                    item=item,
                    published_at=published,
                ),
                report,
            )
            if self._cycle_halted:
                return

    def _deliver_one(
        self, search: IbilikSearch, candidate: Candidate, report: IbilikCycleReport
    ) -> None:
        if self._telegram_halted or self._cycle_halted:
            return
        if candidate.listing_id in self._known_ids:
            return
        if self.dry_run:
            report.would_send.append(candidate.listing_id)
            self._known_ids.add(candidate.listing_id)
            return

        current = self.now()
        if not is_fresh(
            candidate.published_at, now=current, freshness_hours=self._freshness_hours()
        ):
            report.skipped_stale += 1
            return

        if self.store is None:
            raise AlertsError("a writable store is required to send alerts")
        sender = self.sender
        if sender is None:
            raise AlertsError("a Telegram sender is required to send alerts")

        message = build_ibilik_message(
            candidate.item, published_at=candidate.published_at, now=current
        )
        if message is None:
            report.skipped_invalid_url += 1
            self.log.warning(
                "iBilik listing %s has no usable alert payload; skipping",
                candidate.listing_id,
            )
            return

        now_iso = current.astimezone(timezone.utc).isoformat()
        try:
            self.store.upsert_candidate(
                search_name=search.name,
                listing_id=candidate.listing_id,
                item=candidate.item,
                published_at=candidate.published_at,
                now_iso=now_iso,
            )
        except sqlite3.Error as err:
            self._halt_on_state_error(report, candidate.listing_id, err)
            return

        result = sender.send(message)
        if result.ok:
            try:
                self.store.mark_sent(search.name, candidate.listing_id, now_iso)
            except sqlite3.Error as err:
                self._halt_on_state_error(report, candidate.listing_id, err)
                return
            self._known_ids.add(candidate.listing_id)
            report.sent.append(candidate.listing_id)
            self.log.info("sent iBilik alert for listing %s", candidate.listing_id)
            return

        try:
            self.store.mark_failure(search.name, candidate.listing_id, result.error, now_iso)
        except sqlite3.Error as err:
            self._halt_on_state_error(report, candidate.listing_id, err)
            return
        report.failed.append(candidate.listing_id)
        self.log.warning(
            "telegram send failed for iBilik %s: %s", candidate.listing_id, result.error
        )
        if result.rate_limited or result.retry_after is not None:
            cooldown = (
                result.retry_after
                if result.retry_after is not None
                else TELEGRAM_429_DEFAULT_COOLDOWN
            )
            self._telegram_retry_after = max(self._telegram_retry_after, cooldown)
            self._telegram_halted = True
            self.log.warning(
                "telegram rate limited; stopping iBilik sends this cycle for %.0fs",
                cooldown,
            )

    def _halt_on_state_error(
        self, report: IbilikCycleReport, listing_id: str, err: sqlite3.Error
    ) -> None:
        report.errors.append(f"state write failed for {listing_id}: {err}")
        self.log.error("could not write iBilik state for %s: %s", listing_id, err)
        self._cycle_halted = True

    def run_cycle(self) -> IbilikCycleReport:
        report = IbilikCycleReport(started_at=self.now())
        if self.store is not None:
            try:
                self._known_ids = self.store.known_ids()
                self._initialized = self.store.initialized_searches()
            except sqlite3.Error as err:
                self._consecutive_failures += 1
                report.errors.append(f"state read failed: {err}")
                self.log.error(
                    "could not read iBilik state; skipping sends this cycle: %s", err
                )
                report.finished_at = self.now()
                return report

        self._cycle_ids = set()
        self._cycle_halted = False
        self._telegram_halted = False
        cycle_failed = False
        provider_halted = False

        searches = list(self.config.searches)
        for index, search in enumerate(searches):
            if provider_halted or self._cycle_halted or self._telegram_halted:
                break
            try:
                if self.config.initial_seed and search.name not in self._initialized:
                    self._seed_search(search, report)
                else:
                    for _page, result in self._iter_pages(search):
                        report.pages_fetched += 1
                        self._process_page(search, result.get("items") or [], report)
                        if self._cycle_halted or self._telegram_halted:
                            break
            except _IBILIK_FETCH_ERRORS as err:
                cycle_failed = True
                report.errors.append(f"{search.name}: {err}")
                self._record_provider_error(err)
                self.log.warning("iBilik search %r failed: %s", search.name, err)
                if isinstance(err, urllib.error.HTTPError) and err.code == 429:
                    provider_halted = True
            else:
                report.searches_searched += 1
            if (
                index < len(searches) - 1
                and not provider_halted
                and not self._cycle_halted
                and not self._telegram_halted
            ):
                self.sleep(self.config.page_pacing_seconds)

        if cycle_failed:
            self._consecutive_failures += 1
        else:
            self._consecutive_failures = 0
        report.fresh_candidates = len(self._cycle_ids)
        report.finished_at = self.now()
        return report

    def next_delay(self) -> float:
        """Seconds to wait before the next poll, with backoff when unhealthy."""

        delay = self.config.poll_interval_seconds
        if self._consecutive_failures:
            exponent = min(self._consecutive_failures - 1, 16)
            backoff = min(BACKOFF_BASE_SECONDS * (2**exponent), BACKOFF_CAP_SECONDS)
            delay = max(delay, backoff) + self._jitter(backoff * 0.25)
        if self._provider_retry_after:
            delay = max(delay, self._provider_retry_after)
            self._provider_retry_after = 0.0
        if self._telegram_retry_after:
            delay = max(delay, self._telegram_retry_after + 1.0)
            self._telegram_retry_after = 0.0
        return delay


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mudah_alerts.py",
        description=(
            "Poll Mudah and iBilik rental searches (each on its own cadence) and "
            "alert Telegram about fresh listings. When Mudah is not authorised, "
            "its searches are skipped and iBilik keeps polling."
        ),
    )
    parser.add_argument(
        "--config",
        default=None,
        help="Path to a JSON config (default: config.json beside this script).",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run one poll cycle and exit.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Search and report matching listings without sending Telegram "
            "messages or writing seen state."
        ),
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable debug logging.",
    )
    return parser


def _automation_allowed() -> bool:
    return os.environ.get("MUDAH_AUTOMATION_ALLOWED", "").strip() == "1"


def _mudah_gate_required(config: AlertConfig) -> bool:
    """The Mudah permission gate applies only when Mudah keywords are set.

    An iBilik-only config uses the public iBilik API, so it must not be blocked
    by a flag that only exists because of Mudah's robots.txt terms.
    """

    return bool(config.keywords)


def _apply_mudah_gate(
    config: AlertConfig, *, allowed: bool, logger=None
) -> AlertConfig:
    """Drop Mudah keywords when automation has not been authorised.

    This keeps a combined config useful: iBilik uses a public API and is not
    gated, so its searches keep polling while Mudah is skipped with a clear
    warning. A Mudah-only config becomes providerless and ``main`` reports that
    nothing can run. The standalone Mudah finder keeps its own hard gate.
    """

    if allowed or not config.keywords:
        return config
    log = logger or logging.getLogger("mudah_alerts")
    log.warning(
        "MUDAH_AUTOMATION_ALLOWED is not 1; skipping Mudah searches "
        "(robots.txt authorisation required). iBilik keeps polling if configured."
    )
    return replace(config, keywords=())


def _run_dry(config: AlertConfig, log: logging.Logger) -> int:
    """Report matches for every configured provider without writing state."""

    if config.keywords:
        with SentStore(config.database_path, read_only=True) as store:
            report = AlertService(config, store=store, dry_run=True).run_cycle()
        log.info("mudah dry run: %s", report.summary())
        for listing_id in report.would_send:
            log.info("would send %s", listing_id)
        for error in report.errors:
            log.warning("mudah search error: %s", error)

    if config.ibilik is not None:
        with IbilikSentStore(config.database_path, read_only=True) as store:
            report = IbilikAlertService(
                config.ibilik, store=store, dry_run=True
            ).run_cycle()
        log.info("ibilik dry run: %s", report.summary())
        for search_name in report.would_seed:
            log.info("would seed iBilik search %s", search_name)
        for listing_id in report.would_send:
            log.info("would send iBilik %s", listing_id)
        for error in report.errors:
            log.warning("iBilik search error: %s", error)
    return 0


def _build_services(config: AlertConfig, sender) -> tuple[list, list]:
    """Create the stores and services for every configured provider.

    Returns ``(stores, services)``; callers close the stores when done. Each
    provider gets its own store so their dedupe state stays independent.
    """

    stores: list = []
    services: list = []
    if config.keywords:
        store = SentStore(config.database_path)
        stores.append(store)
        services.append(AlertService(config, sender=sender, store=store))
    if config.ibilik is not None:
        ibilik_store = IbilikSentStore(config.database_path)
        stores.append(ibilik_store)
        services.append(
            IbilikAlertService(config.ibilik, sender=sender, store=ibilik_store)
        )
    return stores, services


def _run_once(services, log: logging.Logger) -> int:
    """Run one cycle per provider, containing an unexpected crash.

    Expected provider errors are already turned into reports inside
    ``run_cycle``. Anything that escapes is unexpected, so it is logged here
    without stopping the remaining providers, and the caller gets a nonzero
    code to tell a partial run from a clean one.
    """

    exit_code = 0
    for service in services:
        try:
            report = service.run_cycle()
            log.info("%s once: %s", type(service).__name__, report.summary())
        except Exception as err:  # noqa: BLE001 - isolate providers from each other
            exit_code = 1
            log.error("%s once crashed: %s", type(service).__name__, err)
    return exit_code


def _run_services_forever(
    services,
    stop_event: threading.Event,
    log: logging.Logger,
    *,
    monotonic=time.monotonic,
) -> None:
    """Poll each provider against its own monotonic due time.

    Every provider holds a due time. The loop runs whichever providers are due
    now, then sleeps until the earliest next due time, so a long backoff on one
    provider never delays a healthy one and the wait is never a hot loop. No
    threads are used, and ``stop_event`` breaks the wait promptly.
    """

    if not services:
        return

    schedule = [(service, monotonic()) for service in services]
    while not stop_event.is_set():
        now = monotonic()
        summaries: list[str] = []
        for index, (service, due) in enumerate(schedule):
            if due > now:
                continue
            try:
                report = service.run_cycle()
                summaries.append(f"{type(service).__name__}: {report.summary()}")
            except Exception as err:  # noqa: BLE001 - isolate providers from each other
                log.error("provider cycle crashed: %s", err)
            try:
                delay = service.next_delay()
            except Exception as err:  # noqa: BLE001 - a delay bug must not wedge the loop
                log.error("could not compute next delay: %s", err)
                delay = DEFAULT_POLL_SECONDS
            schedule[index] = (service, monotonic() + max(0.0, delay))
        next_due = min(due for _service, due in schedule)
        wait = next_due - monotonic()
        if wait <= 0:
            # Only reachable from a zero-length delay; keep the loop bounded.
            wait = MIN_SCHEDULE_WAIT
        if summaries:
            log.info(
                "cycle done: %s; next poll in %.1fs", "; ".join(summaries), wait
            )
        if stop_event.wait(wait):
            break


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    log = logging.getLogger("mudah_alerts")

    try:
        config = load_config(args.config)
    except ConfigError as err:
        log.error("configuration error: %s", err)
        return 2

    # Mudah's permission gate only removes the Mudah side; iBilik is a public
    # API and keeps polling. A Mudah-only config with the gate off has nothing
    # left to run, so fail closed with a clear message.
    config = _apply_mudah_gate(config, allowed=_automation_allowed(), logger=log)
    if not config.keywords and config.ibilik is None:
        log.error(
            "nothing to poll: Mudah is disabled (MUDAH_AUTOMATION_ALLOWED is not "
            "1) and no iBilik block is configured."
        )
        return 2

    if args.dry_run:
        return _run_dry(config, log)

    try:
        sender = sender_from_env(timeout=config.search_timeout_seconds)
    except ConfigError as err:
        log.error("%s", err)
        return 2

    lock = SingleInstanceLock(config.lock_path)
    try:
        lock.acquire()
    except LockError as err:
        log.error("%s", err)
        return 3

    stores: list = []
    services: list = []
    stop_event = threading.Event()

    def _handle_signal(signum, _frame):
        log.info("received signal %s; shutting down", signum)
        stop_event.set()

    for signame in ("SIGINT", "SIGTERM"):
        if hasattr(signal, signame):
            try:
                signal.signal(getattr(signal, signame), _handle_signal)
            except (ValueError, OSError):
                pass

    exit_code = 0
    try:
        stores, services = _build_services(config, sender)
        if args.once:
            exit_code = _run_once(services, log)
        else:
            _run_services_forever(services, stop_event, log)
    except KeyboardInterrupt:
        log.info("interrupted")
    finally:
        for store in stores:
            store.close()
        lock.release()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
