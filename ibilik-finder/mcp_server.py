#!/usr/bin/env python3
"""Local stdio MCP server exposing iBilik room search as structured tools.

Reuses the HTTP and normalisation logic in ``ibilik.py`` (no second API
client).  Intended to be launched by an MCP-capable client over stdio:

    py mcp_server.py

Only ``mcp_server.py`` writes to stdout, and only via the MCP protocol.
Diagnostics from the underlying API client go to stderr.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Literal

from fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

import ibilik

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SEARCH_PATH = "/api/v1/client/listings/room-rentals"
LOCATION_PATH = "/api/v1/client/location-search"

DEFAULT_TIMEOUT = 30.0
PAGE_DELAY = 0.5

RoomType = Literal["MASTER", "GUEST", "MIDDLE", "STUDIO", "SINGLE", "SUITE"]
BathroomType = Literal["PRIVATE", "SHARED"]
UnitType = Literal[
    "STUDIO", "DUPLEX", "TRIPLEX", "CORNER_LOT", "END_LOT",
    "INTERMEDIATE", "LOFT", "DUAL_KEY", "SOHO", "PENTHOUSE",
]
FloorLevel = Literal["GROUND", "LOW", "MID", "HIGH", "PENTHOUSE"]
SortBy = Literal["published_at", "price"]
SortDirection = Literal["asc", "desc"]

ROOM_TYPES = frozenset({"MASTER", "GUEST", "MIDDLE", "STUDIO", "SINGLE", "SUITE"})
BATHROOM_TYPES = frozenset({"PRIVATE", "SHARED"})
UNIT_TYPES = frozenset({
    "STUDIO", "DUPLEX", "TRIPLEX", "CORNER_LOT", "END_LOT",
    "INTERMEDIATE", "LOFT", "DUAL_KEY", "SOHO", "PENTHOUSE",
})
FLOOR_LEVELS = frozenset({"GROUND", "LOW", "MID", "HIGH", "PENTHOUSE"})
SORT_FIELDS = frozenset({"published_at", "price"})
SORT_DIRECTIONS = frozenset({"asc", "desc"})

PROFILE_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
MAX_AMENITIES = 20

READ_ONLY_ANNOTATIONS = ToolAnnotations(
    read_only_hint=True,
    idempotent_hint=True,
    open_world_hint=True,
)
STATEFUL_ANNOTATIONS = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
    open_world_hint=True,
)
RESET_ANNOTATIONS = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=True,
    idempotent_hint=True,
    open_world_hint=False,
)

DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS profiles (
    profile        TEXT PRIMARY KEY,
    filters_json   TEXT NOT NULL,
    initialized_at TEXT,
    last_run       TEXT
);
CREATE TABLE IF NOT EXISTS seen (
    profile    TEXT NOT NULL,
    listing_id TEXT NOT NULL,
    first_seen TEXT,
    title      TEXT,
    price      INT,
    PRIMARY KEY (profile, listing_id)
);
CREATE INDEX IF NOT EXISTS idx_seen_profile ON seen(profile);
"""

# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

mcp = FastMCP(
    name="iBilik Room Finder",
    instructions=(
        "Search Malaysian room rentals listed on ibilik.com. "
        "Location inputs (state/city/area) are iBilik location CODES, not free "
        "text - resolve them first with search_locations. Prices are monthly "
        "MYR unless a listing's charge_unit says otherwise. Results include "
        "public contact information (phone/WhatsApp) belonging to real people: "
        "handle it responsibly, do not republish or bulk-collect it. "
        "search_rooms and search_locations are read-only; check_new_rooms and "
        "reset_room_watch change local watch state."
    ),
)

# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


def _clean_optional_str(value, name: str):
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string or null")
    value = value.strip()
    return value or None


def _check_int(name: str, value, lo: int, hi: int):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if value < lo or value > hi:
        raise ValueError(f"{name} must be between {lo} and {hi}")
    return value


def _check_required_int(name: str, value, lo: int, hi: int) -> int:
    checked = _check_int(name, value, lo, hi)
    if checked is None:
        raise ValueError(f"{name} is required")
    return checked


def _check_choice(name: str, value, allowed: frozenset):
    if value is None:
        return None
    if value not in allowed:
        options = ", ".join(sorted(allowed))
        raise ValueError(f"{name} must be one of: {options}")
    return value


def _clean_amenities(values):
    if not values:
        return None
    if len(values) > MAX_AMENITIES:
        raise ValueError(f"amenities accepts at most {MAX_AMENITIES} values")
    cleaned: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            raise ValueError("amenities must be a list of strings")
        value = value.strip()
        if value and value not in seen:
            seen.add(value)
            cleaned.append(value)
    return cleaned or None


def _validate_profile(profile) -> str:
    if not isinstance(profile, str) or not PROFILE_RE.match(profile):
        raise ValueError(
            "profile must match [A-Za-z0-9_-]{1,64}"
        )
    return profile


def _validate_filters(
    query, state, city, area,
    min_price, max_price,
    room_type, bathroom_type, unit_type, floor_level,
    min_sqft, max_sqft, kyc_verified, amenities,
) -> dict:
    query = _clean_optional_str(query, "query")
    state = _clean_optional_str(state, "state")
    city = _clean_optional_str(city, "city")
    area = _clean_optional_str(area, "area")

    min_price = _check_int("min_price", min_price, 0, 10_000_000)
    max_price = _check_int("max_price", max_price, 0, 10_000_000)
    min_sqft = _check_int("min_sqft", min_sqft, 0, 10_000_000)
    max_sqft = _check_int("max_sqft", max_sqft, 0, 10_000_000)
    if min_price is not None and max_price is not None and min_price > max_price:
        raise ValueError("min_price must be <= max_price")
    if min_sqft is not None and max_sqft is not None and min_sqft > max_sqft:
        raise ValueError("min_sqft must be <= max_sqft")

    room_type = _check_choice("room_type", room_type, ROOM_TYPES)
    bathroom_type = _check_choice("bathroom_type", bathroom_type, BATHROOM_TYPES)
    unit_type = _check_choice("unit_type", unit_type, UNIT_TYPES)
    floor_level = _check_choice("floor_level", floor_level, FLOOR_LEVELS)

    if kyc_verified is not None and not isinstance(kyc_verified, bool):
        raise ValueError("kyc_verified must be a boolean or null")

    return {
        "query": query,
        "state": state,
        "city": city,
        "area": area,
        "min_price": min_price,
        "max_price": max_price,
        "room_type": room_type,
        "bathroom_type": bathroom_type,
        "unit_type": unit_type,
        "floor_level": floor_level,
        "min_sqft": min_sqft,
        "max_sqft": max_sqft,
        "kyc_verified": kyc_verified,
        "amenities": _clean_amenities(amenities),
    }


def _filters_to_params(filters: dict, *, limit: int, page: int,
                       sort_by: str, sort_direction: str) -> dict:
    params: dict = {
        "limit": limit,
        "page": page,
        "sortBy": sort_by,
        "sortDirection": sort_direction,
    }
    if filters.get("query"):
        params["filter[search]"] = filters["query"]
    if filters.get("state"):
        params["filter[state]"] = filters["state"]
    if filters.get("city"):
        params["filter[city]"] = filters["city"]
    if filters.get("area"):
        params["filter[area]"] = filters["area"]
    if filters.get("min_price") is not None:
        params["filter[minPrice]"] = filters["min_price"]
    if filters.get("max_price") is not None:
        params["filter[maxPrice]"] = filters["max_price"]
    if filters.get("room_type"):
        params["filter[roomType]"] = filters["room_type"]
    if filters.get("bathroom_type"):
        params["filter[bathroomType]"] = filters["bathroom_type"]
    if filters.get("unit_type"):
        params["filter[unitType]"] = filters["unit_type"]
    if filters.get("floor_level"):
        params["filter[floorLevel]"] = filters["floor_level"]
    if filters.get("min_sqft") is not None:
        params["filter[minSpaceSqft]"] = filters["min_sqft"]
    if filters.get("max_sqft") is not None:
        params["filter[maxSpaceSqft]"] = filters["max_sqft"]
    if filters.get("kyc_verified") is not None:
        params["filter[kycVerified]"] = "true" if filters["kyc_verified"] else "false"
    if filters.get("amenities"):
        params["filter[amenities]"] = list(filters["amenities"])
    return params


# ---------------------------------------------------------------------------
# API access (turns any failure into a normal tool error)
# ---------------------------------------------------------------------------


def _api(path: str, params: dict, timeout: float = DEFAULT_TIMEOUT) -> dict:
    try:
        return ibilik.request_json(path, params, timeout)
    except ibilik.ValidationError as err:
        raise ValueError(err.format()) from None
    except json.JSONDecodeError as err:
        raise ValueError(f"iBilik returned malformed JSON: {err}") from None
    except urllib.error.HTTPError as err:
        raise ValueError(f"iBilik API HTTP error {err.code}: {err.reason}") from None
    except urllib.error.URLError as err:
        raise ValueError(f"iBilik API network error: {err.reason}") from None
    except (TimeoutError, OSError) as err:
        raise ValueError(f"iBilik API request failed: {err}") from None
    except SystemExit:  # defensive: never let a tool kill the MCP process
        raise ValueError("iBilik API rejected the request.") from None


# ---------------------------------------------------------------------------
# Result enrichment
# ---------------------------------------------------------------------------


def _description_excerpt(item: dict, chars: int):
    if not chars or chars <= 0:
        return None
    locales = item.get("locales")
    if not isinstance(locales, dict):
        return None
    en = locales.get("en-US")
    if not isinstance(en, dict):
        return None
    text = en.get("description")
    if not isinstance(text, str):
        return None
    text = " ".join(text.split())
    if not text:
        return None
    if len(text) > chars:
        if chars <= 3:
            text = text[:chars]
        else:
            text = text[: chars - 3].rstrip() + "..."
    return text


def _cover_image_url(item: dict):
    images = item.get("images")
    if not isinstance(images, list):
        return None
    first_url = None
    for image in images:
        if not isinstance(image, dict):
            continue
        url = image.get("url")
        if not url:
            continue
        if first_url is None:
            first_url = url
        if image.get("isCover"):
            return url
    return first_url


def _enrich(item: dict, include_description: bool, description_chars: int) -> dict:
    record = ibilik.normalize(item)
    if include_description:
        excerpt = _description_excerpt(item, description_chars)
        if excerpt is not None:
            record["description_excerpt"] = excerpt
    cover = _cover_image_url(item)
    if cover is not None:
        record["cover_image_url"] = cover
    return record


# ---------------------------------------------------------------------------
# Tool 1: search_rooms
# ---------------------------------------------------------------------------


@mcp.tool(
    annotations=READ_ONLY_ANNOTATIONS,
    description=(
        "Search iBilik room-rental listings. Read-only. Returns normalised "
        "records for a single API page plus pagination metadata. Use "
        "search_locations first to obtain valid state/city/area codes. "
        "amenities uses the site's AND/intersection semantics."
    ),
)
def search_rooms(
    query: Annotated[
        str | None,
        Field(description="Free-text search, e.g. 'sunway university'. Optional."),
    ] = None,
    state: Annotated[
        str | None,
        Field(description="iBilik state code, e.g. 'selangor'. Optional."),
    ] = None,
    city: Annotated[
        str | None,
        Field(description="iBilik city code, e.g. 'cheras'. Optional."),
    ] = None,
    area: Annotated[
        str | None,
        Field(description="iBilik area code, e.g. 'bandar-mahkota-cheras'. Optional."),
    ] = None,
    min_price: Annotated[
        int | None,
        Field(description="Minimum monthly price in MYR (>=0). Optional."),
    ] = None,
    max_price: Annotated[
        int | None,
        Field(description="Maximum monthly price in MYR (>=0). Optional."),
    ] = None,
    room_type: Annotated[
        RoomType | None,
        Field(description="Room type filter (e.g. MASTER, SINGLE). Optional."),
    ] = None,
    bathroom_type: Annotated[
        BathroomType | None,
        Field(description="Bathroom type: PRIVATE or SHARED. Optional."),
    ] = None,
    unit_type: Annotated[
        UnitType | None,
        Field(description="Unit type filter (e.g. STUDIO, LOFT). Optional."),
    ] = None,
    floor_level: Annotated[
        FloorLevel | None,
        Field(description="Floor level filter (e.g. LOW, HIGH). Optional."),
    ] = None,
    min_sqft: Annotated[
        int | None,
        Field(description="Minimum space in square feet (>=0). Optional."),
    ] = None,
    max_sqft: Annotated[
        int | None,
        Field(description="Maximum space in square feet (>=0). Optional."),
    ] = None,
    kyc_verified: Annotated[
        bool | None,
        Field(description="true = only KYC-verified listings; null = no filter."),
    ] = None,
    amenities: Annotated[
        list[str] | None,
        Field(description="Amenity codes; all must be present (AND). Max 20."),
    ] = None,
    page: Annotated[
        int,
        Field(description="1-based page number (>=1). Default 1."),
    ] = 1,
    limit: Annotated[
        int,
        Field(description="Results per page, 1..100. Capped for context safety."),
    ] = 10,
    sort_by: Annotated[
        SortBy,
        Field(description="Sort field: published_at or price."),
    ] = "published_at",
    sort_direction: Annotated[
        SortDirection,
        Field(description="Sort direction: asc or desc."),
    ] = "desc",
    include_description: Annotated[
        bool,
        Field(description="Include a collapsed description excerpt per listing."),
    ] = True,
    description_chars: Annotated[
        int,
        Field(description="Max description excerpt length, 0..2000 (0 omits)."),
    ] = 600,
) -> dict:
    page = _check_required_int("page", page, 1, 1_000_000)
    limit = _check_required_int("limit", limit, 1, 100)
    description_chars = _check_required_int("description_chars", description_chars, 0, 2000)
    sort_by = _check_choice("sort_by", sort_by, SORT_FIELDS) or "published_at"
    sort_direction = _check_choice("sort_direction", sort_direction, SORT_DIRECTIONS) or "desc"

    filters = _validate_filters(
        query, state, city, area,
        min_price, max_price,
        room_type, bathroom_type, unit_type, floor_level,
        min_sqft, max_sqft, kyc_verified, amenities,
    )
    params = _filters_to_params(
        filters, limit=limit, page=page,
        sort_by=sort_by, sort_direction=sort_direction,
    )
    data = _api(SEARCH_PATH, params)
    raw_items = data.get("items") or []
    pagination = data.get("pagination") or {}
    total_pages = pagination.get("totalPages", 0) or 0

    items = [
        _enrich(item, include_description, description_chars)
        for item in raw_items
        if isinstance(item, dict)
    ]
    return {
        "items": items,
        "pagination": {
            "page": pagination.get("page", page),
            "limit": pagination.get("limit", limit),
            "total_items": pagination.get("totalItems", 0),
            "total_pages": total_pages,
            "has_next": page < total_pages,
        },
    }


# ---------------------------------------------------------------------------
# Tool 2: search_locations
# ---------------------------------------------------------------------------


@mcp.tool(
    annotations=READ_ONLY_ANNOTATIONS,
    description=(
        "Resolve a place name to iBilik location codes. Read-only. Use the "
        "returned codes (state/city/area) with search_rooms."
    ),
)
def search_locations(
    query: Annotated[
        str,
        Field(description="Place name to resolve, e.g. 'cheras' (2..100 chars)."),
    ],
    limit: Annotated[
        int,
        Field(description="Max results, 1..50. Default 10."),
    ] = 10,
    listing_types: Annotated[
        list[str] | None,
        Field(description="Listing types to match (default ['ROOM']); max 10."),
    ] = None,
) -> dict:
    cleaned = _clean_optional_str(query, "query")
    if cleaned is None or not (2 <= len(cleaned) <= 100):
        raise ValueError("query must be 2..100 characters after trimming")
    limit = _check_required_int("limit", limit, 1, 50)

    if listing_types is None:
        types = ["ROOM"]
    else:
        if len(listing_types) > 10:
            raise ValueError("listing_types accepts at most 10 values")
        types = []
        seen = set()
        for value in listing_types:
            if not isinstance(value, str):
                raise ValueError("listing_types must be a list of strings")
            value = value.strip().upper()
            if value and value not in seen:
                seen.add(value)
                types.append(value)
        if not types:
            types = ["ROOM"]

    params: dict = {"query": cleaned, "limit": limit}
    params["filter[types]"] = list(types)

    data = _api(LOCATION_PATH, params)
    items = []
    for item in data.get("items") or []:
        if not isinstance(item, dict):
            continue
        items.append({
            "id": item.get("id"),
            "code": item.get("code"),
            "name": item.get("name"),
            "type": item.get("type"),
        })
    return {"items": items, "count": len(items)}


# ---------------------------------------------------------------------------
# Watch state (tools 3 and 4)
# ---------------------------------------------------------------------------


def _db_path() -> str:
    override = os.environ.get("IBILIK_MCP_DB")
    if override:
        return override
    return str(Path(__file__).resolve().with_name(".ibilik_mcp_watch.db"))


def _open_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(DB_SCHEMA)
    conn.commit()
    return conn


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fetch_newest(filters: dict, limit: int, max_pages: int):
    """Fetch newest-first pages, deduped by listing id."""
    items: list[dict] = []
    seen_ids: set[str] = set()
    for page in range(1, max_pages + 1):
        params = _filters_to_params(
            filters, limit=limit, page=page,
            sort_by="published_at", sort_direction="desc",
        )
        data = _api(SEARCH_PATH, params)
        page_items = data.get("items") or []
        for item in page_items:
            if not isinstance(item, dict):
                continue
            listing_id = item.get("id")
            if listing_id and listing_id not in seen_ids:
                seen_ids.add(listing_id)
                items.append(item)
        if len(page_items) < limit:
            break
        if page < max_pages:
            time.sleep(PAGE_DELAY)
    return items


@mcp.tool(
    annotations=STATEFUL_ANNOTATIONS,
    description=(
        "Check for newly published iBilik listings for a saved search profile. "
        "State-changing (writes to a local SQLite DB). The first call for a "
        "profile seeds it and returns no new items; later calls with the SAME "
        "filters return only listings not seen before. One-shot: it never "
        "loops or sleeps beyond 0.5s between pages. Changing a profile's "
        "filters is rejected - use a different profile or reset_room_watch."
    ),
)
def check_new_rooms(
    profile: Annotated[
        str,
        Field(description="Watch profile name, [A-Za-z0-9_-]{1,64}. Default 'default'."),
    ] = "default",
    query: Annotated[str | None, Field(description="Free-text search. Optional.")] = None,
    state: Annotated[str | None, Field(description="iBilik state code. Optional.")] = None,
    city: Annotated[str | None, Field(description="iBilik city code. Optional.")] = None,
    area: Annotated[str | None, Field(description="iBilik area code. Optional.")] = None,
    min_price: Annotated[int | None, Field(description="Minimum monthly MYR (>=0).")] = None,
    max_price: Annotated[int | None, Field(description="Maximum monthly MYR (>=0).")] = None,
    room_type: Annotated[RoomType | None, Field(description="Room type filter.")] = None,
    bathroom_type: Annotated[BathroomType | None, Field(description="Bathroom type filter.")] = None,
    unit_type: Annotated[UnitType | None, Field(description="Unit type filter.")] = None,
    floor_level: Annotated[FloorLevel | None, Field(description="Floor level filter.")] = None,
    min_sqft: Annotated[int | None, Field(description="Minimum sqft (>=0).")] = None,
    max_sqft: Annotated[int | None, Field(description="Maximum sqft (>=0).")] = None,
    kyc_verified: Annotated[bool | None, Field(description="Only KYC-verified when true.")] = None,
    amenities: Annotated[
        list[str] | None,
        Field(description="Amenity codes; all must be present (AND). Max 20."),
    ] = None,
    limit: Annotated[
        int,
        Field(description="Listings per page, 1..100. Default 100."),
    ] = 100,
    max_pages: Annotated[
        int,
        Field(description="Pages to fetch per check, 1..10. Default 3."),
    ] = 3,
    description_chars: Annotated[
        int,
        Field(description="Description excerpt length for new items, 0..1000."),
    ] = 300,
) -> dict:
    profile = _validate_profile(profile)
    limit = _check_required_int("limit", limit, 1, 100)
    max_pages = _check_required_int("max_pages", max_pages, 1, 10)
    description_chars = _check_required_int("description_chars", description_chars, 0, 1000)

    filters = _validate_filters(
        query, state, city, area,
        min_price, max_price,
        room_type, bathroom_type, unit_type, floor_level,
        min_sqft, max_sqft, kyc_verified, amenities,
    )
    canonical = json.dumps(filters, sort_keys=True, ensure_ascii=False)

    conn = _open_db(_db_path())
    try:
        row = conn.execute(
            "SELECT filters_json, initialized_at FROM profiles WHERE profile = ?",
            (profile,),
        ).fetchone()
        if row is not None and row[0] != canonical:
            raise ValueError(
                f"Profile '{profile}' already watches different filters. "
                "Use a different profile name or call reset_room_watch first."
            )
        first_run = row is None

        raw_items = _fetch_newest(filters, limit, max_pages)
        checked_count = len(raw_items)

        if first_run:
            with conn:
                for item in raw_items:
                    record = ibilik.normalize(item)
                    conn.execute(
                        "INSERT OR IGNORE INTO seen"
                        "(profile, listing_id, first_seen, title, price) "
                        "VALUES(?, ?, ?, ?, ?)",
                        (
                            profile,
                            record.get("id"),
                            _now_iso(),
                            record.get("title"),
                            int(record["price"])
                            if isinstance(record.get("price"), (int, float))
                            else None,
                        ),
                    )
                conn.execute(
                    "INSERT INTO profiles(profile, filters_json, initialized_at, last_run) "
                    "VALUES(?, ?, ?, ?)",
                    (profile, canonical, _now_iso(), _now_iso()),
                )
            return {
                "status": "seeded",
                "profile": profile,
                "seeded_count": checked_count,
                "new_items": [],
                "filters": filters,
                "last_run": _now_iso(),
            }

        new_raw = [
            item for item in raw_items
            if not conn.execute(
                "SELECT 1 FROM seen WHERE profile = ? AND listing_id = ?",
                (profile, item.get("id")),
            ).fetchone()
        ]
        new_items = [
            _enrich(item, True, description_chars) for item in new_raw
        ]

        with conn:
            for item in new_raw:
                record = ibilik.normalize(item)
                conn.execute(
                    "INSERT OR IGNORE INTO seen"
                    "(profile, listing_id, first_seen, title, price) "
                    "VALUES(?, ?, ?, ?, ?)",
                    (
                        profile,
                        record.get("id"),
                        _now_iso(),
                        record.get("title"),
                        int(record["price"])
                        if isinstance(record.get("price"), (int, float))
                        else None,
                    ),
                )
            last_run = _now_iso()
            conn.execute(
                "UPDATE profiles SET last_run = ? WHERE profile = ?",
                (last_run, profile),
            )
        return {
            "status": "checked",
            "profile": profile,
            "checked_count": checked_count,
            "new_count": len(new_items),
            "new_items": new_items,
            "last_run": last_run,
            "filters": filters,
        }
    finally:
        conn.close()


@mcp.tool(
    annotations=RESET_ANNOTATIONS,
    description=(
        "Delete a saved watch profile's state (seen listings and metadata). "
        "State-changing: after this, the next check_new_rooms call for the "
        "profile seeds again and reports no new items."
    ),
)
def reset_room_watch(
    profile: Annotated[
        str,
        Field(description="Watch profile name to reset, [A-Za-z0-9_-]{1,64}."),
    ] = "default",
) -> dict:
    profile = _validate_profile(profile)
    conn = _open_db(_db_path())
    try:
        with conn:
            deleted = conn.execute(
                "DELETE FROM seen WHERE profile = ?", (profile,)
            ).rowcount
            conn.execute("DELETE FROM profiles WHERE profile = ?", (profile,))
        return {"profile": profile, "reset": True, "deleted_seen": deleted}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run(transport="stdio", show_banner=False)
