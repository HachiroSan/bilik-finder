#!/usr/bin/env python3
"""Mudah rental search client used by the local MCP server.

Mudah does not publish a rental-search API. Its public search pages currently
embed the result store in Next.js React Server Component (RSC) data. This
module fetches one public search page, extracts that store, and normalises the
ads. A legacy ``__NEXT_DATA__`` parser is kept as a fallback.

Network access is deliberately permission-gated. Mudah's robots.txt states
that automated access requires permission. Set ``MUDAH_AUTOMATION_ALLOWED=1``
only when you are authorised to automate the site.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone


BASE_URL = "https://www.mudah.my"
IMAGE_GRID_BASE_URL = "https://img.rnudah.com/grids"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36 "
    "mudah-finder/1.0"
)

DEFAULT_TIMEOUT = 30.0
MAX_RETRIES = 3
MAX_RESPONSE_BYTES = 8_000_000
SITE_PAGE_SIZE = 40
LOCATION_CACHE_SECONDS = 6 * 60 * 60

CATEGORY_PATHS = {
    "room": "rooms-for-rent",
    "property": "properties-for-rent",
    "apartment_condo": "apartment-condominium-for-rent",
    "house": "houses-for-rent",
    "commercial": "commercial-properties-for-rent",
}
FURNISHING_IDS = {
    "fully": "1",
    "partial": "2",
    "none": "3",
}
SORT_VALUES = {"latest", "price_asc", "price_desc"}
SELLER_TYPES = {"any", "private", "agent"}

SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
NEXT_DATA_RE = re.compile(
    r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
    re.IGNORECASE | re.DOTALL,
)
RSC_CHUNK_RE = re.compile(
    r"self\.__next_f\.push\(\s*\[\s*1\s*,\s*"
    r"(\"(?:\\.|[^\"\\])*\")\s*\]\s*\)",
    re.DOTALL,
)

_location_cache: tuple[float, list[dict]] | None = None


class MudahError(Exception):
    """Base exception for Mudah client failures."""


class MudahPermissionError(MudahError):
    """Raised when network automation has not been explicitly authorised."""


class MudahParseError(MudahError):
    """Raised when a search page no longer matches the supported schemas."""


class MudahBlockedError(MudahError):
    """Raised when the site appears to return a challenge/block page."""


def _automation_allowed() -> bool:
    return os.environ.get("MUDAH_AUTOMATION_ALLOWED", "").strip() == "1"


def _require_automation_permission() -> None:
    if not _automation_allowed():
        raise MudahPermissionError(
            "Mudah's robots.txt states that automated access requires permission. "
            "Set MUDAH_AUTOMATION_ALLOWED=1 only after obtaining authorisation."
        )


def _backoff_seconds(attempt: int, retry_after: str | None = None) -> float:
    if retry_after:
        try:
            return max(0.0, min(float(retry_after), 120.0))
        except (TypeError, ValueError):
            pass
    return min(2**attempt, 16) + random.uniform(0.0, 0.5)


def request_html(
    path: str,
    params: dict | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> tuple[str, str]:
    """Fetch a Mudah page and return ``(html, final_url)``.

    Redirects are followed by urllib. Requests are retried on 429, 5xx, and
    transient network failures. Network access requires the explicit
    ``MUDAH_AUTOMATION_ALLOWED=1`` acknowledgement.
    """

    _require_automation_permission()
    if not isinstance(path, str) or not path.startswith("/") or "://" in path:
        raise ValueError("path must be a site-relative path beginning with '/'")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        raise ValueError("timeout must be a positive number")

    query = urllib.parse.urlencode(params or {}, doseq=True)
    url = f"{BASE_URL}{path}"
    if query:
        url = f"{url}?{query}"
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }

    for attempt in range(MAX_RETRIES + 1):
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=float(timeout)) as response:
                body = response.read(MAX_RESPONSE_BYTES + 1)
                final_url = response.geturl()
                charset = response.headers.get_content_charset() or "utf-8"
            if len(body) > MAX_RESPONSE_BYTES:
                raise MudahError(
                    f"Mudah response exceeded {MAX_RESPONSE_BYTES:,} bytes"
                )
            return body.decode(charset, errors="replace"), final_url
        except urllib.error.HTTPError as err:
            retriable = err.code == 429 or 500 <= err.code < 600
            if retriable and attempt < MAX_RETRIES:
                retry_after = err.headers.get("Retry-After") if err.headers else None
                delay = _backoff_seconds(attempt, retry_after)
                print(
                    f"HTTP {err.code}; retry {attempt + 1}/{MAX_RETRIES} "
                    f"in {delay:.1f}s",
                    file=sys.stderr,
                )
                time.sleep(delay)
                continue
            raise
        except (urllib.error.URLError, TimeoutError) as err:
            if attempt < MAX_RETRIES:
                delay = _backoff_seconds(attempt)
                print(
                    f"Request failed ({err}); retry {attempt + 1}/{MAX_RETRIES} "
                    f"in {delay:.1f}s",
                    file=sys.stderr,
                )
                time.sleep(delay)
                continue
            raise
    raise RuntimeError("unreachable")  # pragma: no cover


def _extract_balanced_json(text: str, marker: str):
    """Decode the JSON object/list immediately following ``marker``."""

    marker_at = text.find(marker)
    if marker_at < 0:
        raise MudahParseError(f"Page data is missing {marker}")
    start = marker_at + len(marker)
    while start < len(text) and text[start].isspace():
        start += 1
    if start >= len(text) or text[start] not in "{[":
        preview = text[start : start + 40]
        raise MudahParseError(
            f"Expected JSON after {marker}; found {preview!r}"
        )

    opening = text[start]
    closing = "}" if opening == "{" else "]"
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == opening:
            depth += 1
        elif char == closing:
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start : index + 1])
                except json.JSONDecodeError as err:
                    raise MudahParseError(
                        f"Invalid embedded JSON after {marker}: {err}"
                    ) from err
    raise MudahParseError(f"Unterminated JSON after {marker}")


def _parse_next_data(html_text: str) -> dict | None:
    match = NEXT_DATA_RE.search(html_text)
    if match is None:
        return None
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError as err:
        raise MudahParseError(f"Invalid __NEXT_DATA__ JSON: {err}") from err
    page_props = data.get("props", {}).get("pageProps", {})
    store = page_props.get("initialStore")
    if not isinstance(store, dict):
        raise MudahParseError("__NEXT_DATA__ does not contain initialStore")
    query = page_props.get("initialQuery")
    return {
        "store": store,
        "initial_query": query if isinstance(query, dict) else {},
        "source_format": "next_data",
    }


def _parse_rsc_data(html_text: str) -> dict | None:
    chunks: list[str] = []
    for match in RSC_CHUNK_RE.finditer(html_text):
        try:
            chunk = json.loads(match.group(1))
        except json.JSONDecodeError as err:
            raise MudahParseError(f"Invalid Next.js RSC chunk: {err}") from err
        if isinstance(chunk, str):
            chunks.append(chunk)
    if not chunks:
        return None

    flight = "".join(chunks)
    store = _extract_balanced_json(flight, '"initialStore":')
    if not isinstance(store, dict):
        raise MudahParseError("RSC initialStore is not an object")
    try:
        query = _extract_balanced_json(flight, '"initialQuery":')
    except MudahParseError:
        query = {}
    return {
        "store": store,
        "initial_query": query if isinstance(query, dict) else {},
        "source_format": "next_rsc",
    }


def parse_search_html(html_text: str) -> dict:
    """Extract the result store from a Mudah search-page HTML document."""

    if not isinstance(html_text, str) or not html_text:
        raise MudahParseError("Mudah returned an empty page")

    parsed = _parse_rsc_data(html_text)
    if parsed is None:
        parsed = _parse_next_data(html_text)
    if parsed is not None:
        store = parsed["store"]
        ads = store.get("ads")
        if ads is not None and not isinstance(ads, list):
            raise MudahParseError("initialStore.ads is not a list")
        return parsed

    lowered = html_text.lower()
    challenge_markers = (
        "cf-chl-",
        "verify you are human",
        "captcha",
        "access denied",
        "attention required",
    )
    if any(marker in lowered for marker in challenge_markers):
        raise MudahBlockedError(
            "Mudah returned a challenge or access-denied page; stop and try later"
        )
    raise MudahParseError(
        "Mudah search data was not found. The site's page schema may have changed."
    )


def _as_number(value):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return value
    if not isinstance(value, str):
        return None
    cleaned = value.replace(",", "").strip()
    if not re.fullmatch(r"-?\d+(?:\.\d+)?", cleaned):
        return None
    number = float(cleaned)
    return int(number) if number.is_integer() else number


def _timestamp_iso(value) -> str | None:
    number = _as_number(value)
    if number is None:
        return None
    try:
        return datetime.fromtimestamp(float(number), timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _whatsapp_enabled(attributes: dict) -> bool:
    agent_data = attributes.get("agentData")
    if not isinstance(agent_data, dict):
        return False
    data = agent_data.get("data")
    if isinstance(data, dict) and str(data.get("userParamsEnableWa")) == "1":
        return True
    section = agent_data.get("section")
    if not isinstance(section, dict):
        return False
    buttons = section.get("buttons")
    if not isinstance(buttons, list):
        return False
    return any(
        isinstance(button, dict) and button.get("type") == "wa"
        for button in buttons
    )


def _image_url(ad: dict, attributes: dict) -> str | None:
    image = attributes.get("image")
    if not isinstance(image, str) or not image:
        return None
    if image.startswith("https://") or image.startswith("http://"):
        return image
    links = ad.get("links")
    base = None
    if isinstance(links, dict):
        base = links.get("imageGridBaseurl")
    if not isinstance(base, str) or not base:
        base = IMAGE_GRID_BASE_URL
    return f"{base.rstrip('/')}/{image.lstrip('/')}"


def normalize_ad(ad: dict, *, featured: bool = False) -> dict:
    """Map one raw Mudah result to a compact, stable record."""

    attributes = ad.get("attributes")
    if not isinstance(attributes, dict):
        attributes = {}

    listing_id = attributes.get("listId")
    if listing_id is None:
        listing_id = ad.get("id")
    phone_hidden = bool(attributes.get("phoneHidden"))
    phone = None if phone_hidden else attributes.get("phone")
    company_ad = bool(attributes.get("companyAd"))
    store_verified = attributes.get("storeVerified")
    seller_verified = None
    if store_verified is not None:
        seller_verified = str(store_verified).lower() not in {
            "",
            "false",
            "none",
            "unverified",
        }

    return {
        "id": str(listing_id) if listing_id is not None else None,
        "ad_id": attributes.get("adId"),
        "title": attributes.get("subject"),
        "price": _as_number(
            attributes.get("monthlyRent", attributes.get("priceAlias"))
        ),
        "currency": "MYR",
        "charge_unit": "PER_MONTH",
        "price_label": attributes.get("priceLabel"),
        "location": attributes.get("locationLabel"),
        "state": attributes.get("regionName"),
        "area": attributes.get("subareaName"),
        "building": attributes.get("buildingName"),
        "property_type": attributes.get("propertyTypeName"),
        "category": attributes.get("categoryName"),
        "bedrooms": _as_number(attributes.get("roomsName")),
        "bathrooms": _as_number(attributes.get("bathroomName")),
        "space_sqft": _as_number(attributes.get("size")),
        "seller_type": "agent" if company_ad else "private",
        "seller_name": attributes.get("nameLabel") or attributes.get("name"),
        "seller_verified": seller_verified,
        "contact_phone": phone,
        "whatsapp": _whatsapp_enabled(attributes),
        "published_at": _timestamp_iso(attributes.get("listTs")),
        "modified_at": _timestamp_iso(attributes.get("modifiedTs")),
        "site_date": attributes.get("date") or attributes.get("origDate"),
        "url": attributes.get("adviewUrl"),
        "cover_image_url": _image_url(ad, attributes),
        "image_count": _as_number(attributes.get("imageCount")),
        "has_video": bool(attributes.get("hasVideo")),
        "urgent": bool(attributes.get("hasPsUrgent")),
        "featured": bool(featured),
    }


def _clean_optional_text(value, name: str, maximum: int = 200) -> str | None:
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


def _validate_slug(value: str, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    cleaned = value.strip().lower()
    if len(cleaned) > 160 or not SLUG_RE.fullmatch(cleaned):
        raise ValueError(
            f"{name} must be a lowercase Mudah slug such as 'kuala-lumpur'"
        )
    return cleaned


def _validate_int(name: str, value, minimum: int, maximum: int):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if value < minimum or value > maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _validate_required_int(
    name: str,
    value,
    minimum: int,
    maximum: int,
) -> int:
    checked = _validate_int(name, value, minimum, maximum)
    if checked is None:
        raise ValueError(f"{name} is required")
    return checked


def _range_value(minimum: int | None, maximum: int | None) -> str | None:
    if minimum is None and maximum is None:
        return None
    return f"{'' if minimum is None else minimum}-{'' if maximum is None else maximum}"


def build_search_request(
    *,
    location: str = "malaysia",
    rental_type: str = "room",
    query: str | None = None,
    min_price: int | None = None,
    max_price: int | None = None,
    bedrooms: int | None = None,
    bathrooms: int | None = None,
    min_sqft: int | None = None,
    max_sqft: int | None = None,
    furnishing: str | None = None,
    page: int = 1,
    sort_by: str = "latest",
) -> tuple[str, dict]:
    """Validate filters and build a Mudah search path/query."""

    location = _validate_slug(location, "location")
    if rental_type not in CATEGORY_PATHS:
        raise ValueError(
            "rental_type must be one of: " + ", ".join(CATEGORY_PATHS)
        )
    query = _clean_optional_text(query, "query", 100)
    min_price = _validate_int("min_price", min_price, 0, 10_000_000)
    max_price = _validate_int("max_price", max_price, 0, 10_000_000)
    min_sqft = _validate_int("min_sqft", min_sqft, 0, 10_000_000)
    max_sqft = _validate_int("max_sqft", max_sqft, 0, 10_000_000)
    bedrooms = _validate_int("bedrooms", bedrooms, 1, 5)
    bathrooms = _validate_int("bathrooms", bathrooms, 1, 5)
    page = _validate_required_int("page", page, 1, 1_000_000)

    if min_price is not None and max_price is not None and min_price > max_price:
        raise ValueError("min_price must be <= max_price")
    if min_sqft is not None and max_sqft is not None and min_sqft > max_sqft:
        raise ValueError("min_sqft must be <= max_sqft")
    if furnishing is not None and furnishing not in FURNISHING_IDS:
        raise ValueError("furnishing must be one of: fully, partial, none")
    if sort_by not in SORT_VALUES:
        raise ValueError("sort_by must be one of: latest, price_asc, price_desc")

    params: dict[str, str | int] = {}
    if query:
        params["q"] = query
    if bathrooms is not None:
        params["bathroom_id"] = bathrooms
    if furnishing is not None:
        params["furnished_id"] = FURNISHING_IDS[furnishing]
    monthly_rent = _range_value(min_price, max_price)
    if monthly_rent is not None:
        params["monthly_rent"] = monthly_rent
    if bedrooms is not None:
        params["rooms_id"] = bedrooms
    size = _range_value(min_sqft, max_sqft)
    if size is not None:
        params["size"] = size
    if sort_by != "latest":
        params["sortby"] = sort_by
    if page > 1:
        params["o"] = page

    path = f"/{location}/{CATEGORY_PATHS[rental_type]}"
    return path, params


def search_rentals(
    *,
    location: str = "malaysia",
    rental_type: str = "room",
    query: str | None = None,
    min_price: int | None = None,
    max_price: int | None = None,
    bedrooms: int | None = None,
    bathrooms: int | None = None,
    min_sqft: int | None = None,
    max_sqft: int | None = None,
    furnishing: str | None = None,
    seller_type: str = "any",
    page: int = 1,
    sort_by: str = "latest",
    include_featured: bool = False,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict:
    """Fetch and normalise one Mudah rental-search result page."""

    if seller_type not in SELLER_TYPES:
        raise ValueError("seller_type must be one of: any, private, agent")
    path, params = build_search_request(
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
        page=page,
        sort_by=sort_by,
    )
    html_text, final_url = request_html(path, params, timeout)
    parsed = parse_search_html(html_text)
    store = parsed["store"]

    organic_raw = store.get("ads") or []
    featured_raw = store.get("featuredAds") or []
    candidates: list[tuple[dict, bool]] = []
    if include_featured:
        candidates.extend((ad, True) for ad in featured_raw if isinstance(ad, dict))
    candidates.extend((ad, False) for ad in organic_raw if isinstance(ad, dict))

    items: list[dict] = []
    seen_ids: set[str] = set()
    for raw_ad, featured in candidates:
        record = normalize_ad(raw_ad, featured=featured)
        listing_id = record.get("id")
        if not listing_id or listing_id in seen_ids:
            continue
        seen_ids.add(listing_id)
        if seller_type != "any" and record.get("seller_type") != seller_type:
            continue
        items.append(record)

    meta = store.get("meta")
    if not isinstance(meta, dict):
        meta = {}
    total_items = _as_number(meta.get("totalResults")) or 0
    total_pages = math.ceil(total_items / SITE_PAGE_SIZE) if total_items else 0
    return {
        "items": items,
        "pagination": {
            "page": page,
            "site_page_size": SITE_PAGE_SIZE,
            "server_total_items": total_items,
            "server_total_pages": total_pages,
            "has_next": page < total_pages,
            "organic_items_on_page": len(organic_raw),
            "featured_items_on_page": len(featured_raw),
            "matched_items_on_page": len(items),
            "seller_filter_local": seller_type != "any",
        },
        "request_url": final_url,
        "initial_query": parsed["initial_query"],
        "source_format": parsed["source_format"],
    }


def _flatten_locations(location_data) -> list[dict]:
    records: list[dict] = []
    if not isinstance(location_data, list):
        return records
    for state in location_data:
        if not isinstance(state, dict):
            continue
        state_name = state.get("name")
        state_slug = state.get("slug")
        if not isinstance(state_name, str) or not isinstance(state_slug, str):
            continue
        records.append(
            {
                "type": "STATE",
                "id": state.get("id"),
                "name": state_name,
                "display_name": state_name,
                "state": state_name,
                "state_slug": state_slug,
                "area": None,
                "area_slug": None,
                "location_slug": state_slug,
            }
        )
        for area in state.get("subarea") or []:
            if not isinstance(area, dict):
                continue
            area_name = area.get("name")
            area_slug = area.get("slug")
            if not isinstance(area_name, str) or not isinstance(area_slug, str):
                continue
            records.append(
                {
                    "type": "AREA",
                    "id": area.get("id"),
                    "name": area_name,
                    "display_name": f"{area_name}, {state_name}",
                    "state": state_name,
                    "state_slug": state_slug,
                    "area": area_name,
                    "area_slug": area_slug,
                    "location_slug": f"{state_slug}-{area_slug}",
                }
            )
    return records


def location_directory(
    *,
    timeout: float = DEFAULT_TIMEOUT,
    force_refresh: bool = False,
) -> list[dict]:
    """Return the cached state/subarea directory embedded by Mudah."""

    global _location_cache
    now = time.monotonic()
    if (
        not force_refresh
        and _location_cache is not None
        and now - _location_cache[0] < LOCATION_CACHE_SECONDS
    ):
        return _location_cache[1]

    html_text, _ = request_html("/malaysia/rooms-for-rent", timeout=timeout)
    parsed = parse_search_html(html_text)
    records = _flatten_locations(parsed["store"].get("locationData"))
    if not records:
        raise MudahParseError("Mudah's page did not contain locationData")
    _location_cache = (now, records)
    return records


def search_locations(
    query: str,
    *,
    state: str | None = None,
    rental_type: str = "room",
    limit: int = 10,
    timeout: float = DEFAULT_TIMEOUT,
) -> list[dict]:
    """Resolve a place name to Mudah state/area location slugs."""

    cleaned = _clean_optional_text(query, "query", 100)
    if cleaned is None:
        raise ValueError("query is required")
    if rental_type not in CATEGORY_PATHS:
        raise ValueError(
            "rental_type must be one of: " + ", ".join(CATEGORY_PATHS)
        )
    limit = _validate_required_int("limit", limit, 1, 50)
    state_filter = _clean_optional_text(state, "state", 100)
    if state_filter:
        state_filter = state_filter.casefold().replace("_", "-")

    query_folded = cleaned.casefold().replace("-", " ")
    tokens = [token for token in query_folded.split() if token]
    matches: list[tuple[int, dict]] = []
    for record in location_directory(timeout=timeout):
        state_values = {
            str(record.get("state", "")).casefold(),
            str(record.get("state_slug", "")).casefold(),
        }
        if state_filter and state_filter not in state_values:
            continue
        name = str(record.get("name", "")).casefold()
        display = str(record.get("display_name", "")).casefold()
        slug = str(record.get("location_slug", "")).casefold()
        haystack = " ".join((name, display, slug.replace("-", " ")))
        if not all(token in haystack for token in tokens):
            continue
        if query_folded in {name, display, slug.replace("-", " ")}:
            score = 0
        elif name.startswith(query_folded) or display.startswith(query_folded):
            score = 1
        elif query_folded in haystack:
            score = 2
        else:
            score = 3
        matches.append((score, record))

    matches.sort(
        key=lambda pair: (
            pair[0],
            0 if pair[1].get("type") == "STATE" else 1,
            str(pair[1].get("display_name", "")).casefold(),
        )
    )
    category_path = CATEGORY_PATHS[rental_type]
    results: list[dict] = []
    for _, record in matches[:limit]:
        result = dict(record)
        result["url"] = f"{BASE_URL}/{record['location_slug']}/{category_path}"
        results.append(result)
    return results


def _build_cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mudah.py",
        description="Search Mudah rental pages after obtaining automation permission.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    locations = subparsers.add_parser(
        "locations", help="resolve a state or area to Mudah location slugs"
    )
    locations.add_argument("query")
    locations.add_argument("--state")
    locations.add_argument("--rental-type", choices=CATEGORY_PATHS, default="room")
    locations.add_argument("--limit", type=int, default=10)
    locations.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)

    search = subparsers.add_parser("search", help="search one rental page")
    search.add_argument("--location", default="malaysia")
    search.add_argument("--rental-type", choices=CATEGORY_PATHS, default="room")
    search.add_argument("--query")
    search.add_argument("--min-price", type=int)
    search.add_argument("--max-price", type=int)
    search.add_argument("--bedrooms", type=int)
    search.add_argument("--bathrooms", type=int)
    search.add_argument("--min-sqft", type=int)
    search.add_argument("--max-sqft", type=int)
    search.add_argument("--furnishing", choices=FURNISHING_IDS)
    search.add_argument("--seller-type", choices=SELLER_TYPES, default="any")
    search.add_argument("--sort", choices=SORT_VALUES, default="latest")
    search.add_argument("--page", type=int, default=1)
    search.add_argument("--limit", type=int, default=10)
    search.add_argument("--include-featured", action="store_true")
    search.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    return parser


def main(argv=None) -> int:
    parser = _build_cli_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "locations":
            items = search_locations(
                args.query,
                state=args.state,
                rental_type=args.rental_type,
                limit=args.limit,
                timeout=args.timeout,
            )
            output = {"items": items, "count": len(items)}
        else:
            limit = _validate_required_int(
                "limit", args.limit, 1, SITE_PAGE_SIZE
            )
            output = search_rentals(
                location=args.location,
                rental_type=args.rental_type,
                query=args.query,
                min_price=args.min_price,
                max_price=args.max_price,
                bedrooms=args.bedrooms,
                bathrooms=args.bathrooms,
                min_sqft=args.min_sqft,
                max_sqft=args.max_sqft,
                furnishing=args.furnishing,
                seller_type=args.seller_type,
                page=args.page,
                sort_by=args.sort,
                include_featured=args.include_featured,
                timeout=args.timeout,
            )
            output["items"] = output["items"][:limit]
            output["pagination"]["returned"] = len(output["items"])
        json.dump(output, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        return 0
    except (MudahError, ValueError, urllib.error.URLError) as err:
        print(f"error: {err}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
