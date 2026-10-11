#!/usr/bin/env python3
"""ibilik-finder - automated room finding on ibilik.com.

A dependency-free (Python 3.14 stdlib only) CLI for the public ibilik
client API.  Subcommands:

    locations   resolve a place name to ibilik location codes
    search      search room-rental listings and print them
    watch       poll for newly published listings and alert on new ones

See README.md for the full API reference and rate-limit notes.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import random
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import IO

# ---------------------------------------------------------------------------
# Global configuration
# ---------------------------------------------------------------------------

BASE_URL = "https://prod-api.ibilik.com"
USER_AGENT = "ibilik-finder/1.0 (+personal use)"
MAX_RETRIES = 4
LIMIT_CAP = 5000

try:  # Windows console UTF-8
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:
    pass

ROOM_TYPES = ["MASTER", "GUEST", "MIDDLE", "STUDIO", "SINGLE", "SUITE"]
BATHROOM_TYPES = ["PRIVATE", "SHARED"]
UNIT_TYPES = [
    "STUDIO", "DUPLEX", "TRIPLEX", "CORNER_LOT", "END_LOT",
    "INTERMEDIATE", "LOFT", "DUAL_KEY", "SOHO", "PENTHOUSE",
]
FLOOR_LEVELS = ["GROUND", "LOW", "MID", "HIGH", "PENTHOUSE"]
SORT_FIELDS = ["published_at", "price"]
SORT_DIRECTIONS = ["asc", "desc"]

CONTACT_TYPES = {
    "PER_MONTH": "mo",
    "PER_NIGHT": "night",
}

# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------


def _backoff_seconds(attempt: int, retry_after: str | None = None) -> float:
    """Exponential backoff with jitter, honoring Retry-After when present."""
    if retry_after:
        try:
            return max(0.0, float(retry_after))
        except (TypeError, ValueError):
            pass
    base = min(2 ** attempt, 16)
    return base + random.uniform(0.0, 0.5)


class IbilikError(Exception):
    """Base class for errors raised by the iBilik client helpers."""


class ValidationError(IbilikError):
    """The API rejected a request (HTTP 422)."""

    def __init__(self, message: str, fields: list | None = None):
        super().__init__(message)
        self.message = message
        self.fields = fields or []

    def format(self) -> str:
        lines = [f"Validation error: {self.message}"]
        for field in self.fields:
            lines.append(f"  - {field.get('key')}: {field.get('message')}")
        return "\n".join(lines)


def _read_validation_error(err: urllib.error.HTTPError) -> ValidationError:
    try:
        payload = json.loads(err.read().decode("utf-8"))
    except Exception:
        return ValidationError("Validation error (HTTP 422).")
    return ValidationError(
        payload.get("message", "request rejected"),
        payload.get("fields") or [],
    )


def _print_validation_error(err) -> None:
    """Print a validation error to stderr (accepts HTTPError or ValidationError)."""
    if isinstance(err, urllib.error.HTTPError):
        err = _read_validation_error(err)
    print(err.format(), file=sys.stderr)


def request_json(path: str, params: dict, timeout: float = 30.0) -> dict:
    """GET a JSON endpoint with retries/backoff.

    Raises ``ValidationError`` on HTTP 422.  Other HTTP/network errors
    propagate unchanged so callers can decide how to handle them.
    """
    query = urllib.parse.urlencode(params, doseq=True)
    url = f"{BASE_URL}{path}?{query}"
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}

    for attempt in range(MAX_RETRIES + 1):
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as resp:
                body = resp.read().decode("utf-8")
            return json.loads(body)
        except urllib.error.HTTPError as err:
            if err.code == 422:
                raise _read_validation_error(err) from None
            retriable = err.code == 429 or 500 <= err.code < 600
            if retriable and attempt < MAX_RETRIES:
                retry_after = err.headers.get("Retry-After") if err.headers else None
                delay = _backoff_seconds(attempt, retry_after)
                print(
                    f"HTTP {err.code}; retry {attempt + 1}/{MAX_RETRIES} in {delay:.1f}s",
                    file=sys.stderr,
                )
                time.sleep(delay)
                continue
            raise
        except (urllib.error.URLError, TimeoutError) as err:
            if attempt < MAX_RETRIES:
                delay = _backoff_seconds(attempt)
                print(
                    f"Request failed ({err}); retry {attempt + 1}/{MAX_RETRIES} in {delay:.1f}s",
                    file=sys.stderr,
                )
                time.sleep(delay)
                continue
            raise
    raise RuntimeError("unreachable")  # pragma: no cover


def api_get(path: str, params: dict, timeout: float = 30.0) -> dict:
    """GET a JSON endpoint.  Prints validation errors and exits 2 on HTTP 422.

    This is the CLI-facing wrapper; library/MCP callers should prefer
    :func:`request_json`, which raises :class:`ValidationError` instead.
    """
    try:
        return request_json(path, params, timeout)
    except ValidationError as err:
        _print_validation_error(err)
        sys.exit(2)


# ---------------------------------------------------------------------------
# Normalization helpers
# ---------------------------------------------------------------------------


def _locale_name(node) -> str | None:
    """Return node.locales['en-US'].name if present, else None."""
    if not isinstance(node, dict):
        return None
    locales = node.get("locales")
    if not isinstance(locales, dict):
        return None
    en = locales.get("en-US")
    if isinstance(en, dict):
        return en.get("name")
    return None


def _first_rate_plan(item: dict):
    plans = item.get("ratePlans")
    if isinstance(plans, list) and plans and isinstance(plans[0], dict):
        return plans[0]
    return None


def _extract_contact(item: dict) -> tuple[str | None, bool]:
    """Return (phone, has_whatsapp) parsing contactInformation defensively."""
    entries = item.get("contactInformation")
    if isinstance(entries, dict):
        entries = [entries]
    if not isinstance(entries, list):
        return None, False

    phone = None
    whatsapp = False
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        contact = entry.get("contact")
        if isinstance(contact, dict):
            ctype = contact.get("contactType")
            value = contact.get("contactValue")
            if phone is None and ctype == "PHONE" and value:
                phone = value
        channels = entry.get("channels")
        if isinstance(channels, list) and "WHATSAPP" in channels:
            whatsapp = True
    return phone, whatsapp


def _amenity_names(item: dict) -> list[str]:
    names: list[str] = []
    for entry in item.get("amenities") or []:
        if not isinstance(entry, dict):
            continue
        amenity = entry.get("amenity")
        name = _locale_name(amenity)
        if name is None and isinstance(amenity, dict):
            name = amenity.get("name")
        if name:
            names.append(name)
    return names


def _as_number(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    return None


def normalize(item: dict) -> dict:
    """Map a raw API item to the compact normalized record."""
    locales = item.get("locales")
    if not isinstance(locales, dict):
        locales = {}
    en = locales.get("en-US")
    if not isinstance(en, dict):
        en = {}

    rate = _first_rate_plan(item)
    price = _as_number(rate.get("chargeAmount")) if rate else None
    charge_unit = rate.get("chargeUnit") if rate else None
    currency = rate.get("currency") if rate else None

    ltr = item.get("longTermRoomRentalListing")
    if not isinstance(ltr, dict):
        ltr = {}

    phone, whatsapp = _extract_contact(item)

    posted_by = item.get("postedBy")
    posted_name = None
    posted_verified = None
    if isinstance(posted_by, dict):
        posted_name = posted_by.get("name")
        posted_verified = posted_by.get("isKycVerified")
        if posted_verified is None:
            posted_verified = posted_by.get("isVerified")

    slug = item.get("slug")
    listing_id = item.get("id")
    url = None
    if listing_id and slug:
        url = f"https://www.ibilik.com/room-rentals/{listing_id}/{slug}"
    elif listing_id:
        url = f"https://www.ibilik.com/room-rentals/{listing_id}"

    images = item.get("images")
    image_count = len(images) if isinstance(images, list) else 0

    return {
        "id": listing_id,
        "title": en.get("title"),
        "price": price,
        "currency": currency,
        "charge_unit": charge_unit,
        "state": _locale_name(item.get("state")),
        "city": _locale_name(item.get("city")),
        "area": _locale_name(item.get("area")),
        "room_type": ltr.get("roomType"),
        "bathroom_type": ltr.get("bathroomType"),
        "unit_type": ltr.get("unitType"),
        "floor_level": ltr.get("floorLevel"),
        "is_studio": ltr.get("isStudio"),
        "space_sqft": _as_number(item.get("spaceSqft")),
        "lat": _as_number(item.get("latitude")),
        "lng": _as_number(item.get("longitude")),
        "amenities": _amenity_names(item),
        "contact_phone": phone,
        "whatsapp": whatsapp,
        "posted_by": posted_name,
        "posted_by_verified": posted_verified,
        "view_count": _as_number(item.get("viewCount")),
        "published_at": item.get("publishedAt"),
        "url": url,
        "image_count": image_count,
    }


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _fmt_price(price) -> str:
    if price is None:
        return "-"
    try:
        value = float(price)
    except (TypeError, ValueError):
        return str(price)
    if value.is_integer():
        return str(int(value))
    return f"{value:,.2f}"


def _fmt_unit(unit) -> str:
    if not unit:
        return "-"
    return CONTACT_TYPES.get(unit, str(unit))


def _truncate(text, length: int) -> str:
    if text is None:
        return "-"
    text = str(text)
    if len(text) <= length:
        return text
    return text[: max(0, length - 3)] + "..."


def _location(rec: dict) -> str:
    parts = [rec.get("state"), rec.get("city"), rec.get("area")]
    parts = [p for p in parts if p]
    return " / ".join(parts) if parts else "-"


def _contact_cell(rec: dict) -> str:
    phone = rec.get("contact_phone")
    if phone and rec.get("whatsapp"):
        return f"{phone} (WA)"
    if phone:
        return phone
    if rec.get("whatsapp"):
        return "(WA)"
    return "-"


def _fmt_duration(seconds: float) -> str:
    total = int(seconds)
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h{minutes:02d}m{secs:02d}s"
    return f"{minutes}m{secs:02d}s"


SEARCH_COLUMNS = ["PRICE", "UNIT", "STATE/CITY/AREA", "ROOM/BATH", "TITLE", "CONTACT", "URL"]
LOCATION_COLUMNS = ["CODE", "NAME", "TYPE"]


def _render_table(columns: list[str], rows: list[list[str]], out) -> None:
    """Print a table, auto-sizing columns and truncating the TITLE column."""
    if not rows:
        print(" | ".join(columns), file=out)
        return
    widths = [len(c) for c in columns]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(str(cell)))
    # Cap the title column (index 4 in search table) to keep output compact.
    title_idx = 4 if len(columns) == len(SEARCH_COLUMNS) else -1

    def fmt_row(row):
        cells = []
        for i, cell in enumerate(row):
            cell = str(cell)
            width = widths[i]
            if i == title_idx:
                cell = _truncate(cell, 50)
                width = max(width, len(cell))
            cells.append(cell)
        return " | ".join(cells)

    print(fmt_row(columns), file=out)
    print("-+-".join("-" * w for w in widths), file=out)
    for row in rows:
        print(fmt_row(row), file=out)


def _search_row(rec: dict) -> list[str]:
    room_bath = "/".join(
        p for p in [rec.get("room_type"), rec.get("bathroom_type")] if p
    ) or "-"
    if rec.get("is_studio"):
        room_bath = (room_bath + " (studio)") if room_bath != "-" else "studio"
    return [
        _fmt_price(rec.get("price")),
        _fmt_unit(rec.get("charge_unit")),
        _location(rec),
        room_bath,
        _truncate(rec.get("title"), 50),
        _contact_cell(rec),
        rec.get("url") or "-",
    ]


# ---------------------------------------------------------------------------
# Output writers
# ---------------------------------------------------------------------------

NORMALIZED_FIELDS = [
    "id", "title", "price", "currency", "charge_unit", "state", "city", "area",
    "room_type", "bathroom_type", "unit_type", "floor_level", "is_studio",
    "space_sqft", "lat", "lng", "amenities", "contact_phone", "whatsapp",
    "posted_by", "posted_by_verified", "view_count", "published_at", "url",
    "image_count",
]


class BaseWriter:
    def __init__(self, out):
        self.out = out
        self.count = 0
        self._opened: IO[str] | None = None

    def write(self, rec):
        self.count += 1

    def finish(self):
        pass

    def close(self):
        if self.out is not sys.stdout:
            self.out.close()


class JsonWriter(BaseWriter):
    def __init__(self, out):
        super().__init__(out)
        self.buffer = []

    def write(self, rec):
        super().write(rec)
        self.buffer.append(rec)

    def finish(self):
        if not self.buffer:
            self.out.write("[]\n")
            return
        json.dump(self.buffer, self.out, ensure_ascii=False, indent=2)
        self.out.write("\n")


class JsonlWriter(BaseWriter):
    def write(self, rec):
        super().write(rec)
        self.out.write(json.dumps(rec, ensure_ascii=False) + "\n")


class CsvWriter(BaseWriter):
    def __init__(self, out, fields):
        super().__init__(out)
        self.fields = fields
        self.writer = csv.DictWriter(out, fieldnames=fields, extrasaction="ignore")
        self.writer.writeheader()

    def write(self, rec):
        super().write(rec)
        row = {}
        for field in self.fields:
            value = rec.get(field)
            if field == "amenities" and isinstance(value, list):
                value = ";".join(value)
            elif isinstance(value, bool):
                value = "true" if value else "false"
            row[field] = value
        self.writer.writerow(row)


class TableWriter(BaseWriter):
    def __init__(self, out, columns, row_fn):
        super().__init__(out)
        self.columns = columns
        self.row_fn = row_fn
        self.rows = []

    def write(self, rec):
        super().write(rec)
        self.rows.append(self.row_fn(rec))

    def finish(self):
        _render_table(self.columns, self.rows, self.out)


def make_writer(args, columns, row_fn, fields):
    out = sys.stdout
    opened = None
    if args.out:
        opened = open(args.out, "w", encoding="utf-8", newline="")
        out = opened
    if args.output == "json":
        if not args.out:
            out = sys.stdout
        writer = JsonWriter(out)
    elif args.output == "jsonl":
        writer = JsonlWriter(out)
    elif args.output == "csv":
        writer = CsvWriter(out, fields)
    else:
        writer = TableWriter(out, columns, row_fn)
    writer._opened = opened
    return writer


def _close_opened(writer) -> None:
    opened = getattr(writer, "_opened", None)
    if opened is not None:
        opened.close()


def finalize(writer):
    writer.finish()
    _close_opened(writer)


# ---------------------------------------------------------------------------
# Filter / param building
# ---------------------------------------------------------------------------


def add_filter_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--search", metavar="TEXT", help="free-text search")
    parser.add_argument("--state", metavar="CODE", help="state code")
    parser.add_argument("--city", metavar="CODE", help="city code")
    parser.add_argument("--area", metavar="CODE", help="area code")
    parser.add_argument("--min-price", type=int, metavar="INT", help="minimum monthly price (MYR)")
    parser.add_argument("--max-price", type=int, metavar="INT", help="maximum monthly price (MYR)")
    parser.add_argument("--room-type", choices=ROOM_TYPES, help="room type")
    parser.add_argument("--bathroom-type", choices=BATHROOM_TYPES, help="bathroom type")
    parser.add_argument("--unit-type", choices=UNIT_TYPES, help="unit type")
    parser.add_argument("--floor-level", choices=FLOOR_LEVELS, help="floor level")
    parser.add_argument("--min-sqft", type=int, metavar="INT", help="minimum space (sqft)")
    parser.add_argument("--max-sqft", type=int, metavar="INT", help="maximum space (sqft)")
    parser.add_argument("--kyc-verified", action="store_true", help="only KYC-verified listings")
    parser.add_argument(
        "--amenity", action="append", metavar="CODE",
        help="amenity code; repeat for AND/intersection semantics",
    )
    parser.add_argument("--posted-by", metavar="USER_ID", help="filter by poster user id")
    parser.add_argument("--sort", choices=SORT_FIELDS, default="published_at")
    parser.add_argument("--direction", choices=SORT_DIRECTIONS, default="desc")


def build_search_params(args, page: int) -> dict:
    limit = args.limit
    if limit > LIMIT_CAP:
        print(
            f"warning: --limit {limit} exceeds cap {LIMIT_CAP}; using {LIMIT_CAP}",
            file=sys.stderr,
        )
        limit = LIMIT_CAP
    params: dict = {
        "limit": limit,
        "page": page,
        "sortBy": args.sort,
        "sortDirection": args.direction,
    }
    if args.search:
        params["filter[search]"] = args.search
    if args.state:
        params["filter[state]"] = args.state
    if args.city:
        params["filter[city]"] = args.city
    if args.area:
        params["filter[area]"] = args.area
    if args.min_price is not None:
        params["filter[minPrice]"] = args.min_price
    if args.max_price is not None:
        params["filter[maxPrice]"] = args.max_price
    if args.room_type:
        params["filter[roomType]"] = args.room_type
    if args.bathroom_type:
        params["filter[bathroomType]"] = args.bathroom_type
    if args.unit_type:
        params["filter[unitType]"] = args.unit_type
    if args.floor_level:
        params["filter[floorLevel]"] = args.floor_level
    if args.min_sqft is not None:
        params["filter[minSpaceSqft]"] = args.min_sqft
    if args.max_sqft is not None:
        params["filter[maxSpaceSqft]"] = args.max_sqft
    if args.kyc_verified:
        params["filter[kycVerified]"] = "true"
    if args.amenity:
        params["filter[amenities]"] = list(args.amenity)
    if args.posted_by:
        params["filter[postedByUserId]"] = args.posted_by
    return params


# ---------------------------------------------------------------------------
# Subcommand: locations
# ---------------------------------------------------------------------------


def cmd_locations(args) -> int:
    params = {"query": args.query, "limit": args.limit}
    if args.types:
        params["filter[types]"] = args.types
    data = api_get("/api/v1/client/location-search", params, args.timeout)
    items = data.get("items") or []

    if args.output == "json":
        json.dump(items, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        return 0

    rows = [
        [str(it.get("code", "")), str(it.get("name", "")), str(it.get("type", ""))]
        for it in items
    ]
    _render_table(LOCATION_COLUMNS, rows, sys.stdout)
    if not items:
        print("0 results", file=sys.stderr)
    return 0


# ---------------------------------------------------------------------------
# Subcommand: search
# ---------------------------------------------------------------------------


def cmd_search(args) -> int:
    if args.raw and args.output not in ("json", "jsonl"):
        print("error: --raw is only valid with --output json or jsonl", file=sys.stderr)
        return 2
    if args.limit < 1:
        print("error: --limit must be >= 1", file=sys.stderr)
        return 2

    writer = make_writer(
        args,
        columns=SEARCH_COLUMNS,
        row_fn=_search_row,
        fields=NORMALIZED_FIELDS,
    )
    try:
        page = 1
        total_pages = None
        total_items = None
        while True:
            params = build_search_params(args, page)
            data = api_get(
                "/api/v1/client/listings/room-rentals", params, args.timeout
            )
            items = data.get("items") or []
            pagination = data.get("pagination") or {}
            total_pages = pagination.get("totalPages", total_pages)
            total_items = pagination.get("totalItems", total_items)

            for item in items:
                if not isinstance(item, dict):
                    continue
                writer.write(item if args.raw else normalize(item))

            if args.pages != 1:
                shown_total = total_pages if total_pages is not None else "?"
                print(
                    f"page {page}/{shown_total} - {writer.count} items",
                    file=sys.stderr,
                )

            # Decide whether to continue.
            if args.pages == 1:
                break
            if not items:
                break
            if total_pages:
                if page >= total_pages:
                    break
            if args.pages > 1 and page >= args.pages:
                break

            page += 1
            if args.delay > 0:
                time.sleep(args.delay)

        finalize(writer)
        if writer.count == 0:
            print("0 results", file=sys.stderr)
        else:
            print(
                f"{writer.count} result(s)"
                + (f" of {total_items}" if total_items is not None else ""),
                file=sys.stderr,
            )
        return 0
    except BaseException:
        _close_opened(writer)
        raise


# ---------------------------------------------------------------------------
# Subcommand: watch
# ---------------------------------------------------------------------------


WATCH_SCHEMA = """
CREATE TABLE IF NOT EXISTS seen (
    listing_id TEXT PRIMARY KEY,
    first_seen TEXT,
    title TEXT,
    price INT
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def _open_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(WATCH_SCHEMA)
    conn.commit()
    return conn


def _get_meta(conn: sqlite3.Connection, key: str):
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def _set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def _id_seen(conn: sqlite3.Connection, listing_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM seen WHERE listing_id = ?", (listing_id,)
    ).fetchone()
    return row is not None


def _insert_seen(conn: sqlite3.Connection, listing_id: str, title, price) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO seen(listing_id, first_seen, title, price) "
        "VALUES(?, ?, ?, ?)",
        (
            listing_id,
            datetime.now(timezone.utc).isoformat(),
            title,
            int(price) if isinstance(price, (int, float)) else None,
        ),
    )


def _beep() -> None:
    try:
        import winsound  # type: ignore

        winsound.Beep(880, 300)
    except Exception:
        pass


def _watch_cycle(args, conn: sqlite3.Connection) -> tuple[int, list[dict]]:
    """Fetch pages newest-first; return (checked, new_raw_items)."""
    limit = args.limit
    if limit > LIMIT_CAP:
        limit = LIMIT_CAP
    checked = 0
    new_items: list[dict] = []
    new_ids: set[str] = set()

    for page in range(1, args.max_pages + 1):
        params = build_search_params(args, page)
        params["limit"] = limit
        params["sortBy"] = "published_at"
        params["sortDirection"] = "desc"
        data = api_get("/api/v1/client/listings/room-rentals", params, args.timeout)
        items = data.get("items") or []
        if not items:
            break
        for item in items:
            if not isinstance(item, dict):
                continue
            listing_id = item.get("id")
            if not listing_id:
                continue
            checked += 1
            if listing_id in new_ids:
                continue
            if not _id_seen(conn, listing_id):
                new_ids.add(listing_id)
                new_items.append(item)
        if len(items) < limit:
            break
        if page < args.max_pages and args.delay > 0:
            time.sleep(args.delay)
    return checked, new_items


def _print_new_block(new_records: list[dict]) -> None:
    banner = "=" * 24 + " NEW LISTINGS " + "=" * 24
    print(banner)
    _render_table(SEARCH_COLUMNS, [_search_row(r) for r in new_records], sys.stdout)
    print("=" * len(banner))


def cmd_watch(args) -> int:
    if args.interval < 30:
        print("error: --interval minimum is 30 seconds", file=sys.stderr)
        return 2

    conn = _open_db(args.db)
    try:
        first_run = (
            conn.execute("SELECT COUNT(*) FROM seen").fetchone()[0] == 0
            and _get_meta(conn, "initialized") is None
        )

        while True:
            started = time.monotonic()
            checked, new_items = _watch_cycle(args, conn)

            if first_run:
                for item in new_items:
                    rec = normalize(item)
                    _insert_seen(conn, rec["id"], rec.get("title"), rec.get("price"))
                conn.commit()
                _set_meta(conn, "initialized", "1")
                _set_meta(conn, "last_run", datetime.now(timezone.utc).isoformat())
                conn.commit()
                print(f"Seeded {len(new_items)} listings (no alerts on first run)")
                first_run = False
            else:
                new_records = [normalize(item) for item in new_items]
                if new_records:
                    _print_new_block(new_records)
                    for rec in new_records:
                        _insert_seen(conn, rec["id"], rec.get("title"), rec.get("price"))
                    conn.commit()
                    if args.beep:
                        _beep()
                else:
                    elapsed = time.monotonic() - started
                    print(
                        f"No new listings (checked {checked}, {_fmt_duration(elapsed)})"
                    )
                _set_meta(conn, "last_run", datetime.now(timezone.utc).isoformat())
                conn.commit()

            if args.once:
                break
            time.sleep(args.interval)
        return 0
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Argument parsing / main
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ibilik.py",
        description="Search and watch ibilik.com room rentals (public API).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # locations
    loc = sub.add_parser("locations", help="resolve place names to location codes")
    loc.add_argument("query", help="place name to search for")
    loc.add_argument("--limit", type=int, default=10)
    loc.add_argument("--types", metavar="LIST", help="comma list, e.g. ROOM")
    loc.add_argument("--output", choices=["table", "json"], default="table")
    loc.add_argument("--out", metavar="FILE", help="write output to FILE")
    loc.add_argument("--timeout", type=float, default=30.0)
    loc.add_argument("--delay", type=float, default=0.5)
    loc.set_defaults(func=cmd_locations)

    # search
    search = sub.add_parser("search", help="search room rentals")
    add_filter_args(search)
    search.add_argument("--limit", type=int, default=100, help="per page (cap 5000)")
    search.add_argument(
        "--pages", type=int, default=1, help="pages to fetch; 0 = follow all"
    )
    search.add_argument(
        "--output", choices=["table", "json", "jsonl", "csv"], default="table"
    )
    search.add_argument("--out", metavar="FILE", help="write output to FILE")
    search.add_argument(
        "--raw", action="store_true",
        help="emit raw API items (only with --output json/jsonl)",
    )
    search.add_argument("--delay", type=float, default=0.5)
    search.add_argument("--timeout", type=float, default=30.0)
    search.set_defaults(func=cmd_search)

    # watch
    watch = sub.add_parser("watch", help="poll for newly published listings")
    add_filter_args(watch)
    watch.add_argument("--limit", type=int, default=100, help="per page (cap 5000)")
    watch.add_argument("--max-pages", type=int, default=3)
    watch.add_argument("--interval", type=float, default=90.0, help="seconds (min 30)")
    watch.add_argument("--db", metavar="PATH", help="SQLite DB path")
    watch.add_argument("--once", action="store_true", help="run one cycle and exit")
    watch.add_argument("--beep", action="store_true", help="beep on new listings (Windows)")
    watch.add_argument("--delay", type=float, default=0.5)
    watch.add_argument("--timeout", type=float, default=30.0)
    watch.set_defaults(func=cmd_watch)

    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if getattr(args, "db", None) is None:
        args.db = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "ibilik_watch.db"
        )

    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    except urllib.error.URLError as err:
        print(f"network error: {err}", file=sys.stderr)
        return 1
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    sys.exit(main())
