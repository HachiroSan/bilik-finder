# mudah-alerts

A small polling service that watches Mudah rental searches and pushes new
listings to Telegram. It reuses the sibling `mudah-finder/mudah.py` client, so
there is no second scraper to keep in sync. State lives in SQLite, which means
restarts and overlapping keyword searches don't resend the same ad.

## Permission gate

Mudah's [robots.txt](https://www.mudah.my/robots.txt) states that automated
access requires permission. Every network call stays disabled until this
variable is set:

```pwsh
$env:MUDAH_AUTOMATION_ALLOWED = "1"
```

Set it only after Mudah has authorised your use. The service will not solve
CAPTCHAs, rotate identities, or evade blocks. The `--dry-run` flag still reads
Mudah, so it needs the same permission. No live call happens during tests.

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
already targets 3.14.

```pwsh
Copy-Item config.example.json config.json
# edit config.json for your searches
py mudah_alerts.py --config config.json
```

`--config` defaults to `config.json` next to the script. Relative
`database_path` values resolve from the config file's folder,
not from wherever you launched the process. That keeps scheduled runs stable.

## Config

```json
{
  "poll_interval_seconds": 120,
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
| `poll_interval_seconds` | 120 | Minimum 60. Each cycle waits this long after finishing. |
| `freshness_hours` | 48 | Listings older than this are ignored. Maximum 48. |
| `location` | `malaysia` | A Mudah location slug. Resolve areas with `mudah.py locations`. |
| `rental_type` | `room` | `room`, `property`, `apartment_condo`, `house`, or `commercial`. |
| `max_price` | none | Monthly rent ceiling in ringgit. Applied by Mudah where possible and checked locally. |
| `max_pages_per_keyword` | 2 | 1 to 5. A cycle stops early when a page has no next page. |
| `page_pacing_seconds` | 2 | Sleep between pages and between keywords. |
| `search_timeout_seconds` | 30 | Per-request timeout. |
| `database_path` | `mudah_alerts.db` | SQLite file. Relative paths sit beside the config. |
| `keywords` | required | Strings, or objects with an optional per-keyword `max_price`. |

Numbers must be finite and inside their range. A poll below 60 seconds, a
freshness window above 48 hours, a negative rent, a NaN, or an unknown rental
type stops startup with a clear message. A `lock_path` key is rejected: the
lock is derived from `database_path` so a config cannot bypass the singleton
guard.

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

## First run

Nothing is in the database yet, so the first cycle sends every matching listing
that falls inside the freshness window. For `Platinum Victory 3` that could be a
handful of messages. If you want to see the list without sending, run once with
`--dry-run`. It prints the listing ids it would send and changes no state.

## Dedupe across restarts and keywords

The SQLite key is the Mudah listing id, one row per listing. Two overlapping
keyword searches that return the same ad produce one alert. A restart reads the
same table, so a listing already sent stays sent. A previously failed listing
stays retryable because it has no `sent_at`.

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

Each alert is HTML, with the title and quotes escaped and only an `https` URL
on a `mudah.my` host linked. Oversized or malformed URLs are skipped, and the
message is never sliced through the middle of the link markup. The body carries
the title, the monthly rent, a relative posted time, and an "Open on Mudah"
link. A listing with a missing, negative, or non-finite price is skipped rather
than sent without an asking price, and the rent is shown exactly (for example
`RM 1,234.50`) rather than rounded.

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
no-op sleep, so no Mudah or Telegram traffic happens.

```pwsh
py -m unittest -v test_mudah_alerts.py
```

Covered invariants: the 48-hour config cap and runtime hard cap, Malaysian
midnight and relative dates, strict clock and relative parsing, month-first and
dotted dates, leap-day handling, `published_at` priority over `site_date` and
`modified_at`, local rent ceilings with per-keyword overrides, duplicates
across restarts and keywords, per-page delivery with a later-page failure,
send-time freshness recheck, Telegram failure, 429 with and without
`retry_after`, growing failure backoff, provider halt on block/429, keyword
pacing, dry-run isolation, the derived singleton lock, fail-closed state reads,
schema exceptions, and token redaction.

Run the finder's own regression suite after changing `mudah.py`:

```pwsh
cd ..\mudah-finder
py -m unittest -v test_mudah.py
```

## Limits

- The finder parses Mudah's current RSC data. A schema change shows up as a
  logged parse error and an empty cycle, not a crash loop. Unexpected
  `ValueError`, `TypeError`, and `AttributeError` from parsing are treated the
  same way.
- A 200 response holding a challenge page raises a blocked error, which feeds
  the backoff. The service does not try to get past it.
- A failed read of the sent-state table aborts the cycle without sending, rather
  than treating the unknown as unsent. A failed state write after a successful
  Telegram send leaves the row unsent, so the next cycle resends it; the
  at-least-once window above still applies.
- Search pages carry no full description, so alerts link back to the listing.
- Public contact details belong to real people. Keep them inside your own
  rental search and don't republish or harvest them.
