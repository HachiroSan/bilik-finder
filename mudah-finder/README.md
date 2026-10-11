# mudah-finder

Local Python client and stdio MCP server for searching rental listings on
[Mudah.my](https://www.mudah.my). It follows the same shape as the sibling
`ibilik-finder`: search, location lookup, one-shot new-listing checks, and a
SQLite-backed watch reset.

Mudah doesn't publish a rental-search API. Its public search pages currently
embed structured results in Next.js React Server Component data. The client
parses that `initialStore`; it also supports the older `__NEXT_DATA__` format
used by previous versions of the site.

## Permission gate

Mudah's [robots.txt](https://www.mudah.my/robots.txt) says automated access
requires permission. Network requests stay disabled unless this environment
variable is set:

```pwsh
$env:MUDAH_AUTOMATION_ALLOWED = "1"
```

Set it only after Mudah has authorised your use. The client won't solve
CAPTCHAs, evade blocks, or retry HTTP 403 responses. Without the variable, all
network tools fail with a clear permission error; offline parsing and tests
still work.

## Files

- `mudah.py`: stdlib HTTP client, current/legacy page parsers, normalisation,
  location lookup, and a small JSON CLI.
- `mcp_server.py`: FastMCP stdio server with four tools.
- `test_mudah.py`: offline parser, filter, watch-state, and MCP protocol tests.
- `requirements-mcp.txt`: pins `fastmcp==4.0.8`.

## Install

Run these commands from `C:\Dev\move-to-kl\mudah-finder`:

```pwsh
py -m pip install -r requirements-mcp.txt
py -m unittest -v test_mudah.py
```

After obtaining permission and setting `MUDAH_AUTOMATION_ALLOWED=1`, try:

```pwsh
# Resolve a location slug
py mudah.py locations cheras --state kuala-lumpur

# Rooms in Kuala Lumpur under RM900, newest first
py mudah.py search --location kuala-lumpur --max-price 900 --limit 5

# Whole units with server-side filters
py mudah.py search `
  --location kuala-lumpur `
  --rental-type property `
  --min-price 1000 `
  --max-price 2000 `
  --bedrooms 2 `
  --bathrooms 1 `
  --min-sqft 500 `
  --max-sqft 1000 `
  --furnishing fully `
  --sort price_asc `
  --limit 10
```

The CLI writes JSON to stdout. Diagnostics and permission errors go to stderr.

## MCP tools

| Tool | State | Purpose |
| --- | --- | --- |
| `search_rentals` | Read-only | Fetch one rental result page and return normalised listings plus pagination. |
| `search_locations` | Read-only | Match a state or area name against Mudah's embedded location directory and return usable slugs. |
| `check_new_rentals` | Writes local state | Seed or check a saved search profile, returning listing IDs not seen before. |
| `reset_rental_watch` | Deletes local state | Remove one profile so its next check seeds again. |

### `search_rentals`

Main inputs:

- `location`: `malaysia`, a state slug such as `kuala-lumpur`, or a combined
  state/area slug such as `kuala-lumpur-cheras`. Use `search_locations` rather
  than guessing.
- `rental_type`: `room`, `property`, `apartment_condo`, `house`, or
  `commercial`.
- `query`, `min_price`, `max_price`, `bedrooms`, `bathrooms`, `min_sqft`,
  `max_sqft`, and `furnishing` (`fully`, `partial`, `none`). Mudah applies these
  filters on the server. Bedroom and bathroom values are exact buckets from 1
  through 4; `5` means 5 or more.
- `sort_by`: `latest`, `price_asc`, or `price_desc`.
- `page`: one-based. Mudah represents pages after the first with `?o=N`.
- `seller_type`: `any`, `private`, or `agent`. This filter runs locally on the
  fetched page because the site's robots rules specifically disallow the
  `adsby` query parameter. Server totals therefore ignore this filter.
- `limit`: output cap from 1 to 40. Mudah still sends its fixed page of up to 40
  organic results.
- `include_featured`: promoted ads are off by default. When enabled, they come
  before organic results and duplicate IDs are removed.

Price ranges use Mudah's `monthly_rent` format: `500-1000`, `-800`, or `1500-`.
Some ranges redirect to an SEO path such as
`/rent-from-500-below-1000`; `urllib` follows that redirect.

Each item can contain:

```text
id, ad_id, title, price, currency, charge_unit, price_label,
location, state, area, building, property_type, category,
bedrooms, bathrooms, space_sqft, seller_type, seller_name,
seller_verified, contact_phone, whatsapp, published_at, modified_at,
site_date, url, cover_image_url, image_count, has_video, urgent, featured
```

Missing source fields become `null`. `published_at` and `modified_at` are UTC
ISO timestamps derived from Mudah's Unix timestamps.

### `search_locations`

Mudah embeds a directory containing 17 states plus their subareas. The MCP
caches it for six hours per process. A result looks like:

```json
{
  "type": "AREA",
  "name": "Cheras",
  "display_name": "Cheras, Kuala Lumpur",
  "state_slug": "kuala-lumpur",
  "area_slug": "cheras",
  "location_slug": "kuala-lumpur-cheras",
  "url": "https://www.mudah.my/kuala-lumpur-cheras/rooms-for-rent"
}
```

Use the optional `state` argument when an area name exists in several states.

### Watch state

`check_new_rentals` always requests organic listings with the `latest` sort.
It fetches at most `max_pages` (default 2, maximum 5), sleeping two seconds
between pages.

The first call for a profile stores all fetched IDs and returns no alerts.
Later calls return unseen IDs. Calling the same profile with different filters
fails instead of silently changing its meaning; reset it or choose another
profile.

State lives in `MUDAH_MCP_DB` when set, otherwise
`.mudah_mcp_watch.db` beside `mcp_server.py`. SQLite uses WAL mode and
transactional updates.

## opencode setup

Add this server to the MCP section of your opencode config:

```json
"mudah": {
  "type": "local",
  "command": ["py", "C:\\Dev\\move-to-kl\\mudah-finder\\mcp_server.py"],
  "enabled": true
}
```

Start opencode from a shell that already has the permission variable when
Mudah has authorised access:

```pwsh
$env:MUDAH_AUTOMATION_ALLOWED = "1"
opencode
```

Restart opencode after changing its MCP configuration.

## Direct MCP smoke test

```python
import asyncio
from fastmcp import Client
import mcp_server


async def main():
    async with Client(mcp_server.mcp) as client:
        print([tool.name for tool in await client.list_tools()])
        result = await client.call_tool(
            "search_rentals",
            {
                "location": "kuala-lumpur",
                "rental_type": "room",
                "max_price": 900,
                "limit": 5,
            },
        )
        print(result.data)


asyncio.run(main())
```

## Limits and breakage

- Search pages contain no full listing description, so results link back to
  the listing rather than fetching 40 detail pages.
- Mudah can change its page schema without notice. A missing `initialStore`
  produces a parser error instead of empty results.
- A 200 response containing a challenge or access-denied page also raises an
  error. The client doesn't work around it.
- Public contact details belong to real people. Keep them inside your rental
  search; don't republish, index, or harvest them.
