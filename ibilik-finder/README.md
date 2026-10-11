# ibilik-finder

A small command-line tool for finding rooms on [ibilik.com](https://www.ibilik.com)
via its public client API. No login, no API key, no dependencies beyond the
Python 3.14 standard library.

The `mudah-alerts` service reuses this module's HTTP and normalisation code for
opt-in iBilik Telegram alerts; its `watch` state here stays separate and is not
involved. See `../mudah-alerts/README.md` for the alert config.

It does three things:

- `locations` — turn a place name ("cheras") into the location codes the API needs.
- `search` — query room rentals, normalise the response, print or export it.
- `watch` — poll for newly published listings and tell you what's new.

Everything runs on Windows with the `py` launcher. It also works anywhere else
Python does; only the `--beep` option is Windows-specific.

---

## Quick start

Run these from `C:\Dev\move-to-kl\ibilik-finder`.

```pwsh
# 1. Find the codes for a place
py ibilik.py locations cheras

# 2. Search rooms under RM800 in Cheras
py ibilik.py search --city cheras --max-price 800 --limit 5

# 3. Free-text search near a campus, as JSON
py ibilik.py search --search "sunway university" --max-price 1000 --limit 3 --output json

# 4. Export a page as CSV
py ibilik.py search --city cheras --limit 3 --output csv --out smoke.csv

# 5. Poll for new listings (first run seeds the DB, no alerts)
py ibilik.py watch --city cheras --max-price 800 --once --db smoke_watch.db

# 6. One JSON record per line
py ibilik.py search --limit 2 --output jsonl --out smoke.jsonl
```

By default `search` fetches one page of 100. Use `--pages 0` to follow every
page (the whole site at `--limit 500` is roughly 56 requests).

---

## Flags

### Shared network flags

Available on every subcommand.

| Flag | Default | Meaning |
| --- | --- | --- |
| `--delay SECONDS` | `0.5` | Sleep between page requests. |
| `--timeout SECONDS` | `30` | Per-request timeout. |

Every request sets a custom User-Agent and retries up to 4 times with
exponential backoff plus jitter on `429`, `5xx`, timeouts and URL errors. A
`Retry-After` header is honoured when present. An HTTP 422 prints the API's
validation message and exits with code `2`. Ctrl+C exits `130`; partial output
is kept.

### `search` filters

`--search TEXT`, `--state CODE`, `--city CODE`, `--area CODE`, `--property CODE`,
`--min-price INT`, `--max-price INT`, `--room-type` (single value),
`--bathroom-type` (single), `--unit-type` (single), `--floor-level` (single),
`--min-sqft INT`, `--max-sqft INT`, `--kyc-verified`, `--amenity CODE`
(repeatable), `--posted-by USER_ID`, `--sort published_at|price`,
`--direction asc|desc`.

- `--amenity` is repeatable and the API applies **AND** semantics: every code
  must be present. There is no union/OR across values — run separate queries for
  that.
- `--room-type` accepts one value only. The API's "last value wins" behaviour is
  not exposed here; passing a comma-separated list is a validation error.

`search` output flags: `--limit` (per page, default 100, cap 5000), `--pages`
(default 1, `0` = follow all), `--output table|json|jsonl|csv`, `--out FILE`,
and `--raw` (emit raw API items instead of normalised ones; only valid with
`json`/`jsonl`).

### `locations`

```
py ibilik.py locations QUERY [--limit N] [--types ROOM] [--output table|json] [--out FILE]
```

### `watch`

```
py ibilik.py watch [filters...] [--limit 100] [--max-pages 3]
                   [--interval 90] [--db PATH] [--once] [--beep]
```

| Flag | Default | Meaning |
| --- | --- | --- |
| `--interval SECONDS` | `90` | Time between cycles. Minimum 30; lower is rejected. |
| `--max-pages N` | `3` | Pages fetched per cycle, newest first. |
| `--db PATH` | `ibilik_watch.db` beside the script | SQLite state file. |
| `--once` | off | Run a single cycle and exit (good for Task Scheduler / cron). |
| `--beep` | off | `winsound.Beep(880, 300)` when new listings appear. No-op off Windows. |

Watch always sorts by `published_at` descending so it can stop as soon as it
covers listings already seen.

---

## Normalised record

`search` normalises each API item to these fields. Anything the API omits or
nulls becomes `null`.

`id, title, price, currency, charge_unit, state, city, area, room_type,
bathroom_type, unit_type, floor_level, is_studio, space_sqft, lat, lng,
amenities, contact_phone, whatsapp, posted_by, posted_by_verified, view_count,
published_at, url, image_count`

`amenities` is a list of display names. In CSV it is joined with `;`. The huge
`thumbnailBase64` field inside `images` is never read, stored or printed — only
the image count is kept.

---

## API reference

Base URL: `https://prod-api.ibilik.com`. No auth is required for the client
listing endpoints.

### Search

`GET /api/v1/client/listings/room-rentals`

Parameters (names contain brackets, so use `urllib.parse.urlencode(..., doseq=True)`):

| Param | Notes |
| --- | --- |
| `limit` | Per page. Tested working up to 5000; default 16. |
| `page` | 1-based. A page beyond `totalPages` returns HTTP 200 with empty `items` and `pagination.totalItems = 0`. |
| `sortBy` | `published_at` or `price`. |
| `sortDirection` | `asc` or `desc`. |
| `filter[search]` | Free text. Space-separated words work ("sunway university" → 704 results). Supports Chinese and Malay. `+` and quotes are treated literally — avoid them. |
| `filter[state]`, `filter[city]`, `filter[area]`, `filter[region]`, `filter[property]` | Location codes from `location-search`. |
| `filter[minPrice]`, `filter[maxPrice]` | Numeric MYR monthly. Non-numeric → 422. |
| `filter[roomType]` | `MASTER` \| `GUEST` \| `MIDDLE` \| `STUDIO` \| `SINGLE` \| `SUITE`. If repeated, the **last** value wins — it is not a union. Comma-separated → 422. |
| `filter[bathroomType]` | `PRIVATE` \| `SHARED`. |
| `filter[unitType]` | `STUDIO` \| `DUPLEX` \| `TRIPLEX` \| `CORNER_LOT` \| `END_LOT` \| `INTERMEDIATE` \| `LOFT` \| `DUAL_KEY` \| `SOHO` \| `PENTHOUSE`. |
| `filter[floorLevel]` | `GROUND` \| `LOW` \| `MID` \| `HIGH` \| `PENTHOUSE`. |
| `filter[minSpaceSqft]`, `filter[maxSpaceSqft]` | Numeric. |
| `filter[kycVerified]` | `true` \| `false`. |
| `filter[amenities]`, `filter[utilities]`, `filter[preferences]` | Codes. Comma or repeated both accepted. Multiple values = **AND/intersection** (verified: co-living=10298, mini-market=16012, both=7095). |
| `filter[types]` | `ROOM` \| `HOTEL` \| ... |
| Others | `filter[postedByUserId]`, `filter[orgId]`, `filter[listingAmenity]`, `filter[unitAmenity]`, `filter[hotelPropertyType]`, `filter[commercialType]`, `filter[eventSpaceType]`. |

Response shape:

```json
{"items": [...], "pagination": {"limit": 16, "page": 1, "totalItems": 0, "totalPages": 0}}
```

422 shape:

```json
{"message": "Validation failed for GET ...",
 "fields": [{"message": "...", "key": "room_type", "source": "query"}]}
```

### Location search

`GET /api/v1/client/location-search?query=cheras&limit=10&filter[types]=ROOM`

Response: `{"items": [{"id", "code", "name", "type"}]}` where `type` is
`STATE` | `REGION` | `CITY` | `AREA`. Example codes: `cheras` (CITY),
`bandar-mahkota-cheras` (AREA), `selangor` (STATE), `petaling-jaya` (CITY).

### Listing item schema

Key paths (all can be null or missing — parse defensively):

- `id` (uuid), `slug`, `type` ("ROOM")
- `locales['en-US'].title`, `locales['en-US'].description`
- `ratePlans` is an **array**: `ratePlans[0].chargeAmount`, `.chargeUnit`
  (`PER_MONTH` | `PER_NIGHT`), `.currency` ("MYR"). May be empty or missing.
- `longTermRoomRentalListing`: `{isStudio, roomType, bathroomType, unitType,
  floorLevel}`. Can be null.
- `spaceSqft` (top-level number)
- `state` / `city` / `area`: null or `{id, code, locales: {'en-US': {name}}}`;
  display name at `...locales['en-US'].name`
- `longitude`, `latitude`, `address` (address is usually null)
- `images`: array of `{url, isCover, ...}`; each also carries a large
  `thumbnailBase64` field — never store or print it
- `amenities`: array of `{amenity: {code, locales: {'en-US': {name}}}}`
- `contactInformation`: **array** (or null) of entries. The phone lives at
  `[i].contact.contactValue` (`contactType == "PHONE"`); `[i].channels` is e.g.
  `["PHONE", "WHATSAPP"]`.
- `postedBy`: null or `{name, isVerified, isKycVerified}`
- `viewCount`, `publishedAt` (ISO), `status` ("PUBLISHED")

Public listing URL: `https://www.ibilik.com/room-rentals/{id}/{slug}`.

---

## Rate-limit findings

Observed behaviour, not vendor documentation:

- No `x-ratelimit-*` headers and no cache-control. Cloudflare sits in front
  (`cf-cache-status: DYNAMIC`).
- 250+ requests over about 5 minutes produced zero throttling:
  - 30 sequential requests at ~3 req/s — all 200
  - 60 requests at concurrency 10 — all 200, 4.5s
  - 100 requests at concurrency 20 — all 200, 6.2s (~16 req/s)
  - a follow-up request immediately after — 200
- Responses are heavy: roughly 10–20 KB per item because of the base64
  thumbnails inside `images`. `limit=100` is about 1.5–2 MB.
- Practical guidance used by this tool: 0.5s delay between requests, concurrency
  1, retry with backoff on `429`/`5xx`. At `limit=500` the whole site
  (~28,000 rooms) is about 56 requests.

---

## Notes

- The `--raw` output is the quickest way to inspect fields when the API adds or
  changes something.
- OR across multiple values is not supported server-side. Run one query per
  value and merge yourself.
- `filter[search]` treats `+` and quotes literally. Prefer plain words.
- `roomType` last-wins means you cannot ask for two room types in one request.

---

## MCP server

`mcp_server.py` is a local stdio [Model Context Protocol](https://modelcontextprotocol.io)
server that exposes iBilik search to MCP-capable AI clients (opencode, Claude
Desktop, etc.). It reuses the same HTTP and normalisation code as the CLI — one
API client, two front ends.

### Install

```pwsh
py -m pip install -r requirements-mcp.txt
```

`requirements-mcp.txt` pins `fastmcp==4.0.8`.

### Run directly

```pwsh
py mcp_server.py
```

It speaks MCP over stdin/stdout and exits when the client does. Diagnostics go
to stderr only, so the protocol stream stays clean.

### Tools

| Tool | State | Inputs | Returns |
| --- | --- | --- | --- |
| `search_rooms` | read-only | `query`, `state`, `city`, `area`, `min_price`, `max_price`, `room_type`, `bathroom_type`, `unit_type`, `floor_level`, `min_sqft`, `max_sqft`, `kyc_verified`, `amenities`, `page`, `limit`, `sort_by`, `sort_direction`, `include_description`, `description_chars` | `{items, pagination}` for one API page. |
| `search_locations` | read-only | `query`, `limit`, `listing_types` | `{items: [{id, code, name, type}], count}`. |
| `check_new_rooms` | state-changing | `profile` + the `search_rooms` filters, plus `limit`, `max_pages`, `description_chars` | First call: `{status: "seeded", seeded_count, new_items: []}`. Later calls: `{status: "checked", checked_count, new_count, new_items, last_run}`. |
| `reset_room_watch` | state-changing | `profile` | `{profile, reset, deleted_seen}`. The next check seeds again. |

Notes on inputs and outputs:

- Location arguments are iBilik **codes** (`cheras`, `selangor`), not free
  text. Call `search_locations` first.
- Prices are monthly MYR. `room_type`, `bathroom_type`, `unit_type`,
  `floor_level`, `sort_by`, `sort_direction` are constrained enums, so clients
  see valid choices in the schema.
- `amenities` uses the site's AND/intersection semantics: every code must be
  present. Max 20 values.
- **Result caps for context safety**: `search_rooms` and `check_new_rooms` cap
  `limit` at 100 per page (`search_locations` at 50). The underlying API allows
  5000, but the CLI remains the tool for bulk pulls. `check_new_rooms` fetches
  at most `max_pages` (default 3, max 10) per call.
- Each returned record is the same normalised shape as the CLI
  (`id, title, price, currency, charge_unit, state, city, area, room_type, ...`),
  enriched with `description_excerpt` (whitespace-collapsed, truncated) and
  `cover_image_url`. The base64 thumbnails are never included.
- Invalid input raises a normal tool error (MCP `isError`), not a crash. A
  rejected API request surfaces the iBilik validation message. The process
  stays alive.

### Watch state

`check_new_rooms` persists state in SQLite, keyed by `profile`:

- Path from `IBILIK_MCP_DB`, otherwise `.ibilik_mcp_watch.db` beside
  `mcp_server.py`.
- Tables: `seen(profile, listing_id, ...)` and
  `profiles(profile, filters_json, initialized_at, last_run)`. WAL mode,
  transactional writes.
- The first call for a profile seeds all currently-fetched listings and returns
  no alerts. Later calls return only IDs not seen before, and update `last_run`.
- Calling a profile with **different filters** is rejected — use a different
  profile name or `reset_room_watch` first. Filters are not silently merged.
- `check_new_rooms` is one-shot; it never loops or sleeps. Schedule it with the
  client or Task Scheduler if you want periodic checks.

### opencode config

Add to your opencode MCP config (do not edit config for you — this is the
snippet to paste yourself):

```json
"ibilik": {
  "type": "local",
  "command": ["py", "C:\\Dev\\move-to-kl\\ibilik-finder\\mcp_server.py"],
  "enabled": true
}
```

Restart opencode after changing MCP config so it picks up the new server.

### Testing with FastMCP

In-process:

```python
import asyncio
from fastmcp import Client
import mcp_server

async def main():
    async with Client(mcp_server.mcp) as client:
        print([t.name for t in await client.list_tools()])
        result = await client.call_tool(
            "search_locations", {"query": "cheras", "limit": 3}
        )
        print(result.data)

asyncio.run(main())
```

Over stdio, as a subprocess:

```python
import asyncio
from fastmcp import Client

async def main():
    config = {"mcpServers": {"ibilik": {
        "command": "py",
        "args": [r"C:\Dev\move-to-kl\ibilik-finder\mcp_server.py"],
    }}}
    async with Client(config) as client:
        print([t.name for t in await client.list_tools()])

asyncio.run(main())
```

## Responsible use

ibilik's listings are public, but the people behind them are not. This tool is
for personal room-hunting. Keep request rates gentle, respect the site's terms,
and treat contact numbers as personal data — don't republish or bulk-collect
them.
