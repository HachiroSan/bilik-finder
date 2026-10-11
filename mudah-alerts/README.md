# mudah-alerts

A small polling service that watches rental searches and pushes new listings to
Telegram. It reuses the sibling `mudah-finder/mudah.py` client for Mudah and the
sibling `ibilik-finder/ibilik.py` client for iBilik, so there is no second
scraper or second bot to keep in sync. State lives in SQLite, which means
restarts and overlapping searches don't resend the same ad.

Mudah keyword searches and iBilik searches are both configured together in the
shipped `config.example.json`; the same process, bot token, and chat id cover
both providers. Each provider keeps its own dedupe state, so an iBilik id can
never collide with a Mudah id, and each polls independently: a bad cycle on one
provider never stops the other.

## Permission gate

Mudah's [robots.txt](https://www.mudah.my/robots.txt) states that automated
access requires permission. The Mudah side stays disabled until this variable is
set:

```pwsh
$env:MUDAH_AUTOMATION_ALLOWED = "1"
```

Set it only after Mudah has authorised your use. The service will not solve
CAPTCHAs, rotate identities, or evade blocks. The `--dry-run` flag still reads
Mudah, so it needs the same permission. No live call happens during tests.

The gate only ever removes the Mudah side. When it is off and the config has
Mudah `keywords`, the service logs a warning and skips Mudah while any `ibilik`
searches keep polling, because iBilik uses a public API and its own terms apply.
A Mudah-only config with the gate off has nothing left to run and exits with a
clear message. The standalone `mudah-finder/mudah.py` keeps its own hard gate.

## Telegram credentials

The bot token and chat id come from the environment, never from the config file:

```pwsh
$env:TELEGRAM_BOT_TOKEN = "123456:ABC..."
$env:TELEGRAM_CHAT_ID = "123456789"
```

A config that contains a token or chat-id key is rejected at startup. Error
messages redact the token before they reach the log.

## Install

No third-party packages are needed. Python 3.11 or newer works, and the repo
already targets 3.14. `config.example.json` is a ready combined config for both
providers:

```pwsh
Copy-Item config.example.json config.json
# edit config.json for your searches
py mudah_alerts.py --config config.json
```

`--config` defaults to `config.json` next to the script. Relative
`database_path` values resolve from the config file's folder,
not from wherever you launched the process. That keeps scheduled runs stable.

### Local run from the repository root

`run_alerts.py` at the repository root is the convenience entry point. It loads
the root `.env` with a stdlib parser (no new dependency) and then runs this CLI,
defaulting `--config` to the root `config.json` and falling back to the tracked
`config.example.json` when that file is absent:

```pwsh
py run_alerts.py --once        # one combined cycle, then exit
py run_alerts.py --dry-run     # report matches only, send nothing
py run_alerts.py               # poll hourly
```

Precedence is explicit: a variable already set in the real environment always
wins, and `.env` only fills the ones that are unset, so a shell export or Docker
`environment:` entry overrides the file. Only the number of keys loaded is
reported; secret values are never read back or logged.

## Config

A Mudah block (the shipped `config.example.json` adds the `ibilik` block
documented below, so both providers run from one file):

```json
{
  "poll_interval_seconds": 3600,
  "freshness_hours": 48,
  "location": "malaysia",
  "rental_type": "room",
  "max_price": 2200,
  "max_pages_per_keyword": 2,
  "page_pacing_seconds": 2,
  "search_timeout_seconds": 30,
  "database_path": "mudah_alerts.db",
  "keywords": [
    {"query": "Platinum Victory 3", "max_price": 1800},
    {"query": "Putra Villa Condominium"}
  ]
}
```

| Key | Default | Notes |
| --- | --- | --- |
| `poll_interval_seconds` | 3600 | Minimum 60, maximum 24 hours. Each cycle waits this long after finishing. |
| `freshness_hours` | 48 | Listings older than this are ignored. Maximum 48. |
| `location` | `malaysia` | A Mudah location slug. Resolve areas with `mudah.py locations`. |
| `rental_type` | `room` | `room`, `property`, `apartment_condo`, `house`, or `commercial`. |
| `max_price` | none | Monthly rent ceiling in ringgit. Applied by Mudah where possible and checked locally. |
| `max_pages_per_keyword` | 2 | 1 to 5. A cycle stops early when a page has no next page. |
| `page_pacing_seconds` | 2 | Sleep between pages and between keywords. |
| `search_timeout_seconds` | 30 | Per-request timeout. |
| `database_path` | `mudah_alerts.db` | SQLite file. Relative paths sit beside the config. |
| `keywords` | required unless `ibilik` is set | Strings, or objects with an optional per-keyword `max_price`. |

Numbers must be finite and inside their range. A poll below 60 seconds, a
freshness window above 48 hours, a negative rent, a NaN, or an unknown rental
type stops startup with a clear message. A `lock_path` key is rejected: the
lock is derived from `database_path` so a config cannot bypass the singleton
guard.

## iBilik searches

iBilik is configured with an `ibilik` block; the shipped `config.example.json`
already includes one. A config can carry Mudah `keywords`, an `ibilik` block, or
both; at least one is required. A config with only an `ibilik` block runs
without the Mudah permission flag.

```json
{
  "poll_interval_seconds": 3600,
  "database_path": "mudah_alerts.db",
  "ibilik": {
    "enabled": true,
    "initial_seed": true,
    "max_pages_per_search": 3,
    "limit_per_page": 100,
    "page_pacing_seconds": 2,
    "search_timeout_seconds": 30,
    "searches": [
      {"name": "cheras-rooms", "city": "cheras", "max_price": 900, "room_type": "MASTER"},
      {"name": "sunway", "query": "sunway university", "max_price": 1000}
    ]
  }
}
```

| iBilik key | Default | Notes |
| --- | --- | --- |
| `enabled` | `true` | `false` turns the block off without deleting it. |
| `initial_seed` | `true` | On first run, record current listings without alerting. |
| `poll_interval_seconds` | top-level `poll_interval_seconds` | Minimum 60. |
| `freshness_hours` | 48 | 1 to 168. Listings older than this are ignored. |
| `max_pages_per_search` | 3 | 1 to 10. Newest-first pages fetched per search. |
| `limit_per_page` | 100 | 1 to 100. |
| `page_pacing_seconds` | 2 | Sleep between pages and between searches. |
| `search_timeout_seconds` | 30 | Per-request timeout. |
| `searches` | required | Non-empty list of search objects. |

Each search object needs a unique `name` (`[A-Za-z0-9_-]{1,64}`) that namespaces
its dedupe rows, plus filters. A search is normally an explicit set of keys, but
it may instead be a single `url` key pointing at an `ibilik.com` search URL: the
site's `?property=`, `?city=`, `?area=`, `?state=` and `?search=` params are
decoded into the filters below. A `url` cannot be combined with any of the
location or `query` keys.

| Filter | Notes |
| --- | --- |
| `query` | Free text, e.g. `sunway university`. |
| `state`, `city`, `area` | iBilik location **codes**, lowercase hyphens like `selangor`, `cheras`, `bandar-mahkota-cheras`. Resolve them with `ibilik.py locations`. |
| `property` | iBilik property **code**, e.g. `the-troika`. Resolve it with `ibilik.py locations`. |
| `url` | A single `ibilik.com` search URL whose query params supply the filters; mutually exclusive with the location keys above. |
| `min_price`, `max_price` | Monthly MYR. Checked again locally for `max_price`. |
| `room_type` | `MASTER`, `GUEST`, `MIDDLE`, `STUDIO`, `SINGLE`, `SUITE`. |
| `bathroom_type` | `PRIVATE`, `SHARED`. |
| `unit_type` | `STUDIO`, `DUPLEX`, `TRIPLEX`, `CORNER_LOT`, `END_LOT`, `INTERMEDIATE`, `LOFT`, `DUAL_KEY`, `SOHO`, `PENTHOUSE`. |
| `floor_level` | `GROUND`, `LOW`, `MID`, `HIGH`, `PENTHOUSE`. |
| `min_sqft`, `max_sqft` | Whole numbers. |
| `kyc_verified` | `true` or `false`. |
| `amenities` | List of iBilik codes; all must be present (AND). Up to 20. |

Unknown keys are rejected so a typo cannot silently widen a search. Location
codes and enums are validated against the same values `ibilik.py` uses, so a bad
code stops startup instead of returning an empty search forever.

## What counts as fresh

Each listing is dated once, in this order:

1. `published_at` from the finder, a UTC ISO timestamp built from Mudah's own
   `listTs`.
2. `site_date` when `published_at` is missing or unparseable.

`modified_at` is never used. That matters because an old ad bumped by an agent
should not look new.

`site_date` covers `2 hours ago`, `45 minutes ago`, `just now`, `today 14:30`,
`yesterday 09:00`, `10 Oct`, `10 Oct 2025`, `Oct 10, 2025`, `10/10/2025`,
`10.10.2025`, and ISO strings. Today and yesterday are read on the Malaysian
clock (UTC+8). A date with no year is assumed to be the current Malaysian year;
if that lands in the future, the service rolls it back a year. A relative
string must be the whole value, and an out-of-range clock like `today 25:99`
is dropped rather than quietly read as midnight.

A listing must sit in the past and no more than `freshness_hours` behind now.
Exactly 48 hours old is accepted; 48 hours and one second is not. Missing or
unparseable dates are dropped. The window is capped at 48 hours both in config
and again at runtime, so even a hand-edited value cannot widen it.

iBilik listings use the API's ISO `publishedAt` directly. Because iBilik has no
published 48-hour rule, its window is set by `ibilik.freshness_hours`, from 1 to
168 hours, and capped again at runtime.

## Send and retry rules

Pages and keywords are processed one at a time. Each matching listing is sent
as soon as its page is fetched, so an alert is not held back while later pages
or keywords are still loading, and a failure on a later page cannot discard a
result already delivered. Before each send the service rechecks the listing's
age against the current clock, not the cycle-start time.

A listing is marked sent only after Telegram answers `ok: true`. If the send
fails, the row keeps an error and a retry count, and the next cycle tries
again. Nothing about a failed send is recorded as delivered.

When Telegram returns HTTP 429, the service stops sending for the rest of the
cycle and waits before polling again. It uses the API's `retry_after` when
present and falls back to a 60-second cooldown when it is not, and it stops
fetching later pages and keywords too, so a pushback does not turn into a hot
loop.

Mudah blocks and 429/5xx responses stop the remaining keyword requests for that
cycle and trigger an adaptive delay with jitter: 30 seconds, then a minute,
then two minutes, capped at 30 minutes. The failing-cycle streak carries across
cycles and only a healthy cycle resets it. A single fetch or parse failure is
logged and the next cycle starts from scratch, so a transient schema hiccup
recovers on its own.

The two providers run as separate services inside one process. An iBilik fetch
failure fails only that search, and a 429 halts the remaining iBilik searches
for the cycle; neither touches Mudah, and a Mudah block does not stop iBilik.
Each service tracks its own backoff and next due time, and the loop sleeps only
until the earliest due time, so a long backoff on one provider never holds up
the other.

With `--once`, each provider runs one cycle; an unexpected crash in one is
logged and the others still run, and the process exits nonzero so a scheduler
can tell a partial run from a clean one.

## First run

Nothing is in the database yet, so the first cycle sends every matching Mudah
listing that falls inside the freshness window. For `Platinum Victory 3` that
could be a handful of messages. If you want to see the list without sending, run
once with `--dry-run`. It prints the listing ids it would send and changes no
state.

iBilik behaves differently on purpose. With `initial_seed` left at `true`, the
first cycle fetches the current listings and records them as already known
without sending anything, so adding a search does not dump its whole backlog
into the chat. Only listings published after that are alerted. Seeding only
happens after every page of the search is fetched, so a partial fetch leaves the
search unseeded and the next cycle retries it in full. In `--dry-run` the same
path prints `would seed iBilik search <name>` instead of listing the backlog.
Set `initial_seed` to `false` if you want the old first-run behaviour for a
search.

In a combined config the first cycle does both at once: it may send fresh Mudah
matches, and it seeds each not-yet-initialised iBilik search without sending that
backlog. Run `--dry-run` first if you want to see the Mudah ids it would send and
the iBilik searches it would seed, with nothing written.

## Dedupe across restarts and keywords

Mudah state keys on the Mudah listing id, one row per listing. Two overlapping
keyword searches that return the same ad produce one alert. A restart reads the
same table, so a listing already sent stays sent. A previously failed listing
stays retryable because it has no `sent_at`.

iBilik state lives in separate `ibilik_sent` and `ibilik_searches` tables in the
same database file, keyed by `(search_name, listing_id)`. That keeps two iBilik
searches apart and, more importantly, keeps iBilik rows from ever colliding with
Mudah ones; an existing Mudah database gains the new tables without its rows
being altered. A seeded listing counts as known but is not marked sent, so
nothing pretends it was delivered, and a failed send keeps both timestamps null
so the next cycle retries it.

`listings.id` must be present for a row to exist. Ads without an id are skipped
in memory and never recorded.

## One process per database

The service takes an OS-level lock before it starts. The lock path is always
the resolved database path plus `.lock`, so `mudah_alerts.db` locks through
`mudah_alerts.db.lock`. A config that tries to set `lock_path` is rejected,
because two configs sharing a database but using different locks would bypass
the guard. The lock releases automatically if the process dies, so a crash
won't wedge the service. A second process on the same database exits with a
message instead of sending duplicates.

## The crash window

Telegram delivery and the local `sent_at` write are two separate steps, and
neither is transactional with the other. A few things can happen:

- The process dies after Telegram accepted the message but before the row was
  committed, so the next cycle sends it again.
- Telegram accepts the message but its HTTP response is lost. The row is only
  marked sent on `ok: true`, so an unknown response counts as a failure and may
  be retried.
- Telegram accepts the message and later a network timeout makes it look
  rejected.

Delivery is best-effort with retries, not guaranteed: a failed listing must be
rediscovered while still fresh to be retried. Occasional duplicates are possible
and the service makes no exactly-once claim. SQLite commit ordering and
`sent_at`-after-`ok` reduce the window; they don't close it.

## Message shape

Each Mudah alert is HTML, with the title and quotes escaped and only an `https`
URL on a `mudah.my` host linked. Oversized or malformed URLs are skipped, and
the message is never sliced through the middle of the link markup. The body
carries the title, a `Mudah · ` provider label before the monthly rent, the
absolute posting time in Malaysia time (MYT) with a relative age in parentheses,
and an "Open on Mudah" link. A listing with a missing, negative, or non-finite price is skipped
rather than sent without an asking price, and the rent is shown exactly (for
example `RM 1,234.50`) rather than rounded.

An iBilik alert follows the same rules with an `ibilik.com` URL. The body names
the provider before the rent with its unit (`/month` or `/night`) on the same
line, then the state/city/area when known, the absolute posting time in MYT with
a relative age in parentheses, and an "Open on iBilik" link. Contact numbers and WhatsApp flags are never included: they are
personal data and have no place in an alert.

## CLI

```pwsh
# Run forever, polling on the configured interval
py mudah_alerts.py --config config.json

# One cycle, then exit
py mudah_alerts.py --config config.json --once

# Report matches without sending or writing seen state
py mudah_alerts.py --config config.json --dry-run

# Extra detail on stderr
py mudah_alerts.py --config config.json -v
```

Ctrl+C and SIGTERM stop the loop between cycles.

## Tests

The suite is offline. It injects a fake clock, fake search, fake sender, and a
no-op sleep, so no Mudah, iBilik, or Telegram traffic happens.

```pwsh
py -m unittest discover -v
```

`test_mudah_alerts.py` covers the 48-hour config cap and runtime hard cap,
Malaysian midnight and relative dates, strict clock and relative parsing,
month-first and dotted dates, leap-day handling, `published_at` priority over
`site_date` and `modified_at`, local rent ceilings with per-keyword overrides,
duplicates across restarts and keywords, per-page delivery with a later-page
failure, send-time freshness recheck, Telegram failure, 429 with and without
`retry_after`, growing failure backoff, provider halt on block/429, keyword
pacing, dry-run isolation, the derived singleton lock, fail-closed state reads,
schema exceptions, and token redaction.

`test_ibilik_alerts.py` covers iBilik config parsing and backward compatibility,
iBilik-only configs skipping the Mudah gate, first-run seeding, new-only and
restart dedupe, overlapping ids across providers, send-failure retry, one
search's fetch failure not stopping another or Mudah, partial-fetch seeding
protection, message safety and contact omission, and dry-run non-mutation.

`test_alert_scheduling.py` drives the scheduler with a fake monotonic clock and
a recording stop event, proving a long backed-off provider does not delay a
healthy one, providers keep independent cadences, the wait never spins, and a
single provider still polls on its own delay. It also runs `main --once` with
injected fake services to show one provider's crash is contained, the other
still runs, and the exit code is nonzero.

`test_combined_providers.py` covers the combined setup: the one-hour default and
the shipped combined examples parsing, dropping Mudah when the gate is off while
iBilik still fetches, a Mudah-only config failing closed, provider crash
isolation in `--once`, the stdlib `.env` loader with real-environment precedence
and no value disclosure, repeated-cycle/restart dedupe, and the root launcher.

Run the finders' own regression suites after changing `mudah.py` or
`ibilik.py`:

```pwsh
cd ..\mudah-finder
py -m unittest -v test_mudah.py
```

## Limits

- The Mudah finder parses Mudah's current RSC data. A schema change shows up as a
  logged parse error and an empty cycle, not a crash loop. Unexpected
  `ValueError`, `TypeError`, and `AttributeError` from parsing are treated the
  same way.
- A 200 response holding a challenge page raises a blocked error, which feeds
  the backoff. The service does not try to get past it.
- A failed read of either sent-state table aborts that service's cycle without
  sending, rather than treating the unknown as unsent. A failed state write
  after a successful Telegram send leaves the row unsent, so the next cycle
  resends it; the at-least-once window above still applies.
- Search pages carry no full description for either site, so alerts link back to
  the listing.
- Public contact details belong to real people. The iBilik client can read
  phone and WhatsApp fields, but the alert builder drops them; keep them inside
  your own rental search and don't republish or harvest them.
