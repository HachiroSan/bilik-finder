#!/usr/bin/env python3
"""Local stdio MCP server for permission-gated Mudah rental search."""

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

import mudah


RentalType = Literal[
    "room",
    "property",
    "apartment_condo",
    "house",
    "commercial",
]
Furnishing = Literal["fully", "partial", "none"]
SellerType = Literal["any", "private", "agent"]
SortBy = Literal["latest", "price_asc", "price_desc"]

PAGE_DELAY_SECONDS = 2.0
PROFILE_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

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
    price      REAL,
    PRIMARY KEY (profile, listing_id)
);
CREATE INDEX IF NOT EXISTS idx_mudah_seen_profile ON seen(profile);
"""


mcp = FastMCP(
    name="Mudah Rental Finder",
    instructions=(
        "Search Malaysian room and property rentals listed on mudah.my. "
        "Mudah's robots.txt states that automated access requires permission; "
        "network calls are disabled unless MUDAH_AUTOMATION_ALLOWED=1 is set "
        "after authorisation has been obtained. The server does not bypass "
        "challenges or access controls. Resolve place names with "
        "search_locations, then pass the returned location_slug to "
        "search_rentals. Results can include public phone/WhatsApp information "
        "belonging to real people; use it only for personal rental enquiries, "
        "never republish or bulk-collect it. check_new_rentals and "
        "reset_rental_watch write local SQLite watch state."
    ),
)


def _check_int(name: str, value, minimum: int, maximum: int):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if value < minimum or value > maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _check_required_int(name: str, value, minimum: int, maximum: int) -> int:
    checked = _check_int(name, value, minimum, maximum)
    if checked is None:
        raise ValueError(f"{name} is required")
    return checked


def _clean_optional_text(value, name: str, maximum: int = 200):
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string or null")
    cleaned = value.strip()
    if not cleaned:
        return None
    if len(cleaned) > maximum:
        raise ValueError(f"{name} must be at most {maximum} characters")
    return cleaned


def _validate_profile(profile) -> str:
    if not isinstance(profile, str) or not PROFILE_RE.fullmatch(profile):
        raise ValueError("profile must match [A-Za-z0-9_-]{1,64}")
    return profile


def _validated_filters(
    location,
    rental_type,
    query,
    min_price,
    max_price,
    bedrooms,
    bathrooms,
    min_sqft,
    max_sqft,
    furnishing,
    seller_type,
) -> dict:
    location = _clean_optional_text(location, "location", 160) or "malaysia"
    location = location.lower()
    query = _clean_optional_text(query, "query", 100)
    min_price = _check_int("min_price", min_price, 0, 10_000_000)
    max_price = _check_int("max_price", max_price, 0, 10_000_000)
    bedrooms = _check_int("bedrooms", bedrooms, 1, 5)
    bathrooms = _check_int("bathrooms", bathrooms, 1, 5)
    min_sqft = _check_int("min_sqft", min_sqft, 0, 10_000_000)
    max_sqft = _check_int("max_sqft", max_sqft, 0, 10_000_000)

    # Reuse the client as the authoritative validator for slugs, enums, and
    # range relationships. This does not perform a network request.
    mudah.build_search_request(
        location=location,
        rental_type=rental_type,
        query=query,
        min_price=min_price,
        max_price=max_price,
        bedrooms=bedrooms,
        bathrooms=bathrooms,
        min_sqft=min_sqft,
        max_sqft=max_sqft,
        furnishing=furnishing,
    )
    if seller_type not in mudah.SELLER_TYPES:
        raise ValueError("seller_type must be one of: any, private, agent")

    return {
        "location": location,
        "rental_type": rental_type,
        "query": query,
        "min_price": min_price,
        "max_price": max_price,
        "bedrooms": bedrooms,
        "bathrooms": bathrooms,
        "min_sqft": min_sqft,
        "max_sqft": max_sqft,
        "furnishing": furnishing,
        "seller_type": seller_type,
    }


def _mudah_call(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except mudah.MudahError as err:
        raise ValueError(str(err)) from None
    except urllib.error.HTTPError as err:
        raise ValueError(f"Mudah HTTP error {err.code}: {err.reason}") from None
    except urllib.error.URLError as err:
        raise ValueError(f"Mudah network error: {err.reason}") from None
    except (TimeoutError, OSError) as err:
        raise ValueError(f"Mudah request failed: {err}") from None


@mcp.tool(
    annotations=READ_ONLY_ANNOTATIONS,
    description=(
        "Search one Mudah rental result page. Read-only. Location is a Mudah "
        "location_slug returned by search_locations, such as 'kuala-lumpur' "
        "or 'kuala-lumpur-cheras'. Price/size/bed/bath/furnishing/query/sort "
        "filters are sent to Mudah. The site returns up to 40 organic ads per "
        "page; limit only caps MCP output. seller_type is applied locally so "
        "server totals do not reflect it. Requires authorised automation and "
        "MUDAH_AUTOMATION_ALLOWED=1."
    ),
)
def search_rentals(
    location: Annotated[
        str,
        Field(
            description=(
                "Mudah location slug, e.g. 'malaysia', 'kuala-lumpur', or "
                "'kuala-lumpur-cheras'. Resolve with search_locations."
            )
        ),
    ] = "malaysia",
    rental_type: Annotated[
        RentalType,
        Field(
            description=(
                "Rental category: room, property (all), apartment_condo, "
                "house, or commercial."
            )
        ),
    ] = "room",
    query: Annotated[
        str | None,
        Field(description="Free-text keyword search, max 100 characters."),
    ] = None,
    min_price: Annotated[
        int | None,
        Field(description="Minimum monthly rent in MYR (>=0)."),
    ] = None,
    max_price: Annotated[
        int | None,
        Field(description="Maximum monthly rent in MYR (>=0)."),
    ] = None,
    bedrooms: Annotated[
        int | None,
        Field(description="Exact bedroom bucket: 1..4, or 5 for 5+."),
    ] = None,
    bathrooms: Annotated[
        int | None,
        Field(description="Exact bathroom bucket: 1..4, or 5 for 5+."),
    ] = None,
    min_sqft: Annotated[
        int | None,
        Field(description="Minimum unit size in square feet."),
    ] = None,
    max_sqft: Annotated[
        int | None,
        Field(description="Maximum unit size in square feet."),
    ] = None,
    furnishing: Annotated[
        Furnishing | None,
        Field(description="Furnishing: fully, partial, none, or null."),
    ] = None,
    seller_type: Annotated[
        SellerType,
        Field(
            description=(
                "any, private, or agent. Applied locally to the fetched page."
            )
        ),
    ] = "any",
    page: Annotated[
        int,
        Field(description="1-based page number. Mudah uses ?o=N after page 1."),
    ] = 1,
    limit: Annotated[
        int,
        Field(description="Maximum returned records, 1..40. Default 10."),
    ] = 10,
    sort_by: Annotated[
        SortBy,
        Field(description="latest, price_asc, or price_desc."),
    ] = "latest",
    include_featured: Annotated[
        bool,
        Field(description="Include promoted featured ads before organic results."),
    ] = False,
) -> dict:
    page = _check_required_int("page", page, 1, 1_000_000)
    limit = _check_required_int("limit", limit, 1, mudah.SITE_PAGE_SIZE)
    filters = _validated_filters(
        location,
        rental_type,
        query,
        min_price,
        max_price,
        bedrooms,
        bathrooms,
        min_sqft,
        max_sqft,
        furnishing,
        seller_type,
    )
    result = _mudah_call(
        mudah.search_rentals,
        **filters,
        page=page,
        sort_by=sort_by,
        include_featured=include_featured,
    )
    result["items"] = result["items"][:limit]
    result["pagination"]["returned"] = len(result["items"])
    result["filters"] = {
        **filters,
        "sort_by": sort_by,
        "include_featured": include_featured,
    }
    return result


@mcp.tool(
    annotations=READ_ONLY_ANNOTATIONS,
    description=(
        "Resolve a Malaysian place name to Mudah location slugs using the "
        "state/subarea directory embedded in Mudah's search page. Read-only. "
        "Use location_slug with search_rentals. Requires authorised automation "
        "and MUDAH_AUTOMATION_ALLOWED=1."
    ),
)
def search_locations(
    query: Annotated[
        str,
        Field(description="State or area name, e.g. 'cheras' (1..100 chars)."),
    ],
    state: Annotated[
        str | None,
        Field(
            description=(
                "Optional state name/slug to disambiguate, e.g. 'kuala-lumpur'."
            )
        ),
    ] = None,
    rental_type: Annotated[
        RentalType,
        Field(description="Category used to build each result URL."),
    ] = "room",
    limit: Annotated[
        int,
        Field(description="Maximum matches, 1..50. Default 10."),
    ] = 10,
) -> dict:
    cleaned = _clean_optional_text(query, "query", 100)
    if cleaned is None:
        raise ValueError("query is required")
    limit = _check_required_int("limit", limit, 1, 50)
    items = _mudah_call(
        mudah.search_locations,
        cleaned,
        state=state,
        rental_type=rental_type,
        limit=limit,
    )
    return {"items": items, "count": len(items)}


def _db_path() -> str:
    override = os.environ.get("MUDAH_MCP_DB")
    if override:
        return override
    return str(Path(__file__).resolve().with_name(".mudah_mcp_watch.db"))


def _open_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(DB_SCHEMA)
    conn.commit()
    return conn


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fetch_newest(filters: dict, max_pages: int) -> tuple[list[dict], int]:
    items: list[dict] = []
    seen_ids: set[str] = set()
    pages_fetched = 0
    for page in range(1, max_pages + 1):
        result = _mudah_call(
            mudah.search_rentals,
            **filters,
            page=page,
            sort_by="latest",
            include_featured=False,
        )
        pages_fetched += 1
        page_items = result.get("items") or []
        for item in page_items:
            if not isinstance(item, dict):
                continue
            listing_id = item.get("id")
            if listing_id and listing_id not in seen_ids:
                seen_ids.add(listing_id)
                items.append(item)

        pagination = result.get("pagination") or {}
        if not pagination.get("has_next"):
            break
        if (pagination.get("organic_items_on_page") or 0) < mudah.SITE_PAGE_SIZE:
            break
        if page < max_pages:
            time.sleep(PAGE_DELAY_SECONDS)
    return items, pages_fetched


@mcp.tool(
    annotations=STATEFUL_ANNOTATIONS,
    description=(
        "Check for newly listed Mudah rentals for a saved search profile. "
        "State-changing: writes local SQLite state. The first call seeds the "
        "profile and returns no alerts; later calls with the same filters "
        "return unseen listing IDs. One-shot only, newest-first, at most five "
        "pages, with a two-second delay between pages. Changing a profile's "
        "filters is rejected. Requires authorised automation and "
        "MUDAH_AUTOMATION_ALLOWED=1."
    ),
)
def check_new_rentals(
    profile: Annotated[
        str,
        Field(description="Profile name matching [A-Za-z0-9_-]{1,64}."),
    ] = "default",
    location: Annotated[
        str,
        Field(description="Mudah location_slug from search_locations."),
    ] = "malaysia",
    rental_type: Annotated[
        RentalType,
        Field(description="room, property, apartment_condo, house, or commercial."),
    ] = "room",
    query: Annotated[str | None, Field(description="Free-text keyword.")] = None,
    min_price: Annotated[
        int | None,
        Field(description="Minimum monthly MYR."),
    ] = None,
    max_price: Annotated[
        int | None,
        Field(description="Maximum monthly MYR."),
    ] = None,
    bedrooms: Annotated[
        int | None,
        Field(description="Exact bedroom bucket: 1..4, or 5 for 5+."),
    ] = None,
    bathrooms: Annotated[
        int | None,
        Field(description="Exact bathroom bucket: 1..4, or 5 for 5+."),
    ] = None,
    min_sqft: Annotated[int | None, Field(description="Minimum square feet.")] = None,
    max_sqft: Annotated[int | None, Field(description="Maximum square feet.")] = None,
    furnishing: Annotated[
        Furnishing | None,
        Field(description="fully, partial, none, or null."),
    ] = None,
    seller_type: Annotated[
        SellerType,
        Field(description="any, private, or agent; applied locally."),
    ] = "any",
    max_pages: Annotated[
        int,
        Field(description="Newest pages to fetch, 1..5. Default 2."),
    ] = 2,
) -> dict:
    profile = _validate_profile(profile)
    max_pages = _check_required_int("max_pages", max_pages, 1, 5)
    filters = _validated_filters(
        location,
        rental_type,
        query,
        min_price,
        max_price,
        bedrooms,
        bathrooms,
        min_sqft,
        max_sqft,
        furnishing,
        seller_type,
    )
    canonical = json.dumps(filters, sort_keys=True, ensure_ascii=False)

    conn = _open_db(_db_path())
    try:
        row = conn.execute(
            "SELECT filters_json FROM profiles WHERE profile = ?",
            (profile,),
        ).fetchone()
        if row is not None and row[0] != canonical:
            raise ValueError(
                f"Profile '{profile}' already watches different filters. "
                "Use another profile or call reset_rental_watch first."
            )
        first_run = row is None
        items, pages_fetched = _fetch_newest(filters, max_pages)
        checked_count = len(items)
        now = _now_iso()

        if first_run:
            with conn:
                for item in items:
                    conn.execute(
                        "INSERT OR IGNORE INTO seen"
                        "(profile, listing_id, first_seen, title, price) "
                        "VALUES(?, ?, ?, ?, ?)",
                        (
                            profile,
                            item.get("id"),
                            now,
                            item.get("title"),
                            item.get("price"),
                        ),
                    )
                conn.execute(
                    "INSERT INTO profiles"
                    "(profile, filters_json, initialized_at, last_run) "
                    "VALUES(?, ?, ?, ?)",
                    (profile, canonical, now, now),
                )
            return {
                "status": "seeded",
                "profile": profile,
                "seeded_count": checked_count,
                "pages_fetched": pages_fetched,
                "new_items": [],
                "filters": filters,
                "last_run": now,
            }

        existing_ids = {
            row[0]
            for row in conn.execute(
                "SELECT listing_id FROM seen WHERE profile = ?", (profile,)
            )
        }
        new_items = [item for item in items if item.get("id") not in existing_ids]
        with conn:
            for item in new_items:
                conn.execute(
                    "INSERT OR IGNORE INTO seen"
                    "(profile, listing_id, first_seen, title, price) "
                    "VALUES(?, ?, ?, ?, ?)",
                    (
                        profile,
                        item.get("id"),
                        now,
                        item.get("title"),
                        item.get("price"),
                    ),
                )
            conn.execute(
                "UPDATE profiles SET last_run = ? WHERE profile = ?",
                (now, profile),
            )
        return {
            "status": "checked",
            "profile": profile,
            "checked_count": checked_count,
            "pages_fetched": pages_fetched,
            "new_count": len(new_items),
            "new_items": new_items,
            "filters": filters,
            "last_run": now,
        }
    finally:
        conn.close()


@mcp.tool(
    annotations=RESET_ANNOTATIONS,
    description=(
        "Delete a saved Mudah watch profile and its seen-listing state. The "
        "next check_new_rentals call for that profile seeds again."
    ),
)
def reset_rental_watch(
    profile: Annotated[
        str,
        Field(description="Profile name matching [A-Za-z0-9_-]{1,64}."),
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


if __name__ == "__main__":
    mcp.run(transport="stdio", show_banner=False)
