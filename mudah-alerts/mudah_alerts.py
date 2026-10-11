#!/usr/bin/env python3
"""Standalone Telegram alert service for permission-gated Mudah searches.

The service reuses the sibling ``mudah-finder/mudah.py`` client. Adding that
directory to ``sys.path`` at import time avoids copying the scraper, and no
change to the finder is required. It polls one or more keyword searches, keeps
only listings published inside a freshness window, and sends each unseen
listing to Telegram once. Seen state lives in SQLite so restarts and overlapping
keyword searches do not resend the same ad.

Mudah's robots.txt says automated access needs permission. Set
``MUDAH_AUTOMATION_ALLOWED=1`` only after you are authorised. Telegram
credentials are read from ``TELEGRAM_BOT_TOKEN`` and ``TELEGRAM_CHAT_ID``.
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
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Reuse the sibling finder instead of copying its scraper.
_FINDER_DIR = Path(__file__).resolve().parent.parent / "mudah-finder"
if str(_FINDER_DIR) not in sys.path:
    sys.path.insert(0, str(_FINDER_DIR))

import mudah  # type: ignore[import-not-found]  # noqa: E402  (import after sys.path setup)


__all__ = [
    "AlertConfig",
    "AlertService",
    "ConfigError",
    "CycleReport",
    "Keyword",
    "LockError",
    "SentStore",
    "SendResult",
    "SingleInstanceLock",
    "TelegramSender",
    "build_message",
    "is_fresh",
    "listing_published_at",
    "load_config",
    "parse_timestamp",
    "sender_from_env",
]

MYT = timezone(timedelta(hours=8))

DEFAULT_POLL_SECONDS = 120.0
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
TELEGRAM_API_ROOT = "https://api.telegram.org"
TELEGRAM_429_DEFAULT_COOLDOWN = 60.0
MESSAGE_HARD_LIMIT = 4096
MESSAGE_SOFT_LIMIT = 4000
MAX_LISTING_URL_LENGTH = 2048
_MAX_RELATIVE_DIGITS = 12
_MAX_RELATIVE_SECONDS = 100 * 365 * 86400

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


def _format_posted(published: datetime, now: datetime | None) -> str:
    if now is None:
        now = datetime.now(timezone.utc)
    moment = _as_utc(now)
    published_utc = _as_utc(published)
    delta = (moment - published_utc).total_seconds()
    if delta < 3600:
        return f"{max(1, int(delta // 60))} min ago"
    if delta < 86400:
        return f"{int(delta // 3600)} h ago"
    return published_utc.astimezone(MYT).strftime("%d %b %Y %H:%M MYT")


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
    tail = [f"{_format_price(price)}/month"]
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


def parse_config(raw: dict, *, base_dir: Path) -> AlertConfig:
    if not isinstance(raw, dict):
        raise ConfigError("config root must be a JSON object")
    _check_no_secrets(raw)
    if "lock_path" in raw:
        raise ConfigError(
            "lock_path is derived from database_path and cannot be overridden; "
            "remove it so one database always maps to one singleton lock"
        )

    keywords = _parse_keywords(raw.get("keywords"))

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
# CLI
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mudah_alerts.py",
        description=(
            "Poll permission-gated Mudah keyword searches and alert Telegram "
            "about fresh listings."
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

    if not _automation_allowed():
        log.error(
            "MUDAH_AUTOMATION_ALLOWED=1 is required. Mudah's robots.txt says "
            "automated access needs permission; set it only after authorisation."
        )
        return 2

    if args.dry_run:
        with SentStore(config.database_path, read_only=True) as store:
            service = AlertService(config, store=store, dry_run=True)
            report = service.run_cycle()
        log.info("dry run: %s", report.summary())
        for listing_id in report.would_send:
            log.info("would send %s", listing_id)
        for error in report.errors:
            log.warning("search error: %s", error)
        return 0

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

    store = SentStore(config.database_path)
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

    try:
        service = AlertService(config, sender=sender, store=store)
        if args.once:
            report = service.run_cycle()
            log.info("once: %s", report.summary())
        else:
            service.run_forever(stop_event)
    except KeyboardInterrupt:
        log.info("interrupted")
    finally:
        store.close()
        lock.release()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
